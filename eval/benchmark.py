"""Measure the existing DOT -> trie -> FLOW/FSM pipeline without changing it.

Run each repetition in a new process. Stage RSS samples show resident memory,
not allocated bytes. The OS high-water mark is cumulative for the process.
"""

import argparse
import hashlib
import importlib.metadata
import json
import logging
import os
import platform
import resource
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def git_value(*args, cwd=None):
    result = subprocess.run(["git", *args], cwd=cwd or Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def measure(name, function, stages, save):
    process = psutil.Process()
    samples = [process.memory_info().rss]
    stop = threading.Event()

    def sample():
        while not stop.wait(0.02):
            samples.append(process.memory_info().rss)

    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    start_wall, start_cpu = time.perf_counter(), time.process_time()
    status = "ok"
    try:
        return function()
    except BaseException:
        status = "error"
        raise
    finally:
        wall, cpu = time.perf_counter() - start_wall, time.process_time() - start_cpu
        stop.set()
        thread.join()
        samples.append(process.memory_info().rss)
        high_water = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform != "darwin":
            high_water *= 1024
        stages.append({"stage": name, "status": status, "wall_sec": wall,
                       "cpu_sec": cpu, "rss_start_bytes": samples[0],
                       "rss_end_bytes": samples[-1], "rss_sampled_peak_bytes": max(samples),
                       "process_rss_high_water_bytes": high_water})
        save()
        print(f"{name}: {wall:.3f}s wall, {cpu:.3f}s CPU, "
              f"{max(samples) / 2**20:.1f} MiB sampled RSS", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True,
                        help="New directory. Existing directories are rejected.")
    parser.add_argument("--service", default="benchmark")
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--code-root", type=Path,
                        help="Optional checkout whose builder and coverage modules are measured.")
    parser.add_argument("--dot-parser", choices=("pydot", "joern"), default="pydot",
                        help="joern requires a checkout that implements the optional strict reader.")
    parser.add_argument("--max-ddg-depth", type=int, default=5)
    parser.add_argument("--flow-max-depth", type=int, default=5)
    args = parser.parse_args(argv)
    code_root = (args.code_root or Path(__file__).resolve().parents[1]).resolve()
    if args.code_root:
        sys.path.insert(0, str(code_root))
    import builder
    from eval.coverage import service_metrics
    from src.offline import method

    if Path(args.service).name != args.service or args.service in (".", ".."):
        parser.error("service must be one directory name")
    args.output.mkdir(parents=True, exist_ok=False)
    service_dir = args.output / "artifacts" / args.service
    service_dir.mkdir(parents=True)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    report = {
        "schema_version": 1,
        "status": "running",
        "meta": {
            "commit": git_value("rev-parse", "HEAD", cwd=code_root),
            "tracked_changes": bool(git_value("diff", "HEAD", "--stat", cwd=code_root)),
            "benchmark_module_sha256": digest(Path(__file__)),
            "builder_module_sha256": digest(Path(builder.__file__)),
            "method_module_sha256": digest(Path(method.__file__)),
            "dot_parser": args.dot_parser,
            "python": sys.version.split()[0],
            "python_hash_seed": os.environ.get("PYTHONHASHSEED", "random"),
            "platform": platform.platform(),
            "cpu_count": psutil.cpu_count(),
            "memory_total_bytes": psutil.virtual_memory().total,
            "versions": {name: importlib.metadata.version(name)
                         for name in ("networkx", "pydot", "pyparsing", "psutil")},
            "rss_sample_interval_sec": 0.02,
            "process_rss_high_water_is_cumulative": True,
            "max_ddg_depth": args.max_ddg_depth,
            "flow_max_depth": args.flow_max_depth,
        },
        "input": {"file": args.dot.name, "bytes": args.dot.stat().st_size,
                  "sha256": digest(args.dot)},
        "stages": [],
    }
    if args.source_manifest:
        report["source"] = json.loads(args.source_manifest.read_text(encoding="utf-8"))

    def save():
        (args.output / "benchmark.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    save()
    wall = time.perf_counter()
    try:
        loader_options = {"dot_parser": "joern"} if args.dot_parser == "joern" else {}
        graph = measure("load_dot", lambda: builder.load_cpg(args.dot, **loader_options),
                        report["stages"], save)
        report["graph"] = {"nodes": graph.number_of_nodes(), "edges": graph.number_of_edges()}
        templates, _ = measure("templates_and_trie", lambda: builder.build_log_trie(
            graph, service_dir, max_ddg_depth=args.max_ddg_depth), report["stages"], save)
        rows = measure("flow_and_fsm", lambda: builder.build_entrypoint_artifacts(
            graph, templates, service_dir, flow_max_depth=args.flow_max_depth),
            report["stages"], save)
        report["counts"] = {"templates": len(templates), "entrypoints": len(rows),
                            "entrypoints_error": sum(row["status"] != "ok" for row in rows)}
        report["coverage"] = measure("coverage", lambda: service_metrics(service_dir),
                                     report["stages"], save)
        report["artifact_bytes"] = sum(path.stat().st_size for path in service_dir.rglob("*")
                                       if path.is_file())
        report["status"] = "ok" if not report["counts"]["entrypoints_error"] else "error"
    except Exception as error:
        report["status"] = "error"
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        report["pipeline_wall_sec"] = time.perf_counter() - wall
        save()
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
