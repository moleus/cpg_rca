"""Measure retained history with one active hypothesis in a synthetic loop.

This is a bounded diagnostic, not a runtime performance benchmark. Memory is
peak Python allocation from tracemalloc. Imports and the FSM are outside it.
"""

import argparse
import gc
import hashlib
import json
from pathlib import Path
import time
import tracemalloc

from src.offline.finite_state_machine import ExecutionSegment, LogTransition, StaticLogFSM
from src.online.stateful_chain_store import ExplicitHypothesisStore


def measure(events):
    fsm = StaticLogFSM("m", "loop", "fixture.loop")
    fsm.states = {
        "s": ExecutionSegment("s", "fixture.loop", "fixture.loop", (), (), (),
                              None, None, True, False, "START_SEGMENT"),
    }
    fsm.transitions = [LogTransition("t", "s", "s", "tick", "l", "fixture.loop", "m")]
    gc.collect()
    tracemalloc.start()
    started = time.perf_counter()
    store = ExplicitHypothesisStore(
        {"loop": fsm}, max_active_chains=1, max_hypotheses_per_chain=1,
        max_total_active_hypotheses=1,
    )
    store.process("tick", observed_call_node_id="l")
    store.allow_new_chains = False
    for _ in range(events - 1):
        store.process("tick", observed_call_node_id="l")
    wall = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    hypotheses = [h for chain in store.active_chains.values() for h in chain.hypotheses.values()]
    return {
        "events": events, "chains_retained": len(store.active_chains),
        "active_hypotheses": sum(h.status == "active" for h in hypotheses),
        "hypotheses_retained": len(hypotheses),
        "log_references_retained": sum(len(h.logs) for h in hypotheses),
        "peak_python_allocation_bytes": peak, "wall_sec_with_tracing": wall,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    report = {
        "experiment": "history_growth_at_beam_one", "synthetic": True,
        "store_sha256": hashlib.sha256((root / "src/online/stateful_chain_store.py").read_bytes()).hexdigest(),
        "limits": {"active_chains": 1, "hypotheses_per_chain": 1, "total_active_hypotheses": 1},
        "memory_measure": "tracemalloc peak, not RSS; excludes imports and the static FSM",
        "runs": [measure(n) for n in (100, 500, 1000)],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
