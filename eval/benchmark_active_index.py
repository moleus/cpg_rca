"""Bounded scan/index A/B test of the real compact runtime store.

Both variants use the same revision and synthetic loop input. Only
index_hypotheses changes. Time runs use no tracemalloc; memory runs are separate.
"""

import argparse
from contextlib import redirect_stdout
from dataclasses import asdict
import gc
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import time
import tracemalloc

from eval.benchmark_compact_history import digest, loop_fsm, storage_counts
from eval.measure_command import run as run_limited
from src.online.stateful_chain_store import ExplicitHypothesisStore


ROOT = Path(__file__).resolve().parents[1]
BASE_COMMIT = "77f5f54ec1391974069db245679373de67d4389f"
SIZES = (100, 1000, 5000, 20000)
SOURCES = ("src/online/stateful_chain_store.py", "src/online/stream_processor.py",
           "src/online/hypothesis_report.py", "eval/benchmark_active_index.py",
           "eval/benchmark_compact_history.py", "eval/measure_command.py",
           "tests/test_active_hypothesis_index.py")


def source_hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def measure(variant, events, trace):
    fsm = loop_fsm()
    input_spec = {"fsm": asdict(fsm), "events": events, "message": "tick", "call_id": "l",
                  "timestamp": None, "indices": "0..events-1", "new_chains": "first event only",
                  "beam_limits": [1, 1, 1], "compact_history": True}
    gc.collect()
    if trace:
        tracemalloc.start()
    start_wall, start_cpu = time.perf_counter(), time.process_time()
    store = ExplicitHypothesisStore(
        {"loop": fsm}, compact_history=True, index_hypotheses=variant == "indexed",
        max_active_chains=1, max_hypotheses_per_chain=1, max_total_active_hypotheses=1,
    )
    for index in range(events):
        store.process("tick", index=index, observed_call_node_id="l")
        store.allow_new_chains = False
    wall, cpu = time.perf_counter() - start_wall, time.process_time() - start_cpu
    rss_peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    current_python, peak_python = tracemalloc.get_traced_memory() if trace else (None, None)
    if trace:
        tracemalloc.stop()

    # Validation is outside the measured stage. Public reads below may turn
    # the index off. Check that it was live throughout the timed processing.
    chain = store.active_chains[0]
    index_live = chain._usable_index() is not None
    assert index_live == (variant == "indexed")
    hypotheses = list(chain.hypotheses.values())
    last = chain.active_hypotheses[0]
    assert len(hypotheses) == events
    assert [h._path_length() for h in hypotheses] == list(range(1, events + 1))
    assert [h._history.log.index for h in hypotheses] == list(range(events))
    assert [log.index for log in last.logs] == list(range(events))
    assert last.transition_ids == ("t",) * events
    counts = storage_counts(hypotheses)
    assert counts["history_nodes"] == events
    assert counts["active_hypotheses"] == 1
    evidence = {
        "events": [asdict(event) for event in store.history],
        "last_path": asdict(last),
        "retained_metadata": [
            [h.hypothesis_id, h.current_state, h.status, h.termination_reason, h.score,
             h._path_length(), h._history.log.index] for h in hypotheses
        ],
        "chain": [chain.chain_id, chain.fsm_key, chain.status, chain.termination_reason,
                  chain.created_at, chain.last_timestamp, chain.correlation_key],
    }
    return {
        "variant": variant, "events": events, "tracemalloc_enabled": trace,
        "input_sha256": digest(input_spec), "evidence_sha256": digest(evidence),
        "store_process_wall_sec": wall, "store_process_cpu_sec": cpu,
        "process_peak_rss_through_store_bytes": rss_peak,
        "current_python_allocation_bytes": current_python,
        "peak_python_allocation_bytes": peak_python,
        "index_live_before_validation": index_live, **counts,
    }


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def summarize(report):
    summary = []
    for events in report["limits"]["sizes"]:
        for variant in ("scan", "indexed"):
            rows = [r for r in report["runs"] if r["events"] == events and
                    r["variant"] == variant and r["status"] == "ok"]
            normal = [r for r in rows if not r["tracemalloc_enabled"]]
            traced = [r for r in rows if r["tracemalloc_enabled"]]
            if not normal or not traced:
                continue
            times = [r["store_process_wall_sec"] for r in normal]
            summary.append({
                "events": events, "variant": variant, "timing_repeats": len(normal),
                "wall_sec_values": times, "median_wall_sec": statistics.median(times),
                "min_wall_sec": min(times), "max_wall_sec": max(times),
                "max_process_peak_rss_bytes": max(r["process_peak_rss_through_store_bytes"] for r in normal),
                "peak_python_allocation_bytes": traced[0]["peak_python_allocation_bytes"],
                "hypotheses_retained": traced[0]["hypotheses_retained"],
                "retained_log_slots": traced[0]["retained_log_slots"],
            })
    mismatches = []
    for events in report["limits"]["sizes"]:
        rows = [r for r in report["runs"] if r["events"] == events and r["status"] == "ok"]
        if len({r["input_sha256"] for r in rows}) > 1 or len({r["evidence_sha256"] for r in rows}) > 1:
            mismatches.append(events)
    expected = len(report["limits"]["sizes"]) * 2 * (report["limits"]["timing_repeats"] + 1)
    report.update(summary=summary, evidence_mismatch_sizes=mismatches,
                  complete=len(report["runs"]) == expected)
    failed = mismatches or any(r["status"] != "ok" for r in report["runs"])
    report["status"] = "failed" if failed else "ok" if report["complete"] else "running"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sizes", type=int, nargs="+", default=list(SIZES))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout-sec", type=int, default=120)
    parser.add_argument("--max-rss-mib", type=int, default=1024)
    parser.add_argument("--worker", choices=("scan", "indexed"), help=argparse.SUPPRESS)
    parser.add_argument("--trace", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if (not 1 <= len(args.sizes) <= 4 or len(set(args.sizes)) != len(args.sizes) or
            any(not 1 <= n <= 20000 for n in args.sizes) or not 1 <= args.repeats <= 3 or
            not 1 <= args.timeout_sec <= 180 or not 128 <= args.max_rss_mib <= 2048):
        parser.error("Use up to four distinct sizes 1..20000; 1..3 repeats; timeout 1..180s; RSS 128..2048 MiB.")
    if os.environ.get("PYTHONHASHSEED") != "0":
        parser.error("Run with PYTHONHASHSEED=0")
    args.output = args.output.resolve()
    if args.worker:
        if len(args.sizes) != 1:
            parser.error("A worker needs one size.")
        write_json(args.output, measure(args.worker, args.sizes[0], args.trace))
        return 0

    report = {
        "experiment": "active_hypothesis_index", "schema_version": 1, "synthetic": True,
        "baseline_design_commit": BASE_COMMIT, "measured_code_commit": git("rev-parse", "HEAD"),
        "tracked_worktree_clean": not bool(git("diff", "HEAD", "--name-only")),
        "sources_sha256": source_hashes(),
        "variants": "Same measured revision and compact_history=True; only index_hypotheses changes.",
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "python_hash_seed": os.getenv("PYTHONHASHSEED"),
                        "execution": "GitHub Actions job" if os.getenv("GITHUB_ACTIONS") == "true"
                                     else "local shared workspace",
                        "packages": {p: importlib.metadata.version(p) for p in ("pandas", "networkx", "psutil")}},
        "limits": {"sizes": args.sizes, "timing_repeats": args.repeats, "traced_repeats": 1,
                   "worker_timeout_sec": args.timeout_sec, "worker_max_rss_mib": args.max_rss_mib},
        "measurement_scope": {
            "time": "Store construction and process() calls. Timing repeats have no tracemalloc. No public live views are read during processing.",
            "python_memory": "Separate tracing run. Includes the store and retained history; excludes imports, FSM, validation and snapshots.",
            "process_rss": "Linux process high-water mark through store processing, including imports; not an allocation delta.",
            "outer_rss": "Process-tree RSS sampled every 50ms, including validation and output.",
            "equality": "Full final path, all events, retained metadata and chain metadata. Branch/report equality is checked in pytest.",
        },
        "runs": [],
    }
    work = args.output.parent / (args.output.stem + "-runs")
    work.mkdir(parents=True, exist_ok=True)
    for events in args.sizes:
        for repeat in range(args.repeats + 1):
            trace = repeat == args.repeats
            for variant in (("scan", "indexed") if repeat % 2 == 0 else ("indexed", "scan")):
                if source_hashes() != report["sources_sha256"]:
                    report.update(status="source_changed", complete=False)
                    write_json(args.output, report)
                    raise RuntimeError("Measured source changed between samples.")
                name = f"n{events}-{variant}-{'trace' if trace else repeat}"
                result_path, process_path = work / (name + ".json"), work / (name + "-process.json")
                command = [sys.executable, "-m", "eval.benchmark_active_index", "--worker", variant,
                           "--sizes", str(events), "--output", str(result_path)]
                if trace:
                    command.append("--trace")
                with redirect_stdout(io.StringIO()):
                    run_limited(command, process_path, args.timeout_sec, args.max_rss_mib * 2**20)
                process = json.loads(process_path.read_text())
                row = {"events": events, "variant": variant, "repeat": repeat,
                       "tracemalloc_enabled": trace, "status": process["status"],
                       "outer_wall_sec": process["wall_sec"],
                       "outer_peak_rss_bytes": process["aggregate_rss_sampled_peak_bytes"],
                       "returncode": process["returncode"]}
                if row["status"] == "ok":
                    row.update(json.loads(result_path.read_text()))
                report["runs"].append(row)
                summarize(report)
                write_json(args.output, report)
                print(f"N={events} variant={variant} trace={trace} status={row['status']}", flush=True)
    print(json.dumps({"status": report["status"], "summary": report["summary"]}, indent=2))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
