"""Method extraction keeps graph order and skips edges of other methods."""

import sys
from pathlib import Path

import networkx as nx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.offline.method import MethodConstructor


def method_graph():
    graph = nx.MultiDiGraph()
    # Source order differs from AST traversal and lexical node order.
    for node_id, label in (
        ("foreign", "CALL"), ("b", "CALL"), ("method", "METHOD"),
        ("a", "CALL"), ("ret", "METHOD_RETURN"),
    ):
        graph.add_node(node_id, label=label, NAME=node_id, FULL_NAME=f"svc.{node_id}")
    for node_id in ("a", "b", "ret"):
        graph.add_edge("method", node_id, label="AST")
    graph.add_edge("b", "ret", label="CFG")
    graph.add_edge("b", "ret", label="CDG")
    graph.add_edge("b", "a", label="REACHING_DEF", VARIABLE="from_b")
    graph.add_edge("method", "a", label="CFG")
    graph.add_edge("a", "b", label="CFG")
    graph.add_edge("a", "b", label="REACHING_DEF", VARIABLE="from_a")
    graph.add_edge("a", "b", label="CDG")
    # These cross the method boundary and must not enter CFG or PDG.
    graph.add_edge("foreign", "a", label="CFG")
    graph.add_edge("foreign", "a", label="REACHING_DEF", VARIABLE="outside")
    graph.add_edge("b", "foreign", label="CFG")
    graph.add_edge("b", "foreign", label="CDG")
    return graph


def test_method_keeps_parallel_edges_and_source_order():
    method = MethodConstructor(method_graph()).build("method")

    assert set(method.cfg_edges) == {
        ("method", "a", ""), ("a", "b", ""), ("b", "ret", ""),
    }
    assert [(edge.src, edge.dst, edge.dep_type, edge.variable) for edge in method.pdg_edges] == [
        ("b", "ret", "CDG", ""),
        ("b", "a", "REACHING_DEF", "from_b"),
        ("a", "b", "REACHING_DEF", "from_a"),
        ("a", "b", "CDG", ""),
    ]
    assert set(method.ast_node_ids) == {"method", "a", "b", "ret"}


def test_method_does_not_scan_all_cpg_edges(monkeypatch):
    graph = method_graph()

    def forbidden_global_scan(*args, **kwargs):
        raise AssertionError("Method extraction scanned every edge of the CPG")

    monkeypatch.setattr(graph, "edges", forbidden_global_scan)
    method = MethodConstructor(graph).build("method")
    assert len(method.cfg_edges) == 3
    assert len(method.pdg_edges) == 4
