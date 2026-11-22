"""A Console.WriteLine literal from the cartservice CPG must be a log template."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from builder import build_log_trie, load_cpg
from src.online.trie_matcher import ServiceTrieMatcher


def test_cartservice_health_literal_matches_online(tmp_path):
    graph = load_cpg(Path(__file__).parent / "fixtures" / "cartservice-health.dot")
    templates, _ = build_log_trie(graph, tmp_path)

    assert [(row.call_node_id, row.raw_template) for row in templates] == [
        ("30064771434", "checking cartservice health <*>"),
    ]
    assert templates[0].method_name == "Check"
    assert templates[0].method_node_id == "107374182443"
    matcher = ServiceTrieMatcher(tmp_path / "trie.json")
    assert matcher.match_call_node_ids("Checking CartService Health") == {"30064771434"}
    assert matcher.match_call_node_ids("Unrelated message") == set()
