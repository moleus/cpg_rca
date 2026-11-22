"""Runtime reports preserve the limits of their static evidence."""

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from builder import serialize_fsm
from runtime_analyzer import build_incident_context, load_fsm
from src.offline.finite_state_machine import ExecutionSegment, LogFlowExtractor, LogTransition, StaticLogFSM
from src.offline.flow import EntrypointFlowResult, SemanticFlowGraph, SemanticUnit, SemanticUnitKind
from src.offline.log import LogTemplateWithMethod
from src.online.stateful_chain_store import ExplicitHypothesisStore
from src.online.stream_processor import RuntimeEvent, RuntimeStreamProcessor, ServiceCatalog
from src.online.trie_matcher import ServiceTrieMatcher


def segment(name, *, start=False, terminal=False, kind="BETWEEN_LOGS"):
    return ExecutionSegment(
        name, "svc.handle", "svc.handle", (), (), (), None, None,
        start, terminal, kind,
    )


def one_step_fsm():
    fsm = StaticLogFSM("m", "handle", "svc.handle")
    fsm.states = {
        "q0": segment("q0", start=True),
        "q1": segment("q1", terminal=True, kind="RETURN_SEGMENT"),
    }
    fsm.transitions = [LogTransition("t0", "q0", "q1", "alpha", "l0", "svc.handle", "m")]
    return fsm


def two_step_fsm():
    fsm = one_step_fsm()
    fsm.states["q1"] = segment("q1")
    fsm.states["q2"] = segment("q2", terminal=True, kind="RETURN_SEGMENT")
    fsm.transitions.append(LogTransition("t1", "q1", "q2", "omega", "l1", "svc.handle", "m"))
    return fsm


def processor_for(fsm, *, trie=None, **store_options):
    store = ExplicitHypothesisStore({"handle": fsm}, **store_options)
    return RuntimeStreamProcessor({
        "svc": ServiceCatalog({"handle": fsm}, {"handle": {}}, store, trie),
    }), store


def test_empty_candidates_cannot_start_a_chain():
    store = ExplicitHypothesisStore({"handle": one_step_fsm()})
    events = store.process("alpha", observed_call_node_ids=set())
    assert [event.verdict for event in events] == ["unknown"]
    assert not store.active_chains


def test_empty_candidates_cannot_complete_an_active_chain():
    store = ExplicitHypothesisStore({"handle": two_step_fsm()})
    store.process("alpha", observed_call_node_id="l0")
    events = store.process("omega", observed_call_node_ids=set())
    assert [event.verdict for event in events] == ["unknown"]
    assert store.active_chains[0].status == "active"
    assert store.active_chains[0].active_hypotheses[0].transition_ids == ("t0",)


def test_missing_candidates_keep_the_legacy_lexical_api():
    store = ExplicitHypothesisStore({"handle": one_step_fsm()})
    assert store.process("alpha")[0].verdict == "completed"


@pytest.mark.parametrize("call_id", ["unknown", ""])
def test_supplied_call_id_does_not_enable_lexical_fallback(call_id):
    store = ExplicitHypothesisStore({"handle": one_step_fsm()})
    events = store.process("alpha", observed_call_node_id=call_id)
    assert [event.verdict for event in events] == ["unknown"]


def test_exact_candidates_and_single_call_id_are_combined():
    store = ExplicitHypothesisStore({"handle": one_step_fsm()})
    events = store.process("different text", observed_call_node_ids=set(), observed_call_node_id="l0")
    assert [event.verdict for event in events] == ["completed"]


@pytest.mark.parametrize("templates", [[], [
    {"call_node_id": "other", "raw_template": "other", "tokens": ["other"], "static_count": 1},
]])
def test_trie_miss_stays_unexplained(tmp_path, templates):
    trie_path = tmp_path / "trie.json"
    trie_path.write_text(json.dumps({"templates": templates}))
    processor, store = processor_for(one_step_fsm(), trie=ServiceTrieMatcher(trie_path))
    reports = processor.process(RuntimeEvent(0, None, "svc", "", "alpha"))
    assert [report["report_type"] for report in reports] == ["structurally_unexplained_event"]
    assert reports[0]["trie_candidates"] == []
    assert not store.active_chains


def test_missing_trie_keeps_the_legacy_lexical_stream():
    processor, _ = processor_for(one_step_fsm())
    reports = processor.process(RuntimeEvent(0, None, "svc", "", "alpha"))
    assert [report["report_type"] for report in reports] == ["completed_execution_hypothesis"]


def test_explicit_call_id_has_priority_over_trie(tmp_path):
    trie_path = tmp_path / "trie.json"
    trie_path.write_text(json.dumps({"templates": []}))
    processor, _ = processor_for(one_step_fsm(), trie=ServiceTrieMatcher(trie_path))
    reports = processor.process(RuntimeEvent(0, None, "svc", "", "different text", "l0"))
    assert [report["report_type"] for report in reports] == ["completed_execution_hypothesis"]


@pytest.mark.parametrize("terminal_flag", [False, True])
@pytest.mark.parametrize("steps", [1, 2])
def test_incomplete_json_never_reports_completion(tmp_path, terminal_flag, steps):
    fsm = one_step_fsm() if steps == 1 else two_step_fsm()
    target = f"q{steps}"
    fsm.states[target] = segment(target, terminal=terminal_flag, kind="INCOMPLETE_SEGMENT")
    payload = serialize_fsm(fsm)
    if terminal_flag:
        payload["terminal_states"] = [target]  # An artifact from the old builder.
    fsm_path = tmp_path / "handle.fsm.json"
    fsm_path.write_text(json.dumps(payload))
    loaded = load_fsm(fsm_path)

    assert target not in loaded.terminals
    processor, store = processor_for(loaded, require_correlation=True)
    reports = processor.process(RuntimeEvent(0, None, "svc", "", "alpha", "l0", "request-a"))
    if steps == 2:
        reports += processor.process(RuntimeEvent(1, None, "svc", "", "omega", "l1", "request-a"))
    assert reports == []
    chain = store.active_chains[0]
    assert chain.status == "incomplete"
    assert chain.termination_reason == "incomplete_static_graph"
    assert not chain.completed_hypotheses

    unfinished = processor.finalize()
    assert len(unfinished) == 1
    assert unfinished[0]["status"] == "incomplete"
    assert unfinished[0]["termination_reason"] == "incomplete_static_graph"
    assert unfinished[0]["correlation_key"] == "request-a"
    assert unfinished[0]["transition_ids"] == ["t0", "t1"][:steps]
    assert unfinished[0]["evidence_limits"]["is_root_cause_verdict"] is False


def test_builder_does_not_mark_a_static_dead_end_as_terminal():
    graph = SemanticFlowGraph("m", "svc.handle", "start")
    graph.nodes["log"] = SemanticUnit("log", SemanticUnitKind.CALL, 'log("alpha")', "1", call_node_ids=["l0"])
    graph.nodes["dead"] = SemanticUnit("dead", SemanticUnitKind.CALL, "unresolved()", "2")
    graph.add_edge("start", "log")
    graph.add_edge("log", "dead")
    flow = EntrypointFlowResult("m", "handle", "svc.handle", semantic_graphs={"svc.handle": graph})
    template = LogTemplateWithMethod("l0", "handle", "m", "alpha", ["alpha"], 1)
    fsm = LogFlowExtractor([template]).extract(flow)

    incomplete = [state for state in fsm.states.values() if state.kind == "INCOMPLETE_SEGMENT"]
    assert incomplete
    assert not any(state.is_terminal for state in incomplete)
    assert not ({state.id for state in incomplete} & fsm.terminals)


def test_incident_context_keeps_report_evidence_limits():
    fsm = one_step_fsm()
    fsm.warnings = ["Fixture has a bounded static graph."]
    processor, _ = processor_for(fsm)
    timestamp = pd.Timestamp("2026-01-01T00:00:00Z")
    reports = processor.process(RuntimeEvent(0, timestamp, "svc", "", "alpha", "l0"))
    context = build_incident_context(reports, timestamp, timestamp, timestamp)

    assert context["schema_version"] == "4.0"
    assert context["evidence_limits"]["is_root_cause_verdict"] is False
    limits = context["chains"][0]["evidence_limits"]
    assert limits == reports[0]["evidence_limits"]
    assert limits["score_kind"] == "cumulative_match_score_not_probability"
    assert limits["segment_facts"] == "union_of_possible_static_paths_not_observed_execution"
    assert limits["completion"] == "matched_last_log_with_a_static_return_path_not_observed_return"
    assert limits["fsm_warnings"] == fsm.warnings


def test_finalize_keeps_timed_out_leaf_without_its_superseded_parent():
    fsm = two_step_fsm()
    fsm.states["q2"] = segment("q2")
    fsm.states["q3"] = segment("q3", terminal=True, kind="RETURN_SEGMENT")
    fsm.transitions.append(LogTransition("t2", "q2", "q3", "last", "l2", "svc.handle", "m"))
    processor, store = processor_for(fsm, time_gap_sec=10, require_correlation=True)
    for index, (seconds, message, call_id) in enumerate([(100, "alpha", "l0"), (101, "omega", "l1"), (120, "noise", "unknown")]):
        processor.process(RuntimeEvent(index, pd.to_datetime(seconds, unit="s", utc=True), "svc", "", message, call_id, "request-a"))

    assert [h.status for h in store.active_chains[0].hypotheses.values()] == ["superseded", "expired"]
    reports = processor.finalize()
    assert len(reports) == 1
    assert reports[0]["status"] == "expired"
    assert reports[0]["termination_reason"] == "timeout"
    assert reports[0]["transition_ids"] == ["t0", "t1"]
    assert reports[0]["correlation_key"] == "request-a"


@pytest.mark.parametrize("limit, reason", [
    ("max_hypotheses_per_chain", "beam_width"),
    ("max_total_active_hypotheses", "global_hypothesis_limit"),
    ("max_active_chains", "active_chain_limit"),
])
def test_finalize_keeps_pruned_leaf_with_its_reason(limit, reason):
    fsm = two_step_fsm()
    if limit != "max_active_chains":
        fsm.states["other"] = segment("other")
        fsm.transitions.append(LogTransition("other-start", "q0", "other", "alpha", "l0", "svc.handle", "m"))
    processor, _ = processor_for(fsm, require_correlation=True, **{limit: 1})
    processor.process(RuntimeEvent(0, pd.to_datetime(100, unit="s", utc=True), "svc", "", "alpha", "l0", "request-a"))
    if limit == "max_active_chains":
        processor.process(RuntimeEvent(1, pd.to_datetime(101, unit="s", utc=True), "svc", "", "alpha", "l0", "request-b"))

    reports = processor.finalize()
    assert sorted(report["status"] for report in reports) == ["active", "pruned"]
    pruned = next(report for report in reports if report["status"] == "pruned")
    assert pruned["termination_reason"] == reason
    assert pruned["correlation_key"] == "request-a"


def test_finalize_does_not_repeat_completed_or_superseded_hypotheses():
    processor, store = processor_for(two_step_fsm())
    processor.process(RuntimeEvent(0, None, "svc", "", "alpha", "l0"))
    completed = processor.process(RuntimeEvent(1, None, "svc", "", "omega", "l1"))
    assert len(completed) == 1
    assert [h.status for h in store.active_chains[0].hypotheses.values()] == ["superseded", "completed"]
    assert processor.finalize() == []


def test_finalize_keeps_incomplete_alternative_of_a_completed_chain():
    fsm = one_step_fsm()
    fsm.states["other"] = segment("other", kind="INCOMPLETE_SEGMENT")
    fsm.transitions.append(LogTransition("other-start", "q0", "other", "alpha", "l0", "svc.handle", "m"))
    processor, store = processor_for(fsm)
    completed = processor.process(RuntimeEvent(0, None, "svc", "", "alpha", "l0"))
    assert len(completed) == 1
    assert store.active_chains[0].status == "completed"
    reports = processor.finalize()
    assert [report["status"] for report in reports] == ["incomplete"]
    assert reports[0]["termination_reason"] == "incomplete_static_graph"


@pytest.mark.parametrize("emit_incomplete", [False, True])
def test_cli_keeps_expired_hypotheses_when_requested(tmp_path, emit_incomplete):
    service = tmp_path / "artifacts" / "svc"
    service.mkdir(parents=True)
    (service / "handle.fsm.json").write_text(json.dumps(serialize_fsm(two_step_fsm())))
    (service / "handle.flow.json").write_text("{}")
    (service / "trie.json").write_text(json.dumps({"templates": [
        {"call_node_id": "l0", "raw_template": "alpha", "tokens": ["alpha"], "static_count": 1},
        {"call_node_id": "l1", "raw_template": "omega", "tokens": ["omega"], "static_count": 1},
    ]}))
    logs = tmp_path / "logs.csv"
    logs.write_text("timestamp,service,message,request_id\n100,svc,alpha,a\n120,svc,noise,a\n")
    output = tmp_path / "incident.json"
    command = [
        sys.executable, str(Path(__file__).resolve().parents[1] / "runtime_analyzer.py"),
        "--artifacts-dir", str(service.parent), "--logs", str(logs), "--output", str(output),
        "--inject-time", "100", "--time-gap-sec", "10", "--correlation-column", "request_id",
    ]
    if emit_incomplete:
        command.append("--emit-incomplete")
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text())
    assert report["chains"] == []
    assert len(report["unexplained_events"]) == 1
    assert len(report["incomplete_hypotheses"]) == int(emit_incomplete)
    if emit_incomplete:
        unfinished = report["incomplete_hypotheses"][0]
        assert unfinished["status"] == "expired"
        assert unfinished["termination_reason"] == "timeout"
        assert unfinished["correlation_key"] == "a"
