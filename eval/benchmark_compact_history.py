"""Compare copied and shared histories in the real store, in bounded processes.

The input is a synthetic one-state loop. It isolates retained-prefix growth.
It does not measure incident accuracy or a complete runtime report workload.
"""

import argparse
from contextlib import redirect_stdout
from dataclasses import asdict
import gc
import hashlib
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

from eval.measure_command import run as run_limited
from src.offline.finite_state_machine import ExecutionSegment, LogTransition, StaticLogFSM
from src.online.stateful_chain_store import (
    ExplicitHypothesisStore, _CompactPathHypothesis, _HistoryPrefix, _HistoryStep,
)


ROOT = Path(__file__).resolve().parents[1]
BASE_COMMIT = "b168e979006c6f5ae9c313099fc98dc8d2681a6c"
SIZES = (100, 500, 1000, 5000)


def digest(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(data).hexdigest()


def loop_fsm():
    fsm = StaticLogFSM("m", "loop", "fixture.loop")
    fsm.states = {
        "s": ExecutionSegment("s", "fixture.loop", "fixture.loop", (), (), (),
                              None, None, True, False, "START_SEGMENT"),
    }
    fsm.transitions = [LogTransition("t", "s", "s", "tick", "l", "fixture.loop", "m")]
    return fsm


def storage_counts(hypotheses):
    copied = [h for h in hypotheses if not isinstance(h, _CompactPathHypothesis)]
    nodes = {}
    for hypothesis in hypotheses:
        if isinstance(hypothesis, _CompactPathHypothesis):
            node = hypothesis._history
            while node is not None and id(node) not in nodes:
                nodes[id(node)] = node
                node = node.parent if isinstance(node, _HistoryStep) else None
    prefixes = [node for node in nodes.values() if isinstance(node, _HistoryPrefix)]
    return {
        "hypotheses_retained": len(hypotheses),
        "active_hypotheses": sum(h.status == "active" for h in hypotheses),
        "logical_transition_references": sum(h._path_length() for h in hypotheses),
        "retained_log_slots": (sum(len(h.logs) for h in copied) + len(nodes) - len(prefixes)
                               + sum(len(prefix.log_values) for prefix in prefixes)),
        "retained_transition_tuple_slots": (sum(len(h.transition_ids) for h in copied)
                                            + sum(len(prefix.transition_ids) for prefix in prefixes)),
        "retained_state_tuple_slots": (sum(len(h.state_path) for h in copied)
                                       + sum(len(prefix.state_path) for prefix in prefixes)),
        "history_nodes": len(nodes),
        "history_parent_links": sum(isinstance(n, _HistoryStep) and n.parent is not None for n in nodes.values()),
    }


def measure(mode, events, trace):
    fsm = loop_fsm()
    input_spec = {"fsm": asdict(fsm), "events": events, "message": "tick",
                  "call_id": "l", "index": "0..events-1", "timestamp": None,
                  "new_chains": "first event only", "beam_limits": [1, 1, 1]}
    gc.collect()
    if trace:
        tracemalloc.start()
    started = time.perf_counter()
    cpu_started = time.process_time()
    store = ExplicitHypothesisStore(
        {"loop": fsm}, max_active_chains=1, max_hypotheses_per_chain=1,
        max_total_active_hypotheses=1, compact_history=mode == "compact",
    )
    for index in range(events):
        store.process("tick", index=index, observed_call_node_id="l")
        store.allow_new_chains = False
    cpu_sec = time.process_time() - cpu_started
    wall_sec = time.perf_counter() - started
    rss_peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    current_python, peak_python = tracemalloc.get_traced_memory() if trace else (None, None)
    if trace:
        tracemalloc.stop()

    # Validation and materialization are outside the store processing timer.
    chain = store.active_chains[0]
    hypotheses = list(chain.hypotheses.values())
    last = chain.active_hypotheses[0]
    evidence = {
        "history": [asdict(event) for event in store.history],
        "last_hypothesis": asdict(last),
        "retained_metadata": [
            [h.hypothesis_id, h.current_state, h.status, h.termination_reason, h.score, h._path_length()]
            for h in hypotheses
        ],
    }
    assert len(hypotheses) == events
    assert last.transition_ids == ("t",) * events
    assert [log.index for log in last.logs] == list(range(events))
    return {
        "mode": mode, "events": events, "tracemalloc_enabled": trace,
        "input_sha256": digest(input_spec), "evidence_sha256": digest(evidence),
        "store_process_wall_sec": wall_sec, "store_process_cpu_sec": cpu_sec,
        "process_peak_rss_through_store_bytes": rss_peak,
        "current_python_allocation_bytes": current_python,
        "peak_python_allocation_bytes": peak_python,
        "chains_retained": len(store.active_chains), **storage_counts(hypotheses),
    }


def git_output(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()


def metadata(args):
    return {
        "experiment": "compact_runtime_history", "schema_version": 1,
        "synthetic": True, "baseline_design_commit": BASE_COMMIT,
        "measured_code_commit": git_output("rev-parse", "HEAD"),
        "tracked_worktree_clean": not bool(git_output("status", "--porcelain", "--untracked-files=no")),
        "sources_sha256": {
            path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
            for path in ("src/online/stateful_chain_store.py", "eval/benchmark_compact_history.py",
                         "eval/measure_command.py", "tests/test_compact_history.py")
        },
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "machine": platform.machine(), "python_hash_seed": os.getenv("PYTHONHASHSEED"),
                        "execution": "GitHub Actions job" if os.getenv("GITHUB_ACTIONS") == "true"
                                     else "local shared workspace; other tasks can affect timing"},
        "limits": {"sizes": args.sizes, "timing_repeats": args.repeats,
                   "traced_repeats": 1, "worker_timeout_sec": args.timeout_sec,
                   "worker_max_rss_mib": args.max_rss_mib},
        "measurements": {
            "time": "Store construction and process() calls; no tracemalloc in timing runs. Includes retained-hypothesis scans.",
            "python_memory": "Separate run with tracemalloc. Excludes imports, static FSM, validation and output snapshots.",
            "process_peak_rss": "Linux ru_maxrss through store processing. Includes imports and any tracing overhead. No subtraction.",
            "outer_peak_rss": "Process tree sampled each 50 ms. Includes imports, validation, output and shutdown.",
            "slots": "Stored reference slots, not unique ChainLog objects or total bytes. Compact snapshots are not retained.",
            "equality": "Digest of final full path, all events and retained metadata. Full branch/report equality is in pytest.",
        },
        "runs": [], "summary": [],
    }


def update_summary(report):
    report["summary"] = []
    for events in report["limits"]["sizes"]:
        for mode in ("copied", "compact"):
            rows = [row for row in report["runs"]
                    if row["events"] == events and row["mode"] == mode and row["status"] == "ok"]
            timing = [row for row in rows if not row["tracemalloc_enabled"]]
            traced = [row for row in rows if row["tracemalloc_enabled"]]
            if not timing or not traced:
                continue
            report["summary"].append({
                "events": events, "mode": mode, "timing_repeats": len(timing),
                "median_store_process_wall_sec": statistics.median(row["store_process_wall_sec"] for row in timing),
                "min_store_process_wall_sec": min(row["store_process_wall_sec"] for row in timing),
                "max_store_process_wall_sec": max(row["store_process_wall_sec"] for row in timing),
                "max_process_peak_rss_bytes": max(row["process_peak_rss_through_store_bytes"] for row in timing),
                "peak_python_allocation_bytes": traced[0]["peak_python_allocation_bytes"],
                "retained_log_slots": traced[0]["retained_log_slots"],
                "hypotheses_retained": traced[0]["hypotheses_retained"],
                "history_nodes": traced[0]["history_nodes"],
            })
    failed = any(row["status"] != "ok" for row in report["runs"])
    mismatches = []
    for events in report["limits"]["sizes"]:
        rows = [row for row in report["runs"] if row["events"] == events and row["status"] == "ok"]
        if len({row["input_sha256"] for row in rows}) > 1 or len({row["evidence_sha256"] for row in rows}) > 1:
            mismatches.append(events)
    expected = len(report["limits"]["sizes"]) * 2 * (report["limits"]["timing_repeats"] + 1)
    report["evidence_mismatch_sizes"] = mismatches
    report["complete"] = len(report["runs"]) == expected
    report["status"] = "failed" if failed or mismatches else "ok" if report["complete"] else "running"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sizes", type=int, nargs="+", default=list(SIZES))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout-sec", type=float, default=30)
    parser.add_argument("--max-rss-mib", type=int, default=1024)
    parser.add_argument("--worker", choices=("copied", "compact"), help=argparse.SUPPRESS)
    parser.add_argument("--trace", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if (not args.sizes or len(set(args.sizes)) != len(args.sizes) or
            any(not 1 <= size <= 5000 for size in args.sizes) or
            len(args.sizes) > 4 or not 1 <= args.repeats <= 3 or
            not 0 < args.timeout_sec <= 60 or not 128 <= args.max_rss_mib <= 2048):
        parser.error("Use up to four distinct sizes from 1 to 5000; 1..3 repeats; timeout <=60s; RSS 128..2048 MiB.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.worker:
        if len(args.sizes) != 1:
            parser.error("A worker needs one size.")
        result = measure(args.worker, args.sizes[0], args.trace)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        return 0

    report = metadata(args)
    work_dir = args.output.parent / (args.output.stem + "-runs")
    work_dir.mkdir(parents=True, exist_ok=True)
    for events in args.sizes:
        for repeat in range(args.repeats + 1):
            trace = repeat == args.repeats
            # Alternate order so one mode is not always measured first.
            for mode in (("copied", "compact") if repeat % 2 == 0 else ("compact", "copied")):
                name = f"n{events}-{mode}-{'trace' if trace else repeat}"
                result_path = work_dir / (name + ".json")
                limit_path = work_dir / (name + "-process.json")
                command = [sys.executable, "-m", "eval.benchmark_compact_history", "--worker", mode,
                           "--sizes", str(events), "--output", str(result_path)]
                if trace:
                    command.append("--trace")
                with redirect_stdout(io.StringIO()):
                    run_limited(command, limit_path, args.timeout_sec, args.max_rss_mib * 2**20)
                process = json.loads(limit_path.read_text())
                row = {"events": events, "mode": mode, "repeat": repeat,
                       "tracemalloc_enabled": trace, "status": process["status"],
                       "outer_wall_sec": process["wall_sec"],
                       "outer_peak_rss_bytes": process["aggregate_rss_sampled_peak_bytes"],
                       "returncode": process["returncode"]}
                if row["status"] == "ok":
                    row.update(json.loads(result_path.read_text()))
                report["runs"].append(row)
                update_summary(report)
                args.output.write_text(json.dumps(report, indent=2) + "\n")
                print(f"N={events} mode={mode} trace={trace} status={row['status']}", flush=True)
    print(json.dumps({"status": report["status"], "summary": report["summary"]}, indent=2))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
