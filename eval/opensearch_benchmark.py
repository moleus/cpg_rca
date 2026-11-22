"""Build and measure a fixed OpenSearch source scope with Joern.

The input is public source code. This command does not run OpenSearch or collect
production logs. It does not download build dependencies or compile OpenSearch.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from eval.measure_command import run

OPENSEARCH_COMMIT = "2220907d3119897b8bd58cbc3c926787cd05ed2c"
JOERN_VERSION = "v4.0.689"
JOERN_ARCHIVE_SHA256 = "c13f0b6adbb1ea2fbf94c046e42ae023394f150f855ca6ca19ef20e20a1805b0"
SCOPES = {
    "notifier": "server/src/main/java/org/opensearch/snapshots/RestoreCompletionNotifier.java",
    "snapshots-info": "server/src/main/java/org/opensearch/snapshots/InternalSnapshotsInfoService.java",
    "recovery-exception": "server/src/main/java/org/opensearch/indices/recovery/RecoveryFailedException.java",
    "snapshots": "server/src/main/java/org/opensearch/snapshots",
    "recovery": "server/src/main/java/org/opensearch/indices/recovery",
    "server": "server/src/main/java",
    "repository": ".",
}


def parse_diagnostics(output):
    logs = "\n".join(path.read_text(encoding="utf-8", errors="replace")
                     for path in output.glob("parse.*.log"))
    problems = re.findall(r"Encountered problems while parsing '([^']+)'", logs)
    failed = re.findall(r"Failed to process '([^']+)'", logs)
    return {
        "warning_lines": sum(" WARN " in line for line in logs.splitlines()),
        "unparsable_statement_warning_lines": sum(
            " WARN " in line and "UnparsableStmt" in line for line in logs.splitlines()),
        "parse_problem_warning_lines": len(problems),
        "parse_problem_reported_names": sorted(set(problems)),
        "failed_to_process_warning_lines": len(failed),
        "failed_to_process_reported_names": sorted(set(failed)),
        "note": "Warnings can repeat. Some names are basenames, so they do not identify unique file paths.",
    }


def prepare_source(checkout, output, scope):
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    if commit != OPENSEARCH_COMMIT:
        raise ValueError(f"Expected OpenSearch {OPENSEARCH_COMMIT}, got {commit}")
    subprocess.run(["git", "diff", "--exit-code", "HEAD", "--", scope],
                   cwd=checkout, check=True, stdout=subprocess.DEVNULL)
    tracked = subprocess.check_output(["git", "ls-files", "-z", "--", scope], cwd=checkout)
    files = sorted(Path(name.decode()) for name in tracked.split(b"\0")
                   if name and name.endswith(b".java"))
    if not files:
        raise ValueError(f"No tracked Java files in {scope}")
    records = []
    for relative in files:
        data = (checkout / relative).read_bytes()
        target = output / "source" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        records.append({"path": relative.as_posix(), "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest()})
    manifest = {
        "repository": "https://github.com/opensearch-project/OpenSearch",
        "commit": commit, "scope": scope,
        "scope_kind": ("single_file" if scope.endswith(".java") else
                       "all_tracked_java_sources" if scope == "." else
                       "server_main_sources" if scope == SCOPES["server"] else "package_subset"),
        "java_files": len(records), "source_bytes": sum(row["bytes"] for row in records),
        "files": records, "joern_version": JOERN_VERSION,
        "joern_expected_archive_sha256": JOERN_ARCHIVE_SHA256, "fetch_dependencies": False,
    }
    path = output / "source-manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-checkout", type=Path, required=True)
    parser.add_argument("--joern-home", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scope", choices=SCOPES, default="snapshots")
    parser.add_argument("--stage-timeout-sec", type=int, default=600)
    parser.add_argument("--max-rss-mib", type=int, default=4096)
    parser.add_argument("--dot-parser", choices=("pydot", "joern"), default="pydot")
    parser.add_argument("--stop-after", choices=("parse", "overlays", "export", "pipeline"),
                        default="pipeline", help="Limit the requested stages. Default: complete pipeline.")
    args = parser.parse_args(argv)
    if args.stage_timeout_sec < 1 or args.max_rss_mib < 1:
        parser.error("time and memory limits must be positive")
    output = args.output.resolve()
    joern = args.joern_home.resolve()
    if not (joern / "lib" / f"io.joern.joern-cli-{JOERN_VERSION[1:]}.jar").is_file():
        parser.error(f"joern-home must contain the pinned {JOERN_VERSION} release")
    output.mkdir(parents=True, exist_ok=False)
    source_manifest = prepare_source(args.source_checkout.resolve(), output, SCOPES[args.scope])
    source, cpg = output / "source", output / "cpg.bin"
    jvm = ["-J-Xmx3g", "-J-XX:ActiveProcessorCount=2"]
    stages = [
        ("parse", [str(joern / "javasrc2cpg"), *jvm, str(source), "--output", str(cpg)]),
        ("overlays", [str(joern / "joern-parse"), *jvm, "--language", "JAVASRC",
                      "--overlaysonly", str(source), "--output", str(cpg)]),
        ("export", [str(joern / "joern-export"), *jvm, str(cpg), "--repr", "all",
                    "--format", "dot", "--out", str(output / "dot")]),
        ("pipeline", [sys.executable, "-m", "eval.benchmark", "--dot",
                      str(output / "dot" / "export.dot"), "--service", f"opensearch-{args.scope}",
                      "--source-manifest", str(source_manifest), "--output", str(output / "pipeline"),
                      "--dot-parser", args.dot_parser]),
    ]
    reports = []
    exit_code = 0
    for name, command in stages:
        print(f"Starting {name}: {args.scope}", flush=True)
        report_path = output / f"{name}.json"
        exit_code = run(command, report_path, args.stage_timeout_sec, args.max_rss_mib * 2**20)
        reports.append(json.loads(report_path.read_text(encoding="utf-8")))
        if exit_code:
            break
        if name == args.stop_after:
            break
    summary = {
        "schema_version": 1, "status": "ok" if exit_code == 0 else "error",
        "scope": args.scope, "source_commit": OPENSEARCH_COMMIT,
        "requested_last_stage": args.stop_after,
        "dot_parser": args.dot_parser,
        "parser_diagnostics": parse_diagnostics(output),
        "completed_stage_count": len(reports), "stages": reports,
        "cpg_bytes": cpg.stat().st_size if cpg.exists() else None,
        "dot_bytes": (output / "dot" / "export.dot").stat().st_size
        if (output / "dot" / "export.dot").exists() else None,
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
