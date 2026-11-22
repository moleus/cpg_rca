"""The shared history must preserve the actual store and report outputs."""

from copy import deepcopy
from dataclasses import asdict, fields, replace
from pathlib import Path
import sys

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.offline.finite_state_machine import ExecutionSegment, LogTransition, StaticLogFSM
from src.online.hypothesis_report import build_llm_chain_summary
from src.online.stateful_chain_store import (
    ChainLog, ExplicitHypothesisStore, PathHypothesis, _CompactPathHypothesis,
    _HistoryStep, _TransitionPathKey,
)
from src.online.stream_processor import RuntimeEvent, RuntimeStreamProcessor, ServiceCatalog


def branch_fsm():
    fsm = StaticLogFSM("m", "handle", "fixture.handle")
    fsm.states = {
        name: ExecutionSegment(
            name, "fixture.handle", "fixture.handle", ("fixture.handle",),
            ("remote.call",), ("ready=true",) if name == "left" else (),
            None, None, name == "start", name == "end",
            "RETURN_SEGMENT" if name == "end" else "BETWEEN_LOGS",
        )
        for name in ("start", "open", "left", "right", "join", "end")
    }
    fsm.transitions = [
        LogTransition(name, source, target, text, call, "fixture.handle", "m")
        for name, source, target, text, call in [
            ("start", "start", "open", "start <*>", "l-start"),
            ("left", "open", "left", "choose <*>", "l-choose"),
            ("right", "open", "right", "choose <*>", "l-choose"),
            ("left-join", "left", "join", "join <*>", "l-join"),
            ("right-join", "right", "join", "join <*>", "l-join"),
            ("tick", "join", "join", "tick <*>", "l-tick"),
            ("done", "join", "end", "done <*>", "l-done"),
        ]
    ]
    fsm.warnings = ["Synthetic FSM for storage equivalence."]
    return fsm


def flow_for(fsm):
    return {
        "entrypoint_node_id": "m", "entrypoint_name": "handle",
        "entrypoint_full_name": "fixture.handle", "external_calls": [],
        "methods": {"fixture.handle": {
            "start_node_id": "start", "return_node_ids": ["end"],
            "nodes": [
                {"node_id": name, "call_node_ids": [t.log_call_node_id for t in fsm.outgoing(name)]}
                for name in fsm.states
            ],
            "edges": [
                {"source_id": t.source_segment_id, "target_id": t.target_segment_id,
                 "condition": "ready=true" if t.target_segment_id == "left" else ""}
                for t in fsm.transitions
            ],
        }},
    }


def event(index, verb, key="a", *, seconds=None, call=True):
    return RuntimeEvent(
        index, pd.to_datetime(index if seconds is None else seconds, unit="s", utc=True),
        "svc", f"bucket-{index}", f"{verb} value-{index}",
        f"l-{verb}" if call else None, key,
    )


def compare_stream(fsm, events, **options):
    stores, processors = [], []
    flow = flow_for(fsm)
    for compact in (False, True):
        store = ExplicitHypothesisStore({"handle": fsm}, compact_history=compact, **options)
        stores.append(store)
        processors.append(RuntimeStreamProcessor({
            "svc": ServiceCatalog({"handle": fsm}, {"handle": flow}, store),
        }))

    all_reports = []
    for item in events:
        reports = [processor.process(item) for processor in processors]
        assert reports[0] == reports[1]
        all_reports.extend(reports[1])
        assert stores[0].history == stores[1].history
        assert [asdict(c) for c in stores[0].active_chains.values()] == [
            asdict(c) for c in stores[1].active_chains.values()
        ]
        pd.testing.assert_frame_equal(stores[0].chains_frame(), stores[1].chains_frame())
        pd.testing.assert_frame_equal(stores[0].hypotheses_frame(), stores[1].hypotheses_frame())

    final = [processor.finalize() for processor in processors]
    assert final[0] == final[1]
    summaries = [
        [build_llm_chain_summary("svc", c, h, fsm, flow)
         for c in store.active_chains.values() for h in c.hypotheses.values()]
        for store in stores
    ]
    assert summaries[0] == summaries[1]
    return stores[1], all_reports, final[1]


def test_branch_rejoin_loop_and_completion_keep_all_evidence():
    events = [event(i, verb) for i, verb in enumerate(
        ["start", "choose", "join", "tick", "tick", "done"]
    )]
    store, reports, final = compare_stream(branch_fsm(), events)
    assert not final
    assert len(reports) == 2
    assert all(r["report_type"] == "completed_execution_hypothesis" for r in reports)
    assert {tuple(r["hypothesis"]["transition_ids"]) for r in reports} == {
        ("start", branch, f"{branch}-join", "tick", "tick", "done")
        for branch in ("left", "right")
    }
    chain = store.active_chains[0]
    left, right = [h for h in chain.hypotheses.values() if h.current_state in {"left", "right"}]
    assert left._history.parent is right._history.parent
    assert left.logs[0] is right.logs[0]
    assert all(len(r["runtime_evidence"]) == 6 for r in reports)
    assert all(r["flow_slice"]["methods"] for r in reports)


@pytest.mark.parametrize("option, reason", [
    ("max_hypotheses_per_chain", "beam_width"),
    ("max_total_active_hypotheses", "global_hypothesis_limit"),
])
def test_pruning_keeps_status_reason_and_branch_order(option, reason):
    store, reports, final = compare_stream(
        branch_fsm(), [event(i, verb) for i, verb in enumerate(
            ["start", "choose", "join", "done"]
        )], **{option: 1},
    )
    assert len(reports) == 1
    assert reports[0]["hypothesis"]["transition_ids"] == ["start", "left", "left-join", "done"]
    assert [r["termination_reason"] for r in final] == [reason]
    assert final[0]["transition_ids"] == ["start", "right"]


def test_chain_limit_keeps_pruned_request_separate():
    _, _, final = compare_stream(branch_fsm(), [
        event(0, "start", "a"), event(1, "start", "b"),
    ], require_correlation=True, max_active_chains=1)
    assert {(r["correlation_key"], r["status"]) for r in final} == {("a", "pruned"), ("b", "active")}
    assert final[0]["termination_reason"] == "active_chain_limit"


def test_interleaved_requests_and_missing_keys_do_not_mix_logs():
    _, reports, final = compare_stream(branch_fsm(), [
        event(0, "start", "a"), event(1, "start", "b"),
        event(2, "choose", "b"), event(3, "join", "b"),
        event(4, "done", None), event(5, "done", "b"),
    ], require_correlation=True)
    completed = [r for r in reports if r["report_type"] == "completed_execution_hypothesis"]
    assert len(completed) == 2
    assert all({log["correlation_key"] for log in r["runtime_evidence"]} == {"b"} for r in completed)
    assert reports[0]["classification"]["reason_code"] == "missing_correlation_key"
    assert [r["correlation_key"] for r in final] == ["a"]


def test_missing_key_still_advances_expiration_time():
    store, reports, final = compare_stream(branch_fsm(), [
        event(0, "start"), event(1, "choose"),
        event(2, "noise", None, seconds=20),
    ], require_correlation=True, time_gap_sec=5)
    assert store.active_chains[0].status == "expired"
    assert reports[0]["classification"]["reason_code"] == "missing_correlation_key"
    assert len(final) == 2
    assert {r["termination_reason"] for r in final} == {"timeout"}


def test_incomplete_alternative_is_retained_after_completion():
    fsm = branch_fsm()
    fsm.states["right"] = replace(fsm.states["right"], kind="INCOMPLETE_SEGMENT", is_terminal=True)
    _, reports, final = compare_stream(fsm, [event(i, verb) for i, verb in enumerate(
        ["start", "choose", "join", "done"]
    )])
    assert len(reports) == 1
    assert len(final) == 1
    assert final[0]["status"] == "incomplete"
    assert final[0]["transition_ids"] == ["start", "right"]


def test_dedup_uses_transition_values_not_parent_identity():
    fsm = branch_fsm()
    fsm.transitions = [
        replace(t, id="branch") if t.id in {"left", "right"}
        else replace(t, id="join") if t.id in {"left-join", "right-join"} else t
        for t in fsm.transitions
    ]
    store, reports, _ = compare_stream(fsm, [event(i, verb) for i, verb in enumerate(
        ["start", "choose", "join", "done"]
    )])
    assert len(reports) == 1
    assert len([h for h in store.active_chains[0].hypotheses.values() if h.current_state == "join"]) == 1
    assert reports[0]["hypothesis"]["state_path"] == ["start", "open", "left", "join", "end"]


def test_lexical_score_and_unknown_events_match():
    _, reports, _ = compare_stream(branch_fsm(), [
        event(i, verb, call=False) for i, verb in enumerate(
            ["start", "choose", "join", "unrelated", "done"]
        )
    ])
    assert reports[0]["report_type"] == "structurally_unexplained_event"
    assert len(reports) == 3


def test_empty_candidate_set_does_not_fall_back_in_either_mode():
    for compact in (False, True):
        store = ExplicitHypothesisStore({"handle": branch_fsm()}, compact_history=compact)
        assert store.process("start x", observed_call_node_ids=set())[0].verdict == "unknown"
        assert not store.active_chains
        store.process("start x", observed_call_node_id="l-start")
        assert store.process("choose x", observed_call_node_ids=set())[0].verdict == "unknown"
        assert store.active_chains[0].active_hypotheses[0].transition_ids == ("start",)


def test_hash_collision_cannot_merge_different_transition_paths():
    log = ChainLog(0, None, "", "tick", "one", "tick", "s", "s", 1.)
    first = _HistoryStep(None, log, 1, 7)
    same = _HistoryStep(None, replace(log, message="other evidence"), 1, 7)
    other = _HistoryStep(None, replace(log, transition_id="two"), 1, 7)
    assert _TransitionPathKey(first) == _TransitionPathKey(same)
    assert _TransitionPathKey(first) != _TransitionPathKey(other)
    assert len({_TransitionPathKey(first), _TransitionPathKey(same), _TransitionPathKey(other)}) == 2


def test_long_history_has_iterative_snapshots_and_immutable_prefixes():
    fsm = branch_fsm()
    fsm.transitions = [LogTransition("tick", "start", "start", "tick", "l-tick", "fixture.handle", "m")]
    store = ExplicitHypothesisStore({"handle": fsm}, compact_history=True)
    for index in range(1500):
        store.process("tick", index=index, observed_call_node_id="l-tick")
        store.allow_new_chains = False
    chain = store.active_chains[0]
    first, last = chain.hypotheses[0], chain.hypotheses[1499]
    assert isinstance(last, PathHypothesis)
    assert isinstance(last, _CompactPathHypothesis)
    assert last._path_length() == 1500
    assert last.transition_ids == ("tick",) * 1500
    assert last.state_path == ("start",) * 1501
    assert [log["index"] for log in asdict(last)["logs"]] == list(range(1500))
    saved = first.logs
    saved.clear()
    assert len(first.logs) == 1
    assert first.status == "superseded"
    assert first.logs[0] is last.logs[0]


def test_default_keeps_the_original_history_containers():
    store = ExplicitHypothesisStore({"handle": branch_fsm()})
    store.process("start x", observed_call_node_id="l-start")
    hypothesis = store.active_chains[0].hypotheses[0]
    assert type(hypothesis) is PathHypothesis
    assert type(hypothesis.logs) is list
    assert type(hypothesis.transition_ids) is tuple
    assert hypothesis.logs is hypothesis.logs


def test_replace_long_history_keeps_values_and_metadata_changes():
    log = ChainLog(0, None, "", "tick", "t", "tick", "s", "s", 1.)
    history = None
    for index in range(3000):
        history = _HistoryStep.append(history, replace(log, index=index))
    original = _CompactPathHypothesis(
        hypothesis_id=7, current_state="s", score=3000., history=history,
    )
    changed = replace(original, score=3001., status="pruned", termination_reason="test")
    expected = asdict(original)
    expected.update(score=3001., status="pruned", termination_reason="test")
    assert isinstance(changed, _CompactPathHypothesis)
    assert asdict(changed) == expected
    assert original.score == 3000.
    assert original.status == "active"
    assert len(changed.logs) == 3000
    # A snapshot root and the original linked steps must use the same dedup key.
    assert changed._path_key() == original._path_key()
    assert hash(changed._path_key()) == hash(original._path_key())
    assert len({changed._path_key(), original._path_key()}) == 1
    snapshot = changed.logs
    snapshot.clear()
    assert len(changed.logs) == 3000


@pytest.mark.parametrize("field_name, value", [
    ("transition_ids", ("different",)),
    ("state_path", ("different-source", "different-target")),
    ("logs", []),
])
def test_replace_preserves_independent_path_fields(field_name, value):
    store = ExplicitHypothesisStore({"handle": branch_fsm()}, compact_history=True)
    store.process("start x", index=0, observed_call_node_id="l-start")
    original = store.active_chains[0].hypotheses[0]
    plain = PathHypothesis(**{field.name: getattr(original, field.name) for field in fields(PathHypothesis)})
    expected = replace(plain, **{field_name: value})
    actual = replace(original, **{field_name: value})
    assert asdict(actual) == asdict(expected)
    assert actual._path_length() == len(expected.transition_ids)
    assert original.transition_ids == ("start",)
    assert original.state_path == ("start", "open")
    assert len(original.logs) == 1


def test_replaced_fields_remain_independent_when_store_appends():
    stores = [ExplicitHypothesisStore({"handle": branch_fsm()}, compact_history=compact)
              for compact in (False, True)]
    for store in stores:
        store.process("start x", index=0, observed_call_node_id="l-start")
        store.allow_new_chains = False
        chain = store.active_chains[0]
        parent = chain.active_hypotheses[0]
        chain.hypotheses[parent.hypothesis_id] = replace(
            parent, transition_ids=("replaced", "path"), state_path=(), logs=[],
        )
    events = [store.process("choose x", index=1, observed_call_node_id="l-choose") for store in stores]
    assert events[0] == events[1]
    assert asdict(stores[0].active_chains[0]) == asdict(stores[1].active_chains[0])
    children = stores[1].active_chains[0].active_hypotheses
    assert {child.transition_ids for child in children} == {
        ("replaced", "path", "left"), ("replaced", "path", "right"),
    }
    assert {child.state_path for child in children} == {("left",), ("right",)}
    assert all([log.index for log in child.logs] == [1] for child in children)


def test_deepcopy_long_path_and_whole_store_preserves_shared_prefixes():
    fsm = branch_fsm()
    fsm.transitions = [LogTransition("tick", "start", "start", "tick", "l-tick", "fixture.handle", "m")]
    store = ExplicitHypothesisStore({"handle": fsm}, compact_history=True)
    for index in range(1500):
        store.process("tick", index=index, observed_call_node_id="l-tick")
        store.allow_new_chains = False
    chain = store.active_chains[0]
    last = chain.active_hypotheses[0]

    # Copy the tail first so prefix sharing does not depend on dictionary order.
    copied_path, copied_first = deepcopy((last, chain.hypotheses[0]))
    assert asdict(copied_path) == asdict(last)
    assert copied_path._history is not last._history
    assert copied_path.logs[0] is not last.logs[0]
    copied_root = copied_path._history
    while copied_root.parent is not None:
        copied_root = copied_root.parent
    assert copied_root is copied_first._history

    # Add an equivalent snapshot root. deepcopy must keep shared ChainLog
    # values between this root and the linked histories that remain retained.
    chain.hypotheses[last.hypothesis_id] = replace(last)
    copied = deepcopy(store)
    copied_chain = copied.active_chains[0]
    assert copied.history == store.history
    assert copied.history is not store.history
    assert list(copied_chain.hypotheses) == list(chain.hypotheses)
    assert all((new.hypothesis_id, new.score, new.status, new.termination_reason) ==
               (old.hypothesis_id, old.score, old.status, old.termination_reason)
               for old, new in zip(chain.hypotheses.values(), copied_chain.hypotheses.values()))
    for index in range(1, 1499):
        assert copied_chain.hypotheses[index]._history.parent is copied_chain.hypotheses[index - 1]._history
    assert copied_chain.hypotheses[0]._history is not chain.hypotheses[0]._history
    copied_last = copied_chain.active_hypotheses[0]
    assert copied_last.logs[0] is copied_chain.hypotheses[0].logs[0]
    assert copied_last.logs[0] is not chain.hypotheses[0].logs[0]
    assert asdict(copied_last) == asdict(last)

    copied.process("tick", index=1500, observed_call_node_id="l-tick")
    assert copied_chain.active_hypotheses[0]._path_length() == 1501
    assert chain.active_hypotheses[0]._path_length() == 1500
    assert len(copied.history) == 1501
    assert len(store.history) == 1500
    assert chain.active_hypotheses[0].status == "active"


def test_deepcopy_branch_store_keeps_one_copied_prefix():
    store = ExplicitHypothesisStore({"handle": branch_fsm()}, compact_history=True)
    store.process("start x", index=0, observed_call_node_id="l-start")
    store.process("choose x", index=1, observed_call_node_id="l-choose")
    copied = deepcopy(store)
    left, right = copied.active_chains[0].active_hypotheses
    assert left._history.parent is right._history.parent
    assert left._history.parent is copied.active_chains[0].hypotheses[0]._history
    assert left._history.parent is not store.active_chains[0].hypotheses[0]._history
    assert asdict(copied.active_chains[0]) == asdict(store.active_chains[0])


def test_storage_counts_include_replacement_snapshot_slots():
    from eval.benchmark_compact_history import storage_counts

    log = ChainLog(0, None, "", "tick", "t", "tick", "s", "s", 1.)
    history = None
    for index in range(3):
        history = _HistoryStep.append(history, replace(log, index=index))
    original = _CompactPathHypothesis(hypothesis_id=0, current_state="s", score=3., history=history)
    snapshot = replace(original, hypothesis_id=1)
    child = _CompactPathHypothesis(
        hypothesis_id=2, current_state="s", score=4.,
        history=_HistoryStep.append(snapshot._history, replace(log, index=3)),
    )
    counts = storage_counts([original, snapshot, child])
    assert counts["retained_log_slots"] == 7  # Three linked slots, three snapshot slots, one new slot.
    assert counts["retained_transition_tuple_slots"] == 3
    assert counts["retained_state_tuple_slots"] == 4
    assert counts["history_nodes"] == 5
    assert counts["history_parent_links"] == 3
    changed = replace(original, transition_ids=("one", "two"), logs=[])
    changed_counts = storage_counts([changed])
    assert changed_counts["logical_transition_references"] == 2
    assert changed_counts["retained_log_slots"] == 0
