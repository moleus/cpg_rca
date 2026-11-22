import pytest

from src.offline.joern_dot import UnsupportedJoernDot, parse_joern_dot


def test_projection_keeps_nodes_and_relevant_edges():
    source = '''digraph {
      "1" [label="CALL" CODE="one"];
      "2" [label="LITERAL" CODE="two"];
      "3" [label="TYPE" NAME="unused but retained"];
      "1" -> "2" [label="ARGUMENT"];
      "1" -> "2" [label="AST"];
      "1" -> "2" [label="CFG"];
      "2" -> "1" [label="REACHING_DEF" VARIABLE="x"];
    }'''
    full = parse_joern_dot(source)
    projected = parse_joern_dot(source, keep_edge_labels=frozenset({'AST', 'CFG', 'REACHING_DEF'}))
    assert list(full.nodes(data=True)) == list(projected.nodes(data=True))
    assert [(u, v, d) for u, v, d in full.edges(data=True) if d['label'] != '"ARGUMENT"'] == list(projected.edges(data=True))
    assert projected.graph['edge_projection']['discarded_edges'] == 1
    assert projected.graph['edge_projection']['input_edges'] == 4
    assert full.graph == {}


@pytest.mark.parametrize('source', [
    'digraph { "1" []; "1" -> "2" [label="IGNORED"]; }',
    'digraph { "1" []; "2" []; "1" -> "2" [label="IGNORED" BAD=unquoted]; }',
])
def test_projection_still_rejects_invalid_discarded_edges(source):
    with pytest.raises(UnsupportedJoernDot):
        parse_joern_dot(source, keep_edge_labels=frozenset({'AST'}))


def test_projection_keeps_entity_encoded_edge_labels_used_by_consumers():
    from src.offline.log import _ast_children

    source = 'digraph { "1" []; "2" []; "1" -> "2" [label="&#65;ST"]; }'
    full = parse_joern_dot(source)
    projected = parse_joern_dot(source, keep_edge_labels=frozenset({'AST'}))
    assert _ast_children(full, '1') == _ast_children(projected, '1') == ['2']
