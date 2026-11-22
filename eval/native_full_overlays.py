"""Apply explicit overlays to the saved OpenSearch native CPG with one time budget.

The default input is the pinned artifact from Actions run 45024003121. No
source parse, DOT export, NetworkX load, or console importCpg operation occurs.
"""

import argparse
import gzip
import json
import os
from pathlib import Path
import shutil
import time

from eval.measure_command import run
from eval.native_overlay_stages import LAYERS
from eval.opensearch_benchmark import JOERN_VERSION, OPENSEARCH_COMMIT
from eval.opensearch_large_scale import digest, environment, save

ROOT = Path(__file__).resolve().parents[1]
FULL = {
    "kind": "saved_repository_scope_with_default_excludes",
    "run_id": 45024003121,
    "artifact_name": "opensearch-full-native-graph-45024003121-1",
    "archive_sha256": "d66ef7890bf8c07d00ada632617e7f0deb1eeca928fe4513132799d239cbd362",
    "raw_sha256": "a07cfe1932801ec453057c74897eb0483c2e7e3fc02cb913dd06db28127504ab",
    "raw_bytes": 74131467, "nodes": 5101213, "edges": 9367638,
    "manifest_java_files": 13770, "default_excluded_java_files": 4657,
    "default_eligible_java_files": 9113,
    "scope_note": "Default Java excludes omit path component test, including 353 src/main/java files. Three AstCreator failures leave FILE without AST. Source completeness is not established.",
}
SMOKE = {
    "kind": "one_file_notifier_smoke",
    "archive_sha256": "1c827246d9b37b420f5fb62e22c89273b23bad97837ca92f1b377c09cf542b68",
    "raw_sha256": "003d4378fdfe06dd95720a0b059944cd890c819e1a4ebacc94a5763af6f4198e",
    "raw_bytes": 70735, "nodes": 323, "edges": 535,
    "manifest_java_files": 1, "default_excluded_java_files": 0,
    "default_eligible_java_files": 1,
}


def restore_verified_archive(archive, target, expected, deadline):
    """Reject a different archive, excess decoded bytes, or a different graph."""
    if digest(archive) != expected["archive_sha256"]:
        raise ValueError("Archive SHA256 does not match the pinned input")
    decoded = 0
    with gzip.open(archive, "rb") as source, target.open("xb") as output:
        while chunk := source.read(2**20):
            decoded += len(chunk)
            if decoded > expected["raw_bytes"]:
                raise ValueError("Decoded graph exceeds the pinned byte count")
            if time.perf_counter() >= deadline:
                raise TimeoutError("Total processing budget expired during archive restore")
            output.write(chunk)
    if decoded != expected["raw_bytes"] or digest(target) != expected["raw_sha256"]:
        raise ValueError("Native CPG byte count or SHA256 does not match the pinned input")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--joern-home", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--smoke", action="store_true", help="Use the fixed one-file notifier archive")
    parser.add_argument("--heap-gib", type=int, default=8)
    parser.add_argument("--processors", type=int, default=4)
    parser.add_argument("--max-rss-mib", type=int, default=12288)
    parser.add_argument("--total-timeout-sec", type=float, default=1200)
    args = parser.parse_args(argv)
    if not (1 <= args.heap_gib <= 8 and 1 <= args.processors <= 4 and
            256 <= args.max_rss_mib <= 12288 and 1 <= args.total_timeout_sec <= 1200):
        parser.error("Use heap 1..8 GiB, processors 1..4, RSS 256..12288 MiB and total time <=1200s")
    joern = args.joern_home.resolve()
    if not (joern / "lib" / f"io.joern.joern-cli-{JOERN_VERSION[1:]}.jar").is_file():
        parser.error("Use the pinned Joern 4.0.689 release")
    if not shutil.which("taskset") or not hasattr(os, "sched_getaffinity"):
        parser.error("Linux taskset and CPU affinity are required")
    cpus = sorted(os.sched_getaffinity(0))[:args.processors]
    if len(cpus) != args.processors:
        parser.error("Not enough available logical CPUs for the requested bound")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    expected = SMOKE if args.smoke else FULL
    java_options = f"-Xmx{args.heap_gib}g -XX:ActiveProcessorCount={args.processors}"
    started = time.perf_counter()
    deadline = started + args.total_timeout_sec
    original = output / "input-cpg.bin"
    report = {
        "schema_version": 1, "status": "running", "source_commit": OPENSEARCH_COMMIT,
        "input": expected, "environment": environment(),
        "overlay_script_sha256": digest(ROOT / "eval/apply_native_overlay.sc"),
        "overlay_driver_sha256": digest(Path(__file__)),
        "native_parse_executed": False, "dot_exported": False, "networkx_loaded": False,
        "source_completeness": "not_established", "rca_pipeline_executed": False,
        "limits": {"requested_heap_gib": args.heap_gib, "requested_processors": args.processors,
                   "cpu_affinity": cpus, "JDK_JAVA_OPTIONS": java_options,
                   "sampled_aggregate_rss_mib": args.max_rss_mib,
                   "total_processing_timeout_sec": args.total_timeout_sec,
                   "timeout_note": "One deadline covers restore and all workers. Worker termination and final hashes can add a short cleanup interval."},
        "stages_requested": list(LAYERS), "stages": [], "completed_overlays": [],
        "last_successful_stage": None, "last_successful_counts": None,
        "timing_note": "One fresh process per layer. Apply time excludes load, counts and persistence. Outer process time includes those tasks and Scala/JVM startup. This is a feasibility run, not a speedup comparison.",
    }
    save(output / "summary.json", report)
    exit_code = 1
    try:
        restore_verified_archive(args.archive.resolve(), original, expected, deadline)
        report["input_verified"] = True
        report["restore_wall_sec"] = time.perf_counter() - started
        current = original
        previous_counts = None
        for index, layer in enumerate(LAYERS, 1):
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                report.update(status="total_time_budget_exhausted", next_overlay=layer)
                exit_code = 124
                break
            directory = output / f"{index:02d}-{layer}"
            directory.mkdir()
            target = directory / "cpg.bin"
            input_hash = digest(current)
            command = ["env", f"JDK_JAVA_OPTIONS={java_options}", "taskset", "--cpu-list",
                       ",".join(map(str, cpus)), str(joern / "joern"),
                       f"-J-Xmx{args.heap_gib}g", f"-J-XX:ActiveProcessorCount={args.processors}",
                       "--script", str(ROOT / "eval/apply_native_overlay.sc"),
                       "--param", f"input={current}", "--param", f"output={target}",
                       "--param", f"layer={layer}", "--param", f"report={directory / 'overlay.json'}",
                       "--param", f"expectedHeapGiB={args.heap_gib}",
                       "--param", f"expectedProcessors={args.processors}"]
            # Recompute after hashes and setup, so time is not reset per layer.
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                report.update(status="total_time_budget_exhausted", next_overlay=layer)
                exit_code = 124
                break
            exit_code = run(command, directory / "process.json", remaining, args.max_rss_mib * 2**20)
            row = {"overlay": layer, "exit_code": exit_code,
                   "process": json.loads((directory / "process.json").read_text()),
                   "input_sha256_before": input_hash, "input_sha256_after": digest(current)}
            if (directory / "overlay.json").exists():
                row["details"] = json.loads((directory / "overlay.json").read_text())
            if target.exists():
                row["output_cpg"] = {"bytes": target.stat().st_size, "sha256": digest(target)}
            report["stages"].append(row)
            save(output / "summary.json", report)
            if row["input_sha256_after"] != input_hash:
                raise AssertionError("An overlay worker changed its input file")
            details = row.get("details", {})
            if exit_code or details.get("status") != "ok":
                report.update(status="overlay_not_completed", failed_overlay=layer)
                exit_code = exit_code or 1
                break
            if previous_counts is not None and details["before"] != previous_counts:
                raise AssertionError("Reloaded graph counts differ from the prior completed stage")
            if index == 1 and (details["before"]["nodes"] != expected["nodes"] or
                               details["before"]["edges"] != expected["edges"] or details["before"]["overlays"]):
                raise AssertionError("Initial native counts or overlays differ from the pinned raw graph")
            completed = list(LAYERS[:index])
            if details["after"]["overlays"] != completed:
                raise AssertionError("Unexpected overlay sequence")
            previous_counts = details["after"]
            report.update(last_successful_stage=layer, last_successful_counts=previous_counts,
                          completed_overlays=completed)
            save(output / "summary.json", report)
            current = target
        else:
            report["status"] = "all_explicit_overlays_completed"
            exit_code = 0
    except Exception as error:
        report.update(status="error", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        report["processing_wall_sec"] = time.perf_counter() - started
        if original.exists():
            report["original_cpg_unchanged"] = digest(original) == expected["raw_sha256"]
        save(output / "summary.json", report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
