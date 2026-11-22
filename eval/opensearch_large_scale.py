"""Bounded native CPG feasibility run for all tracked OpenSearch Java files.

This command runs the Java frontend and a read-only native count query. It does
not add overlays, export DOT, load NetworkX, or run the OpenSearch server.
"""

import argparse
import gzip
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time

import psutil

from eval.measure_command import run
from eval.opensearch_benchmark import JOERN_VERSION, OPENSEARCH_COMMIT, SCOPES, parse_diagnostics, prepare_source

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_LIMIT_BYTES = 200_000_000


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def environment():
    cpuinfo = Path("/proc/cpuinfo")
    cpu_models = sorted({line.split(":", 1)[1].strip() for line in cpuinfo.read_text().splitlines()
                         if line.startswith("model name")}) if cpuinfo.exists() else []
    return {
        "code_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "python": sys.version, "platform": platform.platform(), "cpu_models": cpu_models,
        "logical_cpus": psutil.cpu_count(), "memory_total_bytes": psutil.virtual_memory().total,
        "python_hash_seed": os.environ.get("PYTHONHASHSEED"),
        "psutil": importlib.metadata.version("psutil"),
        "query_sha256": digest(ROOT / "eval/native_cpg_summary.sc"),
        "driver_sha256": digest(Path(__file__)),
        "runner": {name: os.environ[name] for name in (
            "GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT",
            "RUNNER_OS", "RUNNER_ARCH", "ImageOS", "ImageVersion",
        ) if name in os.environ},
    }


def source_groups(manifest):
    """Count path patterns; a main source set can also belong to a benchmark."""
    groups = {}
    for row in manifest["files"]:
        path = "/" + row["path"]
        group = next((name for name in (
            "src/main/java", "src/test/java", "src/internalClusterTest/java",
            "src/javaRestTest/java", "src/testFixtures/java", "src/yamlRestTest/java",
        ) if f"/{name}/" in path), "other_tracked_java")
        target = groups.setdefault(group, {"java_files": 0, "source_bytes": 0})
        target["java_files"] += 1
        target["source_bytes"] += row["bytes"]
    return groups


def archive_cpg(cpg, output, *, max_bytes=ARCHIVE_LIMIT_BYTES, timeout_sec=120):
    """Keep a gzip only if it fits the byte and time budgets."""
    if not cpg.exists() or not cpg.stat().st_size:
        return {"status": "no_nonempty_cpg"}
    output.mkdir(parents=True, exist_ok=True)
    temporary = output / "native-cpg.bin.gz.partial"
    started, read_bytes, status = time.perf_counter(), 0, "ok"
    with cpg.open("rb") as source, temporary.open("wb") as target:
        with gzip.GzipFile(filename="", mode="wb", fileobj=target, compresslevel=1, mtime=0) as compressed:
            while chunk := source.read(2**20):
                compressed.write(chunk)
                read_bytes += len(chunk)
                if target.tell() > max_bytes:
                    status = "compressed_size_limit"
                    break
                if time.perf_counter() - started > timeout_sec:
                    status = "compression_time_limit"
                    break
    compressed_bytes = temporary.stat().st_size
    if compressed_bytes > max_bytes:
        status = "compressed_size_limit"
    report = {"status": status, "input_bytes_read": read_bytes, "compressed_bytes": compressed_bytes,
              "max_compressed_bytes": max_bytes, "timeout_sec": timeout_sec,
              "wall_sec": time.perf_counter() - started}
    if status == "ok":
        archive = output / "native-cpg.bin.gz"
        temporary.rename(archive)
        report.update(file=archive.name, sha256=digest(archive))
    else:
        temporary.unlink()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-checkout", type=Path, required=True)
    parser.add_argument("--joern-home", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scope", choices=("repository", "notifier"), default="repository")
    parser.add_argument("--heap-gib", type=int, default=8)
    parser.add_argument("--processors", type=int, default=4)
    parser.add_argument("--max-rss-mib", type=int, default=12288)
    parser.add_argument("--parse-timeout-sec", type=int, default=1200)
    parser.add_argument("--query-timeout-sec", type=int, default=300)
    args = parser.parse_args(argv)
    if not (1 <= args.heap_gib <= 8 and 1 <= args.processors <= 4 and
            256 <= args.max_rss_mib <= 12288 and 1 <= args.parse_timeout_sec <= 1200 and
            1 <= args.query_timeout_sec <= 300):
        parser.error("Limits: heap 1..8 GiB, CPUs 1..4, RSS 256..12288 MiB, parse <=1200s, query <=300s")
    output, joern, checkout = args.output.resolve(), args.joern_home.resolve(), args.source_checkout.resolve()
    if not (joern / "lib" / f"io.joern.joern-cli-{JOERN_VERSION[1:]}.jar").is_file():
        parser.error(f"Expected the pinned {JOERN_VERSION} Joern installation")
    output.mkdir(parents=True, exist_ok=False)
    cpg = output / "cpg.bin"
    report = {
        "schema_version": 1, "status": "running", "scope": args.scope,
        "source_commit": OPENSEARCH_COMMIT, "environment": environment(),
        "limits": {"requested_java_heap_gib": args.heap_gib, "requested_active_processors": args.processors,
                   "aggregate_rss_mib": args.max_rss_mib, "parse_timeout_sec": args.parse_timeout_sec,
                   "query_timeout_sec": args.query_timeout_sec,
                   "JDK_JAVA_OPTIONS": f"-Xmx{args.heap_gib}g -XX:ActiveProcessorCount={args.processors}",
                   "query_actual_limits": "recorded and checked inside the script JVM"},
        "stages_requested": ["native_java_parse", "native_read_only_counts"],
        "source_completeness": "not_established",
        "rca_pipeline_executed": False,
        "note": "Frontend exit zero and readable CPG do not establish source coverage or an RCA-ready graph.",
    }
    exit_code = 1
    try:
        started = time.perf_counter()
        manifest_path = prepare_source(checkout, output, SCOPES[args.scope])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        report["source"] = {key: manifest[key] for key in ("java_files", "source_bytes", "scope_kind", "scope")}
        report["source"]["path_groups"] = source_groups(manifest)
        report["source"]["path_groups_note"] = "Literal source-set paths. Main source sets can include benchmarks and test utilities."
        report["source_prepare_wall_sec"] = time.perf_counter() - started
        save(output / "summary.json", report)
        jvm = [f"-J-Xmx{args.heap_gib}g", f"-J-XX:ActiveProcessorCount={args.processors}"]
        # Joern's --script child JVM does not inherit launcher -J flags.
        # The Java environment option reaches both launcher and child JVMs.
        java_environment = ["env", f"JDK_JAVA_OPTIONS=-Xmx{args.heap_gib}g -XX:ActiveProcessorCount={args.processors}"]
        parse_code = run([*java_environment, str(joern / "javasrc2cpg"), *jvm, str(output / "source"), "--output", str(cpg)],
                         output / "parse.json", args.parse_timeout_sec, args.max_rss_mib * 2**20)
        report["parse"] = json.loads((output / "parse.json").read_text(encoding="utf-8"))
        report["frontend_exit_zero"] = parse_code == 0
        report["parser_diagnostics"] = parse_diagnostics(output)
        report["status"] = "parse_not_completed" if parse_code else "parse_completed"
        exit_code = parse_code
        if cpg.exists():
            report["cpg"] = {"bytes": cpg.stat().st_size, "sha256_after_parse": digest(cpg)}
        save(output / "summary.json", report)
        if parse_code == 0 and cpg.exists() and cpg.stat().st_size:
            before = digest(cpg)
            query_code = run([
                *java_environment, str(joern / "joern"), *jvm, "--script", str(ROOT / "eval/native_cpg_summary.sc"),
                "--param", f"input={cpg}", "--param", f"output={output / 'native-counts.json'}",
                "--param", f"expectedHeapGiB={args.heap_gib}", "--param", f"expectedProcessors={args.processors}",
            ], output / "query.json", args.query_timeout_sec, args.max_rss_mib * 2**20)
            after = digest(cpg)
            report["query"] = json.loads((output / "query.json").read_text(encoding="utf-8"))
            report["cpg"].update(sha256_before_query=before, sha256_after_query=after,
                                 unchanged_by_query=before == after)
            exit_code = query_code
            report["status"] = "parse_completed_query_failed" if query_code else "native_graph_counted"
            if query_code == 0:
                counts = json.loads((output / "native-counts.json").read_text(encoding="utf-8"))
                report["native_counts"] = counts
                if before != after:
                    raise AssertionError("Native count query changed the graph")
        elif parse_code == 0:
            report["status"], exit_code = "frontend_zero_without_nonempty_cpg", 1
    except Exception as error:
        report.update(status="error", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        report["parser_diagnostics"] = parse_diagnostics(output)
        save(output / "summary.json", report)
        report["cpg_archive"] = archive_cpg(cpg, output / "artifacts")
        report["cpg_archive"]["native_query_completed"] = report["status"] == "native_graph_counted"
        if report["cpg_archive"]["status"] == "ok":
            for filename in ("LICENSE.txt", "NOTICE.txt"):
                if (checkout / filename).is_file():
                    shutil.copyfile(checkout / filename, output / "artifacts" / filename)
        save(output / "summary.json", report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
