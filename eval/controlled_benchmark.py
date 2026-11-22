"""Run a controlled OpenSearch comparison on one prepared DOT artifact.

Both method variants use the same code revision. The baseline replaces only
method.py with the pinned upstream file. Each timed sample uses a new process.
Reader equality is checked once per small Java scope in separate processes.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile

import psutil

from eval.benchmark import digest, measure
from eval.measure_command import run
from eval.opensearch_benchmark import OPENSEARCH_COMMIT, SCOPES, main as prepare_native

ROOT = Path(__file__).resolve().parents[1]
BASELINE_COMMIT = "f8bb418fc3eeb514f4b1dfc1fdce8040470fe3e8"
BASELINE_METHOD_SHA256 = "be5d4c5a99aa58ca25b809feb56597d34c194a297d541cf6c5987df0bd5e5927"
METHOD_PATH = "src/offline/method.py"
READER_SCOPES = ("notifier", "snapshots-info", "recovery-exception")
PREPARED_SCOPES = ("snapshots", *READER_SCOPES)


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def git(*args, cwd=ROOT):
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


def environment():
    cpu = Path("/proc/cpuinfo")
    models = sorted({line.split(":", 1)[1].strip() for line in cpu.read_text().splitlines()
                     if line.startswith("model name")}) if cpu.exists() else []
    return {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "commit": git("rev-parse", "HEAD"),
        "tracked_changes": git("diff", "HEAD", "--stat"),
        "harness_sha256": digest(Path(__file__)),
        "python": sys.version, "platform": platform.platform(),
        "python_hash_seed": os.environ.get("PYTHONHASHSEED", "random"),
        "python_dont_write_bytecode": os.environ.get("PYTHONDONTWRITEBYTECODE"),
        "cpu_models": models, "logical_cpus": psutil.cpu_count(),
        "cpu_affinity": psutil.Process().cpu_affinity() if hasattr(psutil.Process, "cpu_affinity") else None,
        "memory_total_bytes": psutil.virtual_memory().total,
        "versions": {name: importlib.metadata.version(name)
                     for name in ("networkx", "pydot", "pyparsing", "psutil")},
        "runner": {name: os.environ[name] for name in (
            "GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_REF", "GITHUB_RUN_ID",
            "GITHUB_RUN_ATTEMPT", "RUNNER_OS", "RUNNER_ARCH", "ImageOS", "ImageVersion",
        ) if name in os.environ},
    }


def artifact_manifest(directory):
    return {path.relative_to(directory).as_posix(): {
        "bytes": path.stat().st_size, "sha256": digest(path),
    } for path in sorted(directory.rglob("*")) if path.is_file()}


def compare_artifacts(expected, actual):
    """Compare every file and every byte, including JSON array order."""
    left, right = artifact_manifest(expected), artifact_manifest(actual)
    missing, extra = sorted(left.keys() - right.keys()), sorted(right.keys() - left.keys())
    changed = [name for name in sorted(left.keys() & right.keys())
               if (expected / name).read_bytes() != (actual / name).read_bytes()]
    return {"equal": not (missing or extra or changed), "files": len(left),
            "missing": missing, "extra": extra, "changed": changed}


def run_order(repeats):
    for pair in range(1, repeats + 1):
        order = ("baseline", "local") if pair % 2 else ("local", "baseline")
        for variant in order:
            yield pair, variant


def statistics_for(samples):
    return {"count": len(samples), "values": samples,
            "median": statistics.median(samples), "min": min(samples), "max": max(samples)}


def summarize(rows):
    result = {}
    for variant in ("baseline", "local"):
        good = [row for row in rows if row["variant"] == variant and row["status"] == "ok"]
        names = sorted({name for row in good for name in row["metrics"]})
        result[variant] = {name: statistics_for([row["metrics"][name] for row in good]) for name in names}
    ratios = []
    for pair in sorted({row["pair"] for row in rows}):
        matched = {row["variant"]: row for row in rows if row["pair"] == pair and row["status"] == "ok"}
        if len(matched) == 2:
            ratios.append({"pair": pair, "baseline_divided_by_local": {
                metric: matched["baseline"]["metrics"][metric] / matched["local"]["metrics"][metric]
                for metric in ("total.wall_sec", "flow_and_fsm.wall_sec")
                if matched["local"]["metrics"][metric] > 0}})
    result["paired_ratios"] = ratios
    result["note"] = "Completed samples only. No confidence interval or cold-cache claim. Check status and sample counts."
    return result


def prepared_input(root, scope):
    manifest = json.loads((root / "inputs.json").read_text(encoding="utf-8"))
    if manifest["status"] != "ok" or manifest["source_commit"] != OPENSEARCH_COMMIT:
        raise ValueError("Input preparation is incomplete or the source commit is wrong")
    row = manifest["scopes"][scope]
    dot = root / scope / "export.dot"
    source = root / scope / "source-manifest.json"
    if digest(dot) != row["dot_sha256"] or digest(source) != row["source_manifest_sha256"]:
        raise ValueError(f"Input digest mismatch for {scope}")
    source_data = json.loads(source.read_text(encoding="utf-8"))
    if source_data["commit"] != OPENSEARCH_COMMIT or source_data["scope"] != SCOPES[scope]:
        raise ValueError(f"Source manifest mismatch for {scope}")
    return dot, source, row


def prepare(args):
    args.output.mkdir(parents=True, exist_ok=False)
    inputs = args.output / "inputs"
    inputs.mkdir()
    report = {"schema_version": 1, "status": "running", "source_commit": OPENSEARCH_COMMIT,
              "environment": environment(), "scopes": {}}
    try:
        for scope in PREPARED_SCOPES:
            native = args.output / "native" / scope
            code = prepare_native([
                "--source-checkout", str(args.source_checkout), "--joern-home", str(args.joern_home),
                "--scope", scope, "--stop-after", "export", "--output", str(native),
                "--stage-timeout-sec", str(args.timeout_sec), "--max-rss-mib", str(args.max_rss_mib),
            ])
            target = inputs / scope
            target.mkdir()
            for path in (*native.glob("*.json"), *native.glob("*.log")):
                shutil.copyfile(path, target / path.name)
            if code:
                raise RuntimeError(f"Preparation failed for {scope}: exit {code}")
            shutil.copyfile(native / "dot" / "export.dot", target / "export.dot")
            report["scopes"][scope] = {
                "dot_sha256": digest(target / "export.dot"),
                "dot_bytes": (target / "export.dot").stat().st_size,
                "source_manifest_sha256": digest(target / "source-manifest.json"),
            }
            save_json(inputs / "inputs.json", report)
        report["status"] = "ok"
    except Exception as error:
        report.update(status="error", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        save_json(inputs / "inputs.json", report)
    return 0


def compare_methods(args):
    dot, source, input_record = prepared_input(args.inputs, args.scope)
    if git("diff", "HEAD", "--name-only"):
        raise ValueError("Commit tracked changes before creating the measured worktrees")
    args.output.mkdir(parents=True, exist_ok=False)
    revision = git("rev-parse", "HEAD")
    baseline_source = subprocess.check_output(["git", "show", f"{BASELINE_COMMIT}:{METHOD_PATH}"], cwd=ROOT)
    if hashlib.sha256(baseline_source).hexdigest() != BASELINE_METHOD_SHA256:
        raise ValueError("Pinned baseline method.py digest mismatch")
    report = {
        "schema_version": 1, "status": "running", "experiment": "method_edge_scan",
        "environment": environment(), "scope": args.scope, "input": input_record,
        "source_commit": OPENSEARCH_COMMIT, "variant_commit": revision,
        "baseline_method_commit": BASELINE_COMMIT, "repeats_per_variant": args.repeats,
        "order": [{"pair": pair, "variant": variant} for pair, variant in run_order(args.repeats)],
        "limits": {"per_process_timeout_sec": args.timeout_sec, "max_rss_mib": args.max_rss_mib},
        "equality_scope": "Every byte of every generated file, including all FLOW/FSM JSON, trie and index. No normalization.",
        "file_cache": "SHA256 reads the same DOT before each sample. OS file cache is warm; Python processes are new.",
        "variants": {}, "runs": [],
    }
    save = lambda: save_json(args.output / "comparison.json", report)
    reference = None
    with tempfile.TemporaryDirectory(prefix="cpg-rca-method-variants-") as directory:
        roots = {}
        try:
            for variant in ("baseline", "local"):
                path = Path(directory) / variant
                git("worktree", "add", "--detach", str(path), revision)
                roots[variant] = path
                if variant == "baseline":
                    (path / METHOD_PATH).write_bytes(baseline_source)
                changed = git("diff", "HEAD", "--name-only", cwd=path).splitlines()
                expected = [METHOD_PATH] if variant == "baseline" else []
                if changed != expected:
                    raise AssertionError(f"Unexpected variant changes: {variant}: {changed}")
                report["variants"][variant] = {"changed_files": changed,
                                                "method_sha256": digest(path / METHOD_PATH)}
            (args.output / "baseline-method.patch").write_text(
                git("diff", "HEAD", "--", METHOD_PATH, cwd=roots["baseline"]) + "\n", encoding="utf-8")
            save()
            for index, (pair, variant) in enumerate(run_order(args.repeats), 1):
                # Verify and read the complete input before every timed process.
                if digest(dot) != input_record["dot_sha256"]:
                    raise ValueError("DOT changed during the experiment")
                name = f"{index:02d}-{variant}"
                output = args.output / "runs" / name
                command_report = args.output / "commands" / f"{name}.json"
                command = [sys.executable, str(ROOT / "eval/benchmark.py"), "--dot", str(dot),
                           "--output", str(output), "--service", f"opensearch-{args.scope}",
                           "--source-manifest", str(source), "--code-root", str(roots[variant]),
                           "--dot-parser", "joern"]
                code = run(command, command_report, args.timeout_sec, args.max_rss_mib * 2**20)
                outer = json.loads(command_report.read_text(encoding="utf-8"))
                row = {"run": index, "pair": pair, "variant": variant, "status": "error",
                       "command_report": command_report.relative_to(args.output).as_posix(), "metrics": {}}
                report["runs"].append(row)
                if code:
                    row["failure"] = outer["status"]
                    raise RuntimeError(f"Sample {name} failed: {outer['status']}")
                measured = json.loads((output / "benchmark.json").read_text(encoding="utf-8"))
                if measured["status"] != "ok" or measured["counts"]["entrypoints_error"]:
                    raise AssertionError(f"Incomplete pipeline sample: {name}")
                if measured["meta"]["method_module_sha256"] != report["variants"][variant]["method_sha256"]:
                    raise AssertionError(f"Wrong method module loaded: {name}")
                if measured["meta"]["python_hash_seed"] != "0" or measured["input"]["sha256"] != input_record["dot_sha256"]:
                    raise AssertionError(f"Sample configuration changed: {name}")
                artifacts = output / "artifacts" / f"opensearch-{args.scope}"
                files = artifact_manifest(artifacts)
                expected_entries = measured["counts"]["entrypoints"]
                if not expected_entries or not measured["graph"]["edges"]:
                    raise AssertionError(f"No method workload in sample: {name}")
                if sum(name.endswith(".flow.json") for name in files) != expected_entries or \
                        sum(name.endswith(".fsm.json") for name in files) != expected_entries:
                    raise AssertionError(f"Missing FLOW or FSM output: {name}")
                save_json(output / "artifact-manifest.json", files)
                if reference is None:
                    reference = artifacts
                    report["reference_run"] = index
                    report["graph"] = measured["graph"]
                    report["counts"] = measured["counts"]
                    report["coverage"] = measured["coverage"]
                row["equality"] = compare_artifacts(reference, artifacts)
                if not row["equality"]["equal"] or any(
                    measured[key] != report[key] for key in ("graph", "counts", "coverage")
                ):
                    raise AssertionError(f"Output changed in sample {name}")
                row["metrics"] = {
                    "total.wall_sec": outer["wall_sec"],
                    "total.cpu_sec": outer["cpu_user_sec"] + outer["cpu_system_sec"],
                    "total.rss_sampled_peak_bytes": outer["aggregate_rss_sampled_peak_bytes"],
                }
                for stage in measured["stages"]:
                    for metric in ("wall_sec", "cpu_sec", "rss_sampled_peak_bytes"):
                        row["metrics"][f"{stage['stage']}.{metric}"] = stage[metric]
                row["status"] = "ok"
                report["summary"] = summarize(report["runs"])
                save()
            report["status"] = "ok"
        except Exception as error:
            report.update(status="error", error=f"{type(error).__name__}: {error}")
            raise
        finally:
            report["summary"] = summarize(report["runs"])
            save()
            for path in roots.values():
                git("worktree", "remove", "--force", str(path))
    return 0


def graph_payload(graph):
    return {"type": type(graph).__name__, "graph": graph.graph,
            "nodes": list(graph.nodes(data=True)), "edges": list(graph.edges(keys=True, data=True))}


def payload_digest(payload):
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


def reader_case(args):
    import builder

    dot, _, record = prepared_input(args.inputs, args.scope)
    if dot.stat().st_size > 8 * 2**20:
        raise ValueError("Reader equality is limited to DOT files at most 8 MiB")
    args.output.mkdir(parents=True, exist_ok=False)
    report = {"status": "running", "scope": args.scope, "input": record,
              "environment": environment(), "stages": [], "readers": {}}
    save = lambda: save_json(args.output / "equality.json", report)
    try:
        graphs = {reader: measure(f"{reader}_load", lambda: builder.load_cpg(dot, dot_parser=reader),
                                  report["stages"], save) for reader in ("pydot", "joern")}
        payloads = {reader: graph_payload(graph) for reader, graph in graphs.items()}
        report["graph_sha256"] = {reader: payload_digest(payload) for reader, payload in payloads.items()}
        report["graph_equal"] = payloads["pydot"] == payloads["joern"]
        report["graph_scope"] = "Graph metadata; ordered nodes and attributes; ordered edges, keys and attributes. Attribute-map key order is not tested."
        if not report["graph_equal"]:
            raise AssertionError("The complete ordered graphs differ")
        for reader, graph in graphs.items():
            output = args.output / reader
            output.mkdir()

            def build():
                templates, _ = builder.build_log_trie(graph, output, max_ddg_depth=5)
                rows = builder.build_entrypoint_artifacts(graph, templates, output, flow_max_depth=5)
                if any(row["status"] != "ok" for row in rows):
                    raise AssertionError(f"Failed entrypoint under {reader}")
                return {"nodes": graph.number_of_nodes(), "edges": graph.number_of_edges(),
                        "templates": len(templates), "entrypoints": len(rows)}

            report["readers"][reader] = measure(f"{reader}_artifacts", build, report["stages"], save)
            save_json(args.output / f"{reader}-manifest.json", artifact_manifest(output))
        report["artifacts"] = compare_artifacts(args.output / "pydot", args.output / "joern")
        if not report["artifacts"]["equal"]:
            raise AssertionError("Serialized FLOW/FSM/trie/index bytes differ")
        report["status"] = "ok"
    except Exception as error:
        report.update(status="error", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        save()
    return 0


def readers(args):
    args.output.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": 1, "status": "running", "environment": environment(), "scopes": []}
    try:
        for scope in READER_SCOPES:
            command = [sys.executable, "-m", "eval.controlled_benchmark", "reader-case", "--inputs",
                       str(args.inputs), "--scope", scope, "--output", str(args.output / scope)]
            command_report = args.output / f"{scope}-command.json"
            code = run(command, command_report, args.timeout_sec, args.max_rss_mib * 2**20)
            row = {"scope": scope, "exit_code": code,
                   "command": json.loads(command_report.read_text(encoding="utf-8"))}
            equality = args.output / scope / "equality.json"
            if equality.exists():
                row["equality"] = json.loads(equality.read_text(encoding="utf-8"))
            report["scopes"].append(row)
            save_json(args.output / "readers.json", report)
        report["status"] = "ok" if all(row["exit_code"] == 0 for row in report["scopes"]) else "error"
    except Exception as error:
        report.update(status="error", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        save_json(args.output / "readers.json", report)
    return 0 if report["status"] == "ok" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, function in (("prepare", prepare), ("compare", compare_methods),
                           ("readers", readers), ("reader-case", reader_case)):
        command = commands.add_parser(name)
        command.set_defaults(function=function)
        command.add_argument("--output", type=Path, required=True)
        if name == "prepare":
            command.add_argument("--source-checkout", type=Path, required=True)
            command.add_argument("--joern-home", type=Path, required=True)
        else:
            command.add_argument("--inputs", type=Path, required=True)
        if name in ("compare", "reader-case"):
            command.add_argument("--scope", choices=PREPARED_SCOPES, default="snapshots")
        if name == "compare":
            command.add_argument("--repeats", type=int, default=3)
        if name != "reader-case":
            command.add_argument("--timeout-sec", type=int, default=180 if name == "readers" else 600)
            command.add_argument("--max-rss-mib", type=int, default=4096)
    args = parser.parse_args(argv)
    if os.environ.get("PYTHONHASHSEED") != "0":
        parser.error("Run with PYTHONHASHSEED=0")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    if getattr(args, "repeats", 3) not in range(3, 11):
        parser.error("Use 3 to 10 repetitions per variant")
    if getattr(args, "timeout_sec", 1) < 1 or getattr(args, "max_rss_mib", 1) < 1:
        parser.error("Time and memory limits must be positive")
    for key in ("output", "inputs", "source_checkout", "joern_home"):
        value = getattr(args, key, None)
        if value is not None:
            setattr(args, key, value.resolve())
    return args.function(args)


if __name__ == "__main__":
    raise SystemExit(main())
