"""Compare a native Joern GraphML export with its DOT export.

This is a compatibility experiment. It does not replace the production loader.
It tests the simple label/quote adapter that a direct NetworkX import needs.
Exit 1 means the adapter is not an exact replacement for the selected input.
"""

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

import networkx as nx

def quoted_attributes(data):
    result = {}
    for key, value in data.items():
        if isinstance(value, bool):
            value = str(value).lower()
        else:
            value = str(value)
        result["label" if key in ("labelV", "labelE") else key] = json.dumps(value, ensure_ascii=False)
    return result


def edge_records(graph):
    return Counter((source, target, tuple(sorted(data.items())))
                   for source, target, data in graph.edges(data=True))


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dot", type=Path, required=True)
    parser.add_argument("--graphml", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference-parser", choices=("pydot", "joern"), default="pydot")
    parser.add_argument("--code-root", type=Path)
    args = parser.parse_args(argv)
    if args.code_root:
        sys.path.insert(0, str(args.code_root.resolve()))
    import builder
    kwargs = {"dot_parser": "joern"} if args.reference_parser == "joern" else {}
    reference = builder.load_cpg(args.dot, **kwargs)
    candidate = nx.read_graphml(args.graphml, force_multigraph=True)
    for _, data in candidate.nodes(data=True):
        converted = quoted_attributes(data)
        data.clear()
        data.update(converted)
    for _, _, data in candidate.edges(data=True):
        converted = quoted_attributes(data)
        data.clear()
        data.update(converted)
    differences = []
    mismatch_count = 0
    for node, data in reference.nodes(data=True):
        other = candidate.nodes.get(node, {})
        if data != other:
            mismatch_count += 1
            if len(differences) < 5:
                differences.append({"node": node, "fields": {
                    key: [data.get(key), other.get(key)]
                    for key in sorted(set(data) | set(other)) if data.get(key) != other.get(key)
                }})
    report = {
        "schema_version": 1, "reference_parser": args.reference_parser,
        "dot_sha256": digest(args.dot), "graphml_sha256": digest(args.graphml),
        "nodes": [len(reference), len(candidate)],
        "edges": [reference.number_of_edges(), candidate.number_of_edges()],
        "node_attr_mismatch_count": mismatch_count,
        "node_attr_mismatch_sample": differences,
        "node_ids_equal": set(reference) == set(candidate),
        "edge_multiset_equal": edge_records(reference) == edge_records(candidate),
        "node_order_equal": list(reference.nodes) == list(candidate.nodes),
        "edge_order_equal": list(reference.edges) == list(candidate.edges),
    }
    report["exact_match"] = (not mismatch_count and report["node_ids_equal"]
                             and report["edge_multiset_equal"] and report["node_order_equal"]
                             and report["edge_order_equal"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if "sample" not in key}, indent=2))
    return 0 if report["exact_match"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
