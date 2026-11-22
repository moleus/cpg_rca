"""The experiment must detect output changes and exclude failed samples."""

import json

import networkx as nx

from eval.controlled_benchmark import compare_artifacts, graph_payload, run_order, summarize


def test_artifact_equality_checks_bytes_names_and_array_order(tmp_path):
    expected, actual = tmp_path / "expected", tmp_path / "actual"
    expected.mkdir()
    actual.mkdir()
    payload = json.dumps({"edges": [["a", "b"], ["b", "c"]]})
    (expected / "main.flow.json").write_text(payload)
    (actual / "main.flow.json").write_text(payload)
    assert compare_artifacts(expected, actual)["equal"]

    (actual / "main.flow.json").write_text(json.dumps({"edges": [["b", "c"], ["a", "b"]]}))
    assert compare_artifacts(expected, actual)["changed"] == ["main.flow.json"]
    (actual / "main.flow.json").unlink()
    (actual / "other.flow.json").write_text(payload)
    result = compare_artifacts(expected, actual)
    assert not result["equal"]
    assert result["missing"] == ["main.flow.json"]
    assert result["extra"] == ["other.flow.json"]


def test_graph_equality_retains_parallel_keys_attributes_and_node_order():
    baseline = nx.MultiDiGraph()
    baseline.add_node("a", CODE="first\nsecond")
    baseline.add_node("b", label="CALL")
    baseline.add_edge("a", "b", key=0, label="CFG")
    baseline.add_edge("a", "b", key=1, label="CDG")
    equal = baseline.copy()
    assert graph_payload(baseline) == graph_payload(equal)
    equal.edges["a", "b", 1]["label"] = "CFG"
    assert graph_payload(baseline) != graph_payload(equal)
    reverse = nx.MultiDiGraph()
    reverse.add_nodes_from(reversed(list(baseline.nodes(data=True))))
    reverse.add_edges_from(baseline.edges(keys=True, data=True))
    assert graph_payload(baseline) != graph_payload(reverse)


def test_order_and_summary_do_not_count_failed_samples():
    assert list(run_order(3)) == [(1, "baseline"), (1, "local"), (2, "local"),
                                  (2, "baseline"), (3, "baseline"), (3, "local")]
    rows = [
        {"pair": 1, "variant": "baseline", "status": "ok", "metrics":
         {"total.wall_sec": 12, "flow_and_fsm.wall_sec": 10}},
        {"pair": 1, "variant": "local", "status": "ok", "metrics":
         {"total.wall_sec": 4, "flow_and_fsm.wall_sec": 2}},
        {"pair": 2, "variant": "baseline", "status": "error", "metrics":
         {"total.wall_sec": 600, "flow_and_fsm.wall_sec": 599}},
    ]
    summary = summarize(rows)
    assert summary["baseline"]["total.wall_sec"]["values"] == [12]
    assert summary["local"]["total.wall_sec"]["median"] == 4
    assert summary["paired_ratios"] == [{"pair": 1, "baseline_divided_by_local":
                                         {"total.wall_sec": 3, "flow_and_fsm.wall_sec": 5}}]
