"""Discarded edges must not change retained neighbor traversal order."""

from dataclasses import asdict

from eval.edge_projection_benchmark import KEEP
from src.offline.joern_dot import parse_joern_dot
from src.offline.log import build_templates_from_cpg
from src.offline.method import MethodConstructor


def retained(records):
    # Edge keys are outside the projection contract. Attribute values stay exact.
    return [(source, target, data) for source, target, data in records
            if data.get("label", "").strip('"').upper() in KEEP]


def test_retained_outgoing_and_incoming_neighbor_order():
    source = '''digraph {
      "1" [label="CALL" CODE="one"];
      "2" [label="CALL" CODE="two"];
      "3" [label="LITERAL" CODE="three"];
      "4" [label="LITERAL" CODE="four"];
      "1" -> "4" [label="ARGUMENT"];
      "2" -> "4" [label="AST"];
      "1" -> "3" [label="CFG"];
      "1" -> "4" [label="CFG"];
      "3" -> "4" [label="REACHING_DEF" VARIABLE="x"];
      "2" -> "4" [label="CDG"];
      "1" -> "2" [label="CALL"];
      "4" -> "1" [label="CONTAINS"];
    }'''
    full = parse_joern_dot(source)
    projected = parse_joern_dot(source, keep_edge_labels=KEEP)

    # The first discarded edge establishes both neighbor orders in the full CPG.
    assert [target for _, target, _ in retained(full.out_edges("1", data=True))] == ["4", "3"]
    assert [origin for origin, _, _ in retained(full.in_edges("4", data=True))] == ["1", "2", "2", "3"]
    assert list(projected.nodes(data=True)) == list(full.nodes(data=True))
    assert list(projected.edges(data=True)) == retained(full.edges(data=True))
    for node in full:
        assert list(projected.out_edges(node, data=True)) == retained(full.out_edges(node, data=True))
        assert list(projected.in_edges(node, data=True)) == retained(full.in_edges(node, data=True))
    for source, target in projected.edges():
        assert projected._succ[source][target] is projected._pred[target][source]


def test_method_graph_keeps_cfg_edge_and_node_order():
    source = '''digraph {
      "1" [label="METHOD" NAME="handle" FULL_NAME="fixture.handle" IS_EXTERNAL="false"];
      "2" [label="CALL" NAME="a" METHOD_FULL_NAME="a" CODE="a()"];
      "3" [label="CALL" NAME="b" METHOD_FULL_NAME="b" CODE="b()"];
      "4" [label="CALL" NAME="c" METHOD_FULL_NAME="c" CODE="c()"];
      "5" [label="METHOD_RETURN" CODE="RET" TYPE_FULL_NAME="void"];
      "1" -> "2" [label="AST"];
      "1" -> "3" [label="AST"];
      "1" -> "4" [label="AST"];
      "1" -> "5" [label="AST"];
      "2" -> "4" [label="ARGUMENT"];
      "2" -> "3" [label="CFG"];
      "2" -> "4" [label="CFG"];
      "3" -> "5" [label="CFG"];
      "4" -> "5" [label="CFG"];
    }'''
    full = MethodConstructor(parse_joern_dot(source)).build("1")
    projected = MethodConstructor(parse_joern_dot(source, keep_edge_labels=KEEP)).build("1")
    assert [target for origin, target, _ in full.cfg_edges if origin == "2"] == ["4", "3"]
    assert projected.cfg_edges == full.cfg_edges
    assert projected.cfg_nodes == full.cfg_nodes
    assert asdict(projected) == asdict(full)


def test_template_order_when_argument_index_is_missing():
    # The current parser supports missing ARGUMENT_INDEX. Equal fallback ranks
    # preserve the AST traversal order when it sorts message arguments.
    source = '''digraph {
      "1" [label="METHOD" NAME="handle" FULL_NAME="fixture.handle" IS_EXTERNAL="false"];
      "2" [label="CALL" NAME="info" METHOD_FULL_NAME="info" CODE="info()"];
      "3" [label="LITERAL" CODE="first"];
      "4" [label="LITERAL" CODE="second"];
      "1" -> "2" [label="AST"];
      "2" -> "3" [label="ARGUMENT"];
      "2" -> "4" [label="AST"];
      "2" -> "3" [label="AST"];
    }'''
    full = build_templates_from_cpg(parse_joern_dot(source))
    projected = build_templates_from_cpg(parse_joern_dot(source, keep_edge_labels=KEEP))
    assert [template.raw_template for template in full] == ["first second <*>"]
    assert [asdict(template) for template in projected] == [asdict(template) for template in full]
