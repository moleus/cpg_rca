"""A condition from one method must not remove branches from another method."""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.online.flow_slicer import slice_flow_for_hypothesis


def branch_method(name, condition):
    return {
        "start_node_id": f"{name}.start",
        "return_node_ids": [],
        "nodes": [
            {"node_id": f"{name}.yes", "call_node_ids": []},
            {"node_id": f"{name}.no", "call_node_ids": []},
        ],
        "edges": [
            {"source_id": f"{name}.start", "target_id": f"{name}.yes", "condition": f"{condition}=true"},
            {"source_id": f"{name}.start", "target_id": f"{name}.no", "condition": f"{condition}=false"},
        ],
    }


def sample_flow():
    return {
        "entrypoint_node_id": "m", "entrypoint_name": "entry", "entrypoint_full_name": "entry",
        "methods": {
            "entry": branch_method("entry", "request_valid"),
            "callee": branch_method("callee", "cache_hit"),
        },
        "external_calls": [],
    }


def segment(method, conditions=(), direct_methods=()):
    return SimpleNamespace(method_full_name=method, conditions=conditions, direct_methods=direct_methods)


def node_ids(result, method):
    return {node["node_id"] for node in result["methods"][method]["nodes"]}


def test_entry_condition_does_not_remove_unobserved_callee_branches():
    flow = sample_flow()
    result = slice_flow_for_hypothesis(
        flow, [segment("entry", ("request_valid=true",), ("callee",))], [],
    )
    assert node_ids(result, "entry") == {"entry.yes"}
    assert node_ids(result, "callee") == {"callee.yes", "callee.no"}
    assert len(result["methods"]["callee"]["edges"]) == 2
    assert len(flow["methods"]["entry"]["nodes"]) == 2
    metadata = result["slice_metadata"]
    assert metadata["observed_conditions"] == ["request_valid=true"]
    assert metadata["candidate_conditions_by_method"] == {"entry": ["request_valid=true"]}
    assert metadata["condition_scope"] == "method_full_name"
    assert metadata["condition_semantics"] == "static_path_candidates"


def test_each_method_uses_only_its_own_condition_candidates():
    result = slice_flow_for_hypothesis(sample_flow(), [
        segment("entry", ("request_valid=true",), ("callee",)),
        segment("callee", ("cache_hit=false",)),
    ], [])
    assert node_ids(result, "entry") == {"entry.yes"}
    assert node_ids(result, "callee") == {"callee.no"}


def test_without_condition_candidates_all_branches_stay():
    result = slice_flow_for_hypothesis(sample_flow(), [segment("entry", direct_methods=("callee",))], [])
    assert node_ids(result, "entry") == {"entry.yes", "entry.no"}
    assert node_ids(result, "callee") == {"callee.yes", "callee.no"}


def test_conditions_without_a_method_name_do_not_filter_any_method():
    old_segment = SimpleNamespace(conditions=("request_valid=true",), direct_methods=("callee",))
    result = slice_flow_for_hypothesis(sample_flow(), [old_segment], [])
    assert node_ids(result, "entry") == {"entry.yes", "entry.no"}
    assert node_ids(result, "callee") == {"callee.yes", "callee.no"}
    assert result["slice_metadata"]["candidate_conditions_by_method"] == {}


def test_segment_generator_has_the_same_result_as_a_list():
    segments = [segment("entry", ("request_valid=true",), ("callee",))]
    expected = slice_flow_for_hypothesis(sample_flow(), segments, [])
    actual = slice_flow_for_hypothesis(sample_flow(), (item for item in segments), iter(()))
    assert actual == expected
