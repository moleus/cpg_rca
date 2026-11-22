"""Log ownership comes from syntax containment, not data flow."""

import sys
from pathlib import Path

import networkx as nx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.offline.log import _find_parent_method


@pytest.mark.parametrize("ownership_label", ["AST", '"CONTAINS"'])
def test_dataflow_shortcut_does_not_change_log_owner(ownership_label):
    graph = nx.MultiDiGraph()
    graph.add_node("other", label="METHOD", NAME="other")
    graph.add_node("owner", label='"METHOD"', NAME='"handle"')
    graph.add_node("block", label="BLOCK")
    graph.add_node("log", label="CALL")
    graph.add_edge("other", "log", label="REACHING_DEF")
    graph.add_edge("owner", "block", label=ownership_label)
    graph.add_edge("block", "log", label='"AST"')

    assert _find_parent_method(graph, "log") == ("handle", "owner")


def test_missing_containment_does_not_assign_a_dataflow_method():
    graph = nx.MultiDiGraph()
    graph.add_node("other", label="METHOD", NAME="other")
    graph.add_node("log", label="CALL")
    graph.add_edge("other", "log", label="REACHING_DEF")

    assert _find_parent_method(graph, "log") == (None, None)
