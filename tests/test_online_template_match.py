"""The online matcher must use the token policy of builder.py artifacts."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

import networkx as nx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.offline import log as L
from src.online.stateful_chain_store import template_score
from src.online.trie_matcher import ServiceTrieMatcher


def write_matcher(tmp_path, templates, *, with_tokens=True):
    rows = [asdict(template) for template in templates]
    if not with_tokens:
        for row in rows:
            row.pop("tokens")
    path = tmp_path / "trie.json"
    path.write_text(json.dumps({"templates": rows}), encoding="utf-8")
    return ServiceTrieMatcher(path)


def template(text, node_id="log"):
    tokens = text.split()
    return L.LogTemplateWithMethod(
        node_id, "run", "method", text, tokens,
        sum(token != L.WILDCARD for token in tokens),
    )


def test_real_checkoutservice_cpg_matches_online_and_offline(tmp_path):
    """Log nodes are from checkoutservice/export.dot, lines 4333 and 14038.

    This is the PlaceOrder example in PR #7. The log values are test data.
    """
    graph = nx.MultiDiGraph()
    graph.add_node("method", label="METHOD", NAME="PlaceOrder", FILENAME="main.go")
    graph.add_node(
        "30064773387", label="CALL", NAME="Infof",
        METHOD_FULL_NAME="github.com/sirupsen/logrus.Logger.Infof",
    )
    graph.add_node(
        "90194318046", label="LITERAL", ARGUMENT_INDEX="1",
        CODE='"[PlaceOrder] user_id=%q user_currency=%q"',
    )
    graph.add_edge("method", "30064773387", label="AST")
    graph.add_edge("30064773387", "90194318046", label="AST")
    templates = L.build_templates_from_cpg(graph)
    assert [row.raw_template for row in templates] == [
        "placeorder user id <*> user currency <*>"
    ]
    message = '[PlaceOrder] user_id="example-user" user_currency="USD"'
    assert L.map_logs([("test", message)], L.build_trie(templates), templates)[0].matched
    matcher = write_matcher(tmp_path, templates)
    assert matcher.match_call_node_ids(message) == {"30064773387"}


@pytest.mark.parametrize("text,message", [
    ("connection-timeout <*>", "connection-timeout"),
    ("user id <*> done <*>", "user_id=42 done"),
    ("literal star <*>", "literal * star"),
    ("path tpart done <*>", r"path\tpart done"),
])
def test_online_uses_offline_token_policy(tmp_path, text, message):
    templates = [template(text)]
    assert L.map_logs([("test", message)], L.build_trie(templates), templates)[0].matched
    assert write_matcher(tmp_path, templates).match_call_node_ids(message) == {"log"}


def test_raw_template_fallback_preserves_wildcards(tmp_path):
    matcher = write_matcher(
        tmp_path, [template("request <*> failed <*>")], with_tokens=False,
    )
    assert matcher.match_call_node_ids("request abc failed") == {"log"}
    assert matcher.match_call_node_ids("request failed") == {"log"}
    assert matcher.match_call_node_ids("request abc started") == set()


def test_online_keeps_all_matching_call_sites(tmp_path):
    matcher = write_matcher(tmp_path, [
        template("request <*> failed <*>", "first"),
        template("request <*> failed <*>", "second"),
        template("request <*> started <*>", "other"),
    ])
    assert matcher.match_call_node_ids("request abc failed") == {"first", "second"}
    assert matcher.match_call_node_ids("unrelated message") == set()


def test_lexical_fallback_does_not_count_wildcards_as_static_text():
    assert template_score("request failed", "request <*> failed <*>") == 1.0
    assert template_score("connection-timeout", "connection-timeout <*>") == 1.0
    assert template_score("unrelated message", "request <*> failed <*>") == 0.0
