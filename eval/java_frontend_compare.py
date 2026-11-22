"""Compare the pinned Java frontend with an isolated JavaParser replacement.

This is a diagnostic experiment on a fixed challenge set. It does not change
the installed Joern release or establish general binary compatibility.
"""

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import zipfile

from eval.measure_command import run
from eval.opensearch_benchmark import JOERN_ARCHIVE_SHA256, JOERN_VERSION, OPENSEARCH_COMMIT


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = Path("frontends/javasrc2cpg")
LAUNCHER = FRONTEND / "lib/io.joern.javasrc2cpg-4.0.689-launcher.jar"
MAVEN = "https://repo.maven.apache.org/maven2/com/github/javaparser"
REPLACEMENT_SHA256 = {
    "javaparser-core": "b5499a3b1c40b16c0671fabe478c9aafeab38160c6fde74a6c13f42d86716ecd",
    "javaparser-symbol-solver-core": "4cb097c0834427939c01f4169dd0bab20068673913341c5b6db27be1a7445098",
}
# Selected before this comparison from known syntax/AST failures. This is not a
# random sample or a complete package. Imports outside the set remain absent.
OPENSEARCH_FILES = (
    "server/src/main/java/org/opensearch/common/Rounding.java",
    "server/src/main/java/org/opensearch/common/util/LocaleUtils.java",
    "server/src/main/java/org/opensearch/index/mapper/DerivedFieldSupportedTypes.java",
    "server/src/main/java/org/opensearch/index/search/NestedHelper.java",
    "server/src/main/java/org/opensearch/ingest/ValueSource.java",
    "server/src/main/java/org/opensearch/wlm/ResourceType.java",
    "plugins/ingestion-hive/src/main/java/org/opensearch/plugin/hive/HiveShardConsumer.java",
    "sandbox/plugins/analytics-backend-datafusion/src/main/java/org/opensearch/be/datafusion/docvalues/bridge/DecodedBatch.java",
)
FIXTURES = {
    "yield": "eval/fixtures/java/YieldRepro.java",
    "enum_lambda": "eval/fixtures/java/enum_lambda/EnumLambdaRepro.java",
}
CASES = (*FIXTURES, "opensearch_modern_java")
LINKAGE_ERRORS = (
    "NoSuchMethodError", "NoSuchFieldError", "NoClassDefFoundError",
    "AbstractMethodError", "IncompatibleClassChangeError", "VerifyError",
    "UnsupportedClassVersionError", "ExceptionInInitializerError", "LinkageError",
)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def manifest_fields(raw):
    text = raw.decode("utf-8").replace("\r\n", "\n").replace("\n ", "")
    return dict(line.split(": ", 1) for line in text.splitlines() if ": " in line)


def encode_manifest(fields):
    lines = []
    for key, value in fields.items():
        line = f"{key}: {value}"
        # The pinned launcher manifest contains ASCII. Limit each physical line
        # to 72 bytes, including CRLF; continuation spaces are not value data.
        line.encode("ascii")
        lines.append(line[:70])
        line = line[70:]
        while line:
            lines.append(" " + line[:69])
            line = line[69:]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def inventory(home):
    files = [path for path in (home / FRONTEND).rglob("*") if path.is_file()]
    files += [home / "javasrc2cpg", home / "conf/log4j2.xml"]
    with zipfile.ZipFile(home / LAUNCHER) as archive:
        fields = manifest_fields(archive.read("META-INF/MANIFEST.MF"))
    return {
        "files": {path.relative_to(home).as_posix(): digest(path) for path in sorted(files)},
        "launcher_main_class": fields["Main-Class"],
        "launcher_class_path": fields["Class-Path"].split(),
    }


def verify_baseline(home, archive_path):
    if digest(archive_path) != JOERN_ARCHIVE_SHA256:
        raise ValueError("Joern archive does not match the pinned SHA-256")
    result = inventory(home)
    # Check the frontend and shared exporter libraries against the same archive.
    # Extra files in these locations are also rejected.
    checked = dict(result["files"])
    for path in (home / "lib").glob("*.jar"):
        checked[path.relative_to(home).as_posix()] = digest(path)
    for relative in ("joern-export", "bin/joern-export"):
        checked[relative] = digest(home / relative)
    with zipfile.ZipFile(archive_path) as archive:
        for relative, actual in checked.items():
            expected = hashlib.sha256(archive.read("joern-cli/" + relative)).hexdigest()
            if actual != expected:
                raise ValueError(f"Installed Joern file differs from its archive: {relative}")
    expected_jars = {f"com.github.javaparser.{name}-3.28.0.jar" for name in REPLACEMENT_SHA256}
    actual_jars = {name for name in result["launcher_class_path"] if "javaparser" in name}
    if actual_jars != expected_jars:
        raise ValueError(f"Unexpected baseline JavaParser Class-Path: {actual_jars}")
    result["archive_sha256"] = JOERN_ARCHIVE_SHA256
    result["verified_exporter_files"] = len(checked) - len(result["files"])
    return result


def prepare_candidate(home, replacement_dir, target):
    replacements = {}
    for name, expected in REPLACEMENT_SHA256.items():
        path = replacement_dir / f"{name}-3.28.2.jar"
        if digest(path) != expected:
            raise ValueError(f"Replacement checksum mismatch: {name}")
        replacements[name] = path
    # Copy the complete Java frontend distribution. Other language frontends
    # are not used or copied. No hard links or writes to the baseline are used.
    target.mkdir(parents=True, exist_ok=False)
    shutil.copytree(home / FRONTEND, target / FRONTEND)
    (target / "conf").mkdir()
    shutil.copy2(home / "conf/log4j2.xml", target / "conf/log4j2.xml")
    shutil.copy2(home / "javasrc2cpg", target / "javasrc2cpg")
    with zipfile.ZipFile(target / LAUNCHER) as archive:
        entries = [(entry, archive.read(entry)) for entry in archive.infolist()]
    fields = manifest_fields(dict((entry.filename, data) for entry, data in entries)["META-INF/MANIFEST.MF"])
    class_path = fields["Class-Path"].split()
    for name, replacement in replacements.items():
        old = f"com.github.javaparser.{name}-3.28.0.jar"
        new = f"com.github.javaparser.{name}-3.28.2.jar"
        if class_path.count(old) != 1:
            raise ValueError(f"Expected one launcher Class-Path entry for {old}")
        class_path[class_path.index(old)] = new
        (target / FRONTEND / "lib" / old).unlink()
        shutil.copy2(replacement, target / FRONTEND / "lib" / new)
    fields["Class-Path"] = " ".join(class_path)
    with zipfile.ZipFile(target / LAUNCHER, "w") as archive:
        for entry, data in entries:
            archive.writestr(entry, encode_manifest(fields) if entry.filename == "META-INF/MANIFEST.MF" else data)
    return inventory(target)


def prepare_sources(checkout, output, cases):
    if "opensearch_modern_java" in cases:
        actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
        if actual != OPENSEARCH_COMMIT:
            raise ValueError(f"Expected OpenSearch {OPENSEARCH_COMMIT}, got {actual}")
    manifests = {}
    for case in cases:
        records = []
        paths = OPENSEARCH_FILES if case == "opensearch_modern_java" else (FIXTURES[case],)
        for relative in paths:
            if case == "opensearch_modern_java":
                # Read committed content, not a modified working-tree file.
                data = subprocess.check_output(["git", "show", f"{OPENSEARCH_COMMIT}:{relative}"], cwd=checkout)
            else:
                data = (ROOT / relative).read_bytes()
            destination = output / "sources" / case / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            records.append({"path": relative, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        manifests[case] = {
            "case": case, "files": records, "java_files": len(records),
            "source_bytes": sum(record["bytes"] for record in records),
            "source_commit": OPENSEARCH_COMMIT if case == "opensearch_modern_java" else None,
            "selection": "fixed_known_failure_challenge_set" if case == "opensearch_modern_java" else "synthetic_reproducer",
            "fetch_dependencies": False,
        }
        save(output / "manifests" / f"{case}.json", manifests[case])
    return manifests


def diagnostics(directory):
    logs = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in sorted(directory.glob("parse.*.log")))
    problems = re.findall(r"Encountered problems while parsing '([^']+)'", logs)
    failed = re.findall(r"Failed to process '([^']+)'", logs)
    unknown_types = re.findall(r"unknown statement of type class ([\w.$]+)", logs)
    return {
        "warning_lines": sum(" WARN " in line for line in logs.splitlines()),
        "parse_problem_warning_lines": len(problems),
        "parse_problem_reported_names": sorted(set(problems)),
        "failed_to_process_warning_lines": len(failed),
        "failed_to_process_reported_names": sorted(set(failed)),
        "unparsable_statement_warning_lines": sum("UnparsableStmt" in line and " WARN " in line for line in logs.splitlines()),
        "unknown_statement_warning_lines": len(unknown_types),
        "unknown_statement_types": dict(Counter(unknown_types)),
        "linkage_error_names": [name for name in LINKAGE_ERRORS if name in logs],
        "note": "Warnings can repeat. Reported basenames do not always identify unique paths.",
    }


def count_graph(dot, output, source_root):
    from src.offline.joern_dot import parse_joern_dot
    from src.offline.log import LOG_METHOD_NAMES, _clean, _find_parent_method

    graph = parse_joern_dot(dot.read_text(encoding="utf-8"))
    labels, call_names, controls = Counter(), Counter(), Counter()
    methods, log_calls, unknown_nodes = [], [], []

    def filename(value):
        value = _clean(value)
        prefix = str(source_root) + "/"
        return value[len(prefix):] if value.startswith(prefix) else value

    for node_id, attributes in graph.nodes(data=True):
        label = _clean(attributes.get("label", ""))
        labels[label] += 1
        if label == "METHOD":
            methods.append({
                "name": _clean(attributes.get("NAME", "")),
                "full_name": _clean(attributes.get("FULL_NAME", "")),
                "filename": filename(attributes.get("FILENAME", "")),
                "line": _clean(attributes.get("LINE_NUMBER", "")),
                "is_external": _clean(attributes.get("IS_EXTERNAL", "false")).lower() == "true",
            })
        elif label == "CALL":
            name = _clean(attributes.get("NAME", ""))
            call_names[name] += 1
            if name.lower() in LOG_METHOD_NAMES:
                _, owner = _find_parent_method(graph, node_id)
                owner_attributes = graph.nodes[owner] if owner is not None else {}
                log_calls.append({
                    "name": name, "code": _clean(attributes.get("CODE", "")),
                    "method_full_name": _clean(attributes.get("METHOD_FULL_NAME", "")),
                    "owner_full_name": _clean(owner_attributes.get("FULL_NAME", "")),
                    "filename": filename(owner_attributes.get("FILENAME", "")),
                    "line": _clean(attributes.get("LINE_NUMBER", "")),
                })
        elif label == "CONTROL_STRUCTURE":
            controls[_clean(attributes.get("CONTROL_STRUCTURE_TYPE", ""))] += 1
        elif label == "UNKNOWN":
            unknown_nodes.append({key.lower(): _clean(attributes.get(key, "")) for key in (
                "CODE", "PARSER_TYPE_NAME", "LINE_NUMBER",
            )})
    sort_key = lambda record: json.dumps(record, sort_keys=True)
    save(output, {
        "schema_version": 1, "dot_sha256": digest(dot),
        "nodes": graph.number_of_nodes(), "edges": graph.number_of_edges(),
        "labels": dict(labels), "method_count": len(methods),
        "internal_method_count": sum(not method["is_external"] for method in methods),
        "call_count": labels["CALL"], "log_call_candidate_count": len(log_calls),
        "call_names": dict(call_names), "control_structures": dict(controls),
        "methods": sorted(methods, key=sort_key), "log_call_candidates": sorted(log_calls, key=sort_key),
        "unknown_nodes": sorted(unknown_nodes, key=sort_key),
        "graph_stage": "shared_joern_export_with_its_default_overlays",
        "log_call_definition": "CALL NAME lowercased in src.offline.log.LOG_METHOD_NAMES; this is a name heuristic, not a source-site oracle",
    })


def stage(command, output, timeout, rss_bytes):
    run(command, output, timeout, rss_bytes)
    return json.loads(output.read_text())


def compare_results(results):
    comparisons = []
    for case in dict.fromkeys(result["case"] for result in results):
        arms = {result["variant"]: result for result in results if result["case"] == case}
        baseline, candidate = arms["bundled"], arms["javaparser_3_28_2"]
        deltas = None
        if baseline["counts"] is not None and candidate["counts"] is not None:
            deltas = {key: candidate["counts"][key] - baseline["counts"][key] for key in (
                "method_count", "internal_method_count", "call_count", "log_call_candidate_count", "nodes", "edges",
            )}
        comparisons.append({
            "case": case, "count_deltas_candidate_minus_bundled": deltas,
            "counts_available_for_both": deltas is not None,
            "status_by_variant": {name: arm["status"] for name, arm in arms.items()},
            "parse_measurements": {name: {
                key: arm["stages"]["parse"][key] for key in (
                    "status", "returncode", "wall_sec", "aggregate_rss_sampled_peak_bytes",
                )
            } for name, arm in arms.items() if "parse" in arm["stages"]},
        })
    return comparisons


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    count = subparsers.add_parser("count")
    count.add_argument("--dot", type=Path, required=True)
    count.add_argument("--output", type=Path, required=True)
    count.add_argument("--source-root", type=Path, required=True)
    compare = subparsers.add_parser("run")
    compare.add_argument("--source-checkout", type=Path, required=True)
    compare.add_argument("--joern-home", type=Path, required=True)
    compare.add_argument("--joern-archive", type=Path, required=True)
    compare.add_argument("--replacement-dir", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    compare.add_argument("--cases", nargs="+", choices=CASES, default=list(CASES))
    compare.add_argument("--parse-timeout-sec", type=int, default=180)
    compare.add_argument("--export-timeout-sec", type=int, default=60)
    compare.add_argument("--max-rss-mib", type=int, default=3072)
    args = parser.parse_args(argv)
    if args.action == "count":
        count_graph(args.dot, args.output, args.source_root)
        return 0
    if min(args.parse_timeout_sec, args.export_timeout_sec, args.max_rss_mib) < 1:
        parser.error("time and RSS limits must be positive")
    if len(args.cases) != len(set(args.cases)):
        parser.error("cases must be unique")
    java = shutil.which("java")
    if java is None:
        parser.error("Java must be on PATH")
    java = Path(java).resolve()
    java_home = java.parents[1]
    output, home = args.output.resolve(), args.joern_home.resolve()
    output.mkdir(parents=True, exist_ok=False)
    baseline = verify_baseline(home, args.joern_archive.resolve())
    candidate_home = output / "candidate-distribution"
    candidate = prepare_candidate(home, args.replacement_dir.resolve(), candidate_home)
    save(output / "inventory-bundled.json", baseline)
    save(output / "inventory-javaparser_3_28_2.json", candidate)
    sources = prepare_sources(args.source_checkout.resolve(), output, args.cases)
    rss_bytes = args.max_rss_mib * 2**20
    flags = ["-java-home", str(java_home), "-J-Xmx2g", "-J-XX:ActiveProcessorCount=2"]
    probes, results = {}, []
    for variant, frontend_home, version in (
        ("bundled", home, "3.28.0"), ("javaparser_3_28_2", candidate_home, "3.28.2"),
    ):
        probe_path = output / "probes" / f"{variant}.json"
        probe = stage([str(java), "-Xmx512m", "-XX:ActiveProcessorCount=2", "-cp",
                       str(frontend_home / LAUNCHER), str(ROOT / "eval/FrontendClasspath.java")], probe_path, 30, rss_bytes)
        values = dict(line.split("=", 1) for line in probe_path.with_suffix(".stdout.log").read_text().splitlines() if "=" in line)
        expected = {
            "core_path": frontend_home / FRONTEND / f"lib/com.github.javaparser.javaparser-core-{version}.jar",
            "symbol_solver_path": frontend_home / FRONTEND / f"lib/com.github.javaparser.javaparser-symbol-solver-core-{version}.jar",
        }
        probe["class_origins"] = values
        probe["expected_class_origins_loaded"] = all(values.get(key) == str(path) for key, path in expected.items())
        probes[variant] = probe
        save(probe_path, probe)
        for case in args.cases:
            directory = output / "runs" / case / variant
            directory.mkdir(parents=True)
            record = {
                "case": case, "variant": variant, "status": "setup_failed",
                "cpg_completeness": "not_established", "stages": {}, "counts": None,
            }
            results.append(record)
            if probe["status"] != "ok" or not probe["expected_class_origins_loaded"]:
                save(directory / "summary.json", record)
                continue
            source, cpg = output / "sources" / case, directory / "cpg.bin"
            commands = [
                ("parse", [str(frontend_home / "javasrc2cpg"), *flags, str(source), "--output", str(cpg)], args.parse_timeout_sec),
                ("export", [str(home / "joern-export"), *flags, str(cpg), "--repr", "all", "--format", "dot", "--out", str(directory / "dot")], args.export_timeout_sec),
                ("count", [sys.executable, "-m", "eval.java_frontend_compare", "count", "--dot", str(directory / "dot/export.dot"), "--output", str(directory / "counts.json"), "--source-root", str(source)], 30),
            ]
            for name, command, timeout in commands:
                print(f"Starting {case}/{variant}/{name}", flush=True)
                measurement = stage(command, directory / f"{name}.json", timeout, rss_bytes)
                record["stages"][name] = measurement
                if measurement["status"] != "ok":
                    record["status"] = f"{name}_failed"
                    break
                if name == "parse" and (not cpg.is_file() or cpg.stat().st_size == 0):
                    record["status"] = "missing_cpg"
                    break
            record["diagnostics"] = diagnostics(directory)
            record["cpg_bytes"] = cpg.stat().st_size if cpg.is_file() else None
            if record["stages"].get("count", {}).get("status") == "ok" and (directory / "counts.json").is_file():
                record["counts"] = json.loads((directory / "counts.json").read_text())
                record["status"] = "measurements_complete"
            if record["diagnostics"]["linkage_error_names"]:
                record["status"] = "linkage_failure"
            elif record["status"] == "measurements_complete" and record["diagnostics"]["warning_lines"]:
                record["status"] = "measurements_complete_with_frontend_diagnostics"
            save(directory / "summary.json", record)
    final_inventory = inventory(home)
    unchanged = final_inventory == {key: baseline[key] for key in final_inventory}
    complete = unchanged and all(record["status"] in (
        "measurements_complete", "measurements_complete_with_frontend_diagnostics",
    ) for record in results)
    summary = {
        "schema_version": 1, "experiment": "isolated_java_frontend_dependency_comparison",
        "status": "measurements_complete" if complete else "measurement_or_compatibility_failure",
        "cpg_completeness": "not_established",
        "baseline_unchanged": unchanged, "joern_version": JOERN_VERSION,
        "harness_sha256": digest(Path(__file__)),
        "opensearch_commit": OPENSEARCH_COMMIT, "sources": sources,
        "replacement_artifacts": {name: {"sha256": checksum, "url": f"{MAVEN}/{name}/3.28.2/{name}-3.28.2.jar"} for name, checksum in REPLACEMENT_SHA256.items()},
        "environment": {"python": platform.python_version(), "platform": platform.platform(), "java": str(java), "logical_cpus": os.cpu_count()},
        "probes": probes, "results": results, "comparisons": compare_results(results),
        "limitations": [
            "Fixed challenge set, selected from known failures; not representative OpenSearch coverage.",
            "Only the two JavaParser JARs and launcher Class-Path change. All other dependencies remain bundled.",
            "Successful class loading and these runs do not establish general binary or semantic compatibility.",
            "No project dependencies are fetched. Missing imports can affect type resolution in both arms.",
            "One sequential run per arm and case; timings are observations, not a speedup estimate.",
            "Counts measure exported structure. Equal or larger counts do not establish graph correctness.",
            "A zero exit code does not establish complete parsing; diagnostics are reported separately.",
            "No OpenSearch runtime, incident data, or RCA accuracy is evaluated.",
        ],
    }
    save(output / "comparison.json", summary)
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
