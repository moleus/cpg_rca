"""Probe bundled jimple2cpg on two javac-compiled standard-library fixtures.

This route requires compiled bytecode. It is separate from the source frontend
dependency comparison and does not compile the OpenSearch challenge set.
"""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import shutil
import struct
import sys
import zipfile

from eval.java_frontend_compare import FIXTURES, ROOT, count_graph, diagnostics, digest, save, stage
from eval.opensearch_benchmark import JOERN_ARCHIVE_SHA256, JOERN_VERSION


def binary_inventory(home, archive_path):
    if digest(archive_path) != JOERN_ARCHIVE_SHA256:
        raise ValueError("Joern archive does not match the pinned SHA-256")
    paths = [path for path in (home / "frontends/jimple2cpg").rglob("*") if path.is_file()]
    paths += list((home / "lib").glob("*.jar"))
    paths += [home / relative for relative in ("jimple2cpg", "joern-export", "bin/joern-export", "conf/log4j2.xml")]
    result = {path.relative_to(home).as_posix(): digest(path) for path in sorted(paths)}
    with zipfile.ZipFile(archive_path) as archive:
        for relative, actual in result.items():
            expected = hashlib.sha256(archive.read("joern-cli/" + relative)).hexdigest()
            if actual != expected:
                raise ValueError(f"Installed Joern file differs from its archive: {relative}")
    return result


def class_manifest(directory):
    records = []
    for path in sorted(directory.rglob("*.class")):
        data = path.read_bytes()
        if len(data) < 8:
            raise ValueError(f"Truncated class file: {path.name}")
        magic, minor, major = struct.unpack(">IHH", data[:8])
        if magic != 0xCAFEBABE or major != 65:
            raise ValueError(f"Expected Java 21 class format for {path.name}, got magic={magic:x}, major={major}")
        records.append({"path": path.relative_to(directory).as_posix(), "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(), "major": major, "minor": minor})
    if not records:
        raise ValueError("Compilation did not produce class files")
    return records


def instruction_evidence(disassembly):
    return {
        "long_xor_instruction": bool(re.search(r"\b\d+:\s+lxor\b", disassembly)),
        "integer_add_instruction": bool(re.search(r"\b\d+:\s+iadd\b", disassembly)),
        "lambda_method_names": sorted(set(re.findall(r"\b(lambda\$[\w$]+)\(", disassembly))),
    }


def graph_evidence(dot):
    from src.offline.joern_dot import parse_joern_dot
    from src.offline.log import _clean, _find_parent_method

    graph = parse_joern_dot(dot.read_text(encoding="utf-8"))
    evidence = []
    for node_id, attributes in graph.nodes(data=True):
        if _clean(attributes.get("label", "")) != "CALL":
            continue
        name = _clean(attributes.get("NAME", ""))
        _, owner_id = _find_parent_method(graph, node_id)
        owner = graph.nodes[owner_id] if owner_id is not None else {}
        owner_name = _clean(owner.get("NAME", ""))
        if name in {"<operator>.xor", "<operator>.addition"} or "lambda$" in owner_name:
            evidence.append({
                "name": name, "code": _clean(attributes.get("CODE", "")),
                "owner_name": owner_name, "owner_full_name": _clean(owner.get("FULL_NAME", "")),
                "line": _clean(attributes.get("LINE_NUMBER", "")),
            })
    return {
        "xor_in_apply": any(row["name"] == "<operator>.xor" and row["owner_name"] == "apply" for row in evidence),
        "addition_in_lambda_body": any(row["name"] == "<operator>.addition" and "lambda$" in row["owner_name"] for row in evidence),
        "operator_and_lambda_calls": sorted(evidence, key=lambda row: json.dumps(row, sort_keys=True)),
        "scope": "Observed operation nodes and their AST method owners; not full semantic equivalence.",
    }


def count_and_check(dot, output, source_root, disassembly):
    count_graph(dot, output, source_root)
    result = json.loads(output.read_text())
    result["bytecode_evidence"] = instruction_evidence(disassembly.read_text(encoding="utf-8"))
    result["operation_evidence"] = graph_evidence(dot)
    save(output, result)


def fixture_quality_gates(case, counts):
    if counts is None:
        return {"status": "not_evaluated", "checks": {}}
    bytecode, operations = counts["bytecode_evidence"], counts["operation_evidence"]
    if case == "yield":
        checks = {
            "javap_has_lxor": bytecode["long_xor_instruction"],
            "cpg_has_xor_in_apply": operations["xor_in_apply"],
        }
    elif case == "enum_lambda":
        lambda_methods = set(bytecode["lambda_method_names"])
        checks = {
            "javap_has_lambda_method": bool(lambda_methods),
            "javap_has_iadd": bytecode["integer_add_instruction"],
            "cpg_has_addition_in_that_lambda": any(
                call["name"] == "<operator>.addition" and call["owner_name"] in lambda_methods
                for call in operations["operator_and_lambda_calls"]
            ),
        }
    else:
        raise ValueError(f"Unknown fixture: {case}")
    return {"status": "passed" if all(checks.values()) else "failed", "checks": checks,
            "scope": "Named fixture operations only; UNKNOWN nodes are retained and full graph equivalence is not established."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    count = commands.add_parser("count")
    count.add_argument("--dot", type=Path, required=True)
    count.add_argument("--output", type=Path, required=True)
    count.add_argument("--source-root", type=Path, required=True)
    count.add_argument("--disassembly", type=Path, required=True)
    probe = commands.add_parser("run")
    probe.add_argument("--joern-home", type=Path, required=True)
    probe.add_argument("--joern-archive", type=Path, required=True)
    probe.add_argument("--output", type=Path, required=True)
    probe.add_argument("--parse-timeout-sec", type=int, default=120)
    probe.add_argument("--export-timeout-sec", type=int, default=60)
    probe.add_argument("--max-rss-mib", type=int, default=2048)
    args = parser.parse_args(argv)
    if args.action == "count":
        count_and_check(args.dot, args.output, args.source_root, args.disassembly)
        return 0
    if min(args.parse_timeout_sec, args.export_timeout_sec, args.max_rss_mib) < 1:
        parser.error("time and RSS limits must be positive")
    java_name = shutil.which("java")
    if java_name is None:
        parser.error("JDK 21 must be on PATH")
    java = Path(java_name).resolve()
    home, output = args.joern_home.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    inventory = binary_inventory(home, args.joern_archive.resolve())
    save(output / "binary-inventory.json", inventory)
    rss_bytes = args.max_rss_mib * 2**20
    tool = [str(java), "-Xmx512m", "-XX:ActiveProcessorCount=2", str(ROOT / "eval/JdkTool.java")]
    flags = ["-java-home", str(java.parents[1]), "-J-Xmx1g", "-J-XX:ActiveProcessorCount=2"]
    runtime = stage([str(java), "-version"], output / "runtime-version.json", 30, rss_bytes)
    runtime_text = "\n".join((output / f"runtime-version.{stream}.log").read_text().strip()
                             for stream in ("stdout", "stderr")).strip()
    version = stage([*tool, "javac", "--version"], output / "compiler-version.json", 30, rss_bytes)
    version_text = (output / "compiler-version.stdout.log").read_text().strip()
    if runtime["status"] != "ok" or version["status"] != "ok" or not re.fullmatch(r"javac 21(?:\.[\w.+-]+)*", version_text):
        save(output / "summary.json", {"status": "unsupported_compiler", "compiler": version_text, "measurement": version})
        return 1
    help_result = stage([str(home / "jimple2cpg"), *flags, "--help"], output / "frontend-help.json", 30, rss_bytes)
    if help_result["status"] != "ok":
        save(output / "summary.json", {"status": "frontend_help_failed", "measurement": help_result})
        return 1
    records = []
    for case, relative in FIXTURES.items():
        directory = output / "runs" / case
        source_dir, classes = directory / "source", directory / "classes"
        source_dir.mkdir(parents=True)
        classes.mkdir()
        source = source_dir / Path(relative).name
        source.write_bytes((ROOT / relative).read_bytes())
        record = {
            "case": case, "route": "javac_then_bundled_jimple2cpg", "status": "not_run",
            "cpg_completeness": "not_established", "counts": None, "stages": {},
            "source": {"repository_path": relative, "sha256": digest(source), "bytes": source.stat().st_size},
        }
        records.append(record)
        cpg, dot = directory / "cpg.bin", directory / "dot/export.dot"
        stage_commands = [
            ("compile", [*tool, "javac", "-source", "21", "-target", "21", "-g", "-parameters", "-d", str(classes), str(source)], 30),
            ("disassemble", [*tool, "javap", "-classpath", str(classes), "-p", "-c", source.stem], 30),
            ("parse", [str(home / "jimple2cpg"), *flags, str(classes), "--output", str(cpg)], args.parse_timeout_sec),
            ("export", [str(home / "joern-export"), *flags, str(cpg), "--repr", "all", "--format", "dot", "--out", str(dot.parent)], args.export_timeout_sec),
            ("count", [sys.executable, "-m", "eval.java_bytecode_probe", "count", "--dot", str(dot), "--output", str(directory / "counts.json"), "--source-root", str(source_dir), "--disassembly", str(directory / "disassemble.stdout.log")], 30),
        ]
        for name, command, timeout in stage_commands:
            print(f"Starting bytecode/{case}/{name}", flush=True)
            result = stage(command, directory / f"{name}.json", timeout, rss_bytes)
            record["stages"][name] = result
            if result["status"] != "ok":
                record["status"] = f"{name}_failed"
                break
            if name == "compile":
                try:
                    record["compiled_classes"] = class_manifest(classes)
                except ValueError as error:
                    record["status"], record["error"] = "invalid_compiled_classes", str(error)
                    break
            if name == "parse" and (not cpg.is_file() or cpg.stat().st_size == 0):
                record["status"] = "missing_cpg"
                break
        record["diagnostics"] = diagnostics(directory)
        parse_logs = "\n".join(path.read_text(encoding="utf-8", errors="replace")
                               for path in directory.glob("parse.*.log"))
        record["diagnostics"]["error_lines"] = sum(" ERROR " in line for line in parse_logs.splitlines())
        if record["stages"].get("count", {}).get("status") == "ok" and (directory / "counts.json").is_file():
            record["counts"] = json.loads((directory / "counts.json").read_text())
            record["unknown_node_count"] = record["counts"]["labels"].get("UNKNOWN", 0)
            observations = record["diagnostics"]["warning_lines"] or record["diagnostics"]["error_lines"] or record["unknown_node_count"]
            record["status"] = "measurements_complete_with_graph_observations" if observations else "measurements_complete"
        if record["diagnostics"]["linkage_error_names"]:
            record["status"] = "linkage_failure"
        record["quality_gates"] = fixture_quality_gates(case, record["counts"])
        save(directory / "summary.json", record)
    unchanged = binary_inventory(home, args.joern_archive.resolve()) == inventory
    complete = unchanged and all(record["status"] in (
        "measurements_complete", "measurements_complete_with_graph_observations",
    ) for record in records)
    gates_passed = all(record["quality_gates"]["status"] == "passed" for record in records)
    save(output / "summary.json", {
        "schema_version": 1, "experiment": "compiled_standard_library_fixtures_only",
        "status": "fixture_quality_gates_passed" if complete and gates_passed else "measurement_or_quality_gate_failure",
        "measurement_status": "complete" if complete else "failed",
        "quality_gate_status": "passed" if gates_passed else "failed_or_not_evaluated",
        "cpg_completeness": "not_established", "joern_version": JOERN_VERSION,
        "joern_archive_sha256": JOERN_ARCHIVE_SHA256, "baseline_unchanged": unchanged,
        "harness_sha256": digest(Path(__file__)), "compiler": version_text,
        "runtime_version": runtime_text,
        "environment": {"python": platform.python_version(), "platform": platform.platform(), "java": str(java)},
        "results": records,
        "limitations": [
            "Only two standard-library fixtures are compiled. OpenSearch is not compiled or evaluated by this route.",
            "Compilation uses the JDK javac ToolProvider with source/target 21; every class must have major version 65.",
            "Compiler and disassembler timings include the Java source-launcher helper overhead.",
            "Bytecode can add compiler-generated methods and lose source structure. Counts are not directly equivalent to source frontend counts.",
            "Operation nodes and AST owners are inspected; this is not complete CFG, data-flow, or semantic equivalence.",
            "One diagnostic run per fixture; no statistical speedup or runtime RCA accuracy claim.",
        ],
    })
    return 0 if complete and gates_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
