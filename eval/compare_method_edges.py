"""Check exact output and measure local method edge scans against a git revision.

The verified strict DOT reader is supplied from a separate checkout. Both
implementations use the same loaded graph. No method graph cache is used.
Run timing repetitions in separate processes. Peak RSS includes graph loading.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time
import types

import builder
from src.offline.entrypoint import EntrypointDetector
from src.offline.finite_state_machine import LogFlowExtractor
from src.offline import flow, log


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=builder._json_default)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dot", type=Path, action="append", required=True)
    parser.add_argument("--loader-root", type=Path, required=True)
    parser.add_argument("--baseline-ref", default="f8bb418fc3eeb514f4b1dfc1fdce8040470fe3e8")
    parser.add_argument("--entrypoint", action="append")
    parser.add_argument("--implementation", choices=("verify", "baseline", "local"), default="verify")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    code_root = Path(__file__).resolve().parents[1]
    source = subprocess.check_output(
        ["git", "show", f"{args.baseline_ref}:src/offline/method.py"], cwd=code_root,
    )
    baseline_module = types.ModuleType("method_baseline")
    sys.modules[baseline_module.__name__] = baseline_module
    exec(compile(source, f"{args.baseline_ref}:src/offline/method.py", "exec"), baseline_module.__dict__)
    local_constructor = flow.MethodConstructor
    loader_path = args.loader_root / "src/offline/joern_dot.py"
    spec = importlib.util.spec_from_file_location("strict_dot_reader", loader_path)
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    report = {
        "experiment": "local_method_edge_scan", "implementation": args.implementation,
        "baseline_commit": args.baseline_ref,
        "local_method_sha256": hashlib.sha256((code_root / "src/offline/method.py").read_bytes()).hexdigest(),
        "loader_sha256": hashlib.sha256(loader_path.read_bytes()).hexdigest(),
        "python_hash_seed": os.environ.get("PYTHONHASHSEED", "random"),
        "flow_max_depth": 5, "services": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    for dot in args.dot:
        start = time.perf_counter()
        raw = dot.read_text(encoding="utf-8")
        graph = loader.parse_joern_dot(raw)
        load_sec = time.perf_counter() - start
        input_hash = digest(raw)
        del raw
        templates = log.build_templates_from_cpg(graph)
        extractor = LogFlowExtractor(templates)
        detected = EntrypointDetector(graph).detect()
        entrypoints = [entry.node_id for entry in detected]
        if args.entrypoint:
            missing = set(args.entrypoint) - set(entrypoints)
            if missing:
                raise ValueError(f"Requested entrypoints not detected: {sorted(missing)}")
            entrypoints = args.entrypoint
        implementations = ["baseline", "local"] if args.implementation == "verify" else [args.implementation]
        analyzers = {}
        for kind in implementations:
            flow.MethodConstructor = baseline_module.MethodConstructor if kind == "baseline" else local_constructor
            analyzers[kind] = flow.EntrypointFlow(graph, max_depth=5)
        flow.MethodConstructor = local_constructor
        row = {
            "service": dot.parent.name, "input_sha256": input_hash,
            "nodes": graph.number_of_nodes(), "edges": graph.number_of_edges(),
            "entrypoints": len(entrypoints), "load_sec": load_sec, "results": [],
            "timing": {kind: {"flow_sec": 0.0, "fsm_sec": 0.0, "serialize_sec": 0.0}
                       for kind in implementations},
        }
        report["services"].append(row)
        for index, entrypoint in enumerate(entrypoints, 1):
            outputs = {}
            methods = 0
            for kind, analyzer in analyzers.items():
                start = time.perf_counter()
                result = analyzer.build(entrypoint)
                row["timing"][kind]["flow_sec"] += time.perf_counter() - start
                start = time.perf_counter()
                before = canonical(builder.serialize_flow(result))
                method_data = canonical([asdict(entry) for entry in result.sequence])
                row["timing"][kind]["serialize_sec"] += time.perf_counter() - start
                start = time.perf_counter()
                fsm = extractor.extract(result)
                row["timing"][kind]["fsm_sec"] += time.perf_counter() - start
                start = time.perf_counter()
                after = canonical(builder.serialize_flow(result))
                if before != after:
                    raise AssertionError(f"FSM extraction mutated FLOW: {entrypoint}")
                outputs[kind] = (before, method_data, canonical(builder.serialize_fsm(fsm)))
                row["timing"][kind]["serialize_sec"] += time.perf_counter() - start
                methods = len(result.sequence)
                del result, fsm
            if args.implementation == "verify" and outputs["baseline"] != outputs["local"]:
                raise AssertionError(f"MethodGraph/FLOW/FSM output changed: {dot.parent.name}/{entrypoint}")
            selected = next(iter(outputs.values()))
            row["results"].append({
                "entrypoint": entrypoint, "method_invocations": methods,
                "flow_sha256": digest(selected[0]), "methods_sha256": digest(selected[1]),
                "fsm_sha256": digest(selected[2]), "equal": args.implementation == "verify",
            })
            if index % 10 == 0 or index == len(entrypoints):
                print(f"{dot.parent.name}: {index}/{len(entrypoints)}", flush=True)
                save()
        if "local" in analyzers:
            order = analyzers["local"]._mc._node_order
            row["node_order_entries"] = len(order)
            row["node_order_owned_bytes"] = sys.getsizeof(order) + sum(sys.getsizeof(value) for value in order.values())
            del order
        if entrypoints:
            del analyzer
        del analyzers, graph
        save()
    report["peak_rss_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    report["status"] = "ok"
    save()
    print(json.dumps({"status": "ok", "services": len(report["services"]),
                      "peak_rss_mib": report["peak_rss_mib"]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
