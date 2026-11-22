"""Index reads must preserve evidence and the existing mutable public views."""

from copy import copy, deepcopy
from dataclasses import asdict, fields, replace

import pandas as pd
import pytest

from eval.benchmark_compact_history import loop_fsm
from src.online.stateful_chain_store import ActiveChain, ExplicitHypothesisStore, PathHypothesis
from src.online.stream_processor import RuntimeStreamProcessor, ServiceCatalog
from test_compact_history import branch_fsm, event, flow_for


def chain_snapshot(chain):
    # Inspect values without exposing live objects through the public API.
    # Calling asdict(chain) here would test scan fallback after the first step.
    return {field.name: (
        {key: asdict(h) for key, h in chain._retained_hypotheses().items()}
        if field.name == "hypotheses" else getattr(chain, field.name)
    ) for field in fields(ActiveChain)}


def index_enabled(chain):
    return chain._usable_index() is not None


def scenario(name):
    fsm = branch_fsm()
    options = {}
    verbs = ["start", "choose", "join", "tick", "tick", "done"]
    events = None
    if name == "chain_beam":
        options["max_hypotheses_per_chain"] = 1
    elif name == "global_beam":
        options["max_total_active_hypotheses"] = 1
    elif name == "chain_limit":
        options.update(max_active_chains=1, require_correlation=True)
        events = [event(0, "start", "a"), event(1, "start", "b"), event(2, "choose", "b")]
    elif name == "expire_missing_key":
        options.update(time_gap_sec=5, require_correlation=True)
        events = [event(0, "start"), event(1, "choose"), event(2, "noise", None, seconds=20)]
    elif name == "correlation":
        options["require_correlation"] = True
        events = [event(0, "start", "a"), event(1, "start", "b"), event(2, "choose", "b"),
                  event(3, "join", "b"), event(4, "done", None), event(5, "done", "b")]
    elif name == "incomplete":
        fsm.states["right"] = replace(fsm.states["right"], kind="INCOMPLETE_SEGMENT", is_terminal=True)
    elif name == "dedup":
        fsm.transitions = [
            replace(t, id="branch") if t.id in {"left", "right"}
            else replace(t, id="join") if t.id in {"left-join", "right-join"} else t
            for t in fsm.transitions
        ]
    return fsm, events or [event(i, verb) for i, verb in enumerate(verbs)], options


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("case", [
    "branch_loop_complete", "chain_beam", "global_beam", "chain_limit",
    "expire_missing_key", "correlation", "incomplete", "dedup",
])
def test_store_and_stream_evidence_stays_equal_while_index_is_live(case, compact):
    fsm, events, options = scenario(case)
    stores = [ExplicitHypothesisStore({"handle": fsm}, compact_history=compact,
                                     index_hypotheses=indexed, **options)
              for indexed in (False, True)]
    processors = [RuntimeStreamProcessor({
        "svc": ServiceCatalog({"handle": fsm}, {"handle": flow_for(fsm)}, store),
    }) for store in stores]
    for item in events:
        assert processors[0].process(item) == processors[1].process(item)
        assert stores[0].history == stores[1].history
        assert [chain_snapshot(c) for c in stores[0].active_chains.values()] == [
            chain_snapshot(c) for c in stores[1].active_chains.values()
        ]
        pd.testing.assert_frame_equal(stores[0].hypotheses_frame(), stores[1].hypotheses_frame())
        pd.testing.assert_frame_equal(stores[0].chains_frame(), stores[1].chains_frame())
        assert all(index_enabled(c) for c in stores[1].active_chains.values())
    assert processors[0].finalize() == processors[1].finalize()
    assert all(index_enabled(c) for c in stores[1].active_chains.values())
    # Check the public dataclass representation after all indexed processing.
    assert [asdict(c) for c in stores[0].active_chains.values()] == [
        asdict(c) for c in stores[1].active_chains.values()
    ]
    assert all(not index_enabled(c) for c in stores[1].active_chains.values())


def started_store(*, indexed=True, compact=True):
    store = ExplicitHypothesisStore({"handle": branch_fsm()}, compact_history=compact,
                                    index_hypotheses=indexed)
    store.process("start x", index=0, observed_call_node_id="l-start")
    store.allow_new_chains = False
    return store, store.active_chains[0]


def test_index_avoids_retained_status_scans(monkeypatch):
    original = PathHypothesis.__getattribute__
    reads = 0

    def count_status(self, name):
        nonlocal reads
        reads += name == "status"
        return original(self, name)

    monkeypatch.setattr(PathHypothesis, "__getattribute__", count_status)
    measured = []
    n = 300
    for indexed in (False, True):
        reads = 0
        store = ExplicitHypothesisStore({"loop": loop_fsm()}, compact_history=True,
                                        index_hypotheses=indexed)
        for i in range(n):
            store.process("tick", index=i, observed_call_node_id="l")
            store.allow_new_chains = False
        measured.append(reads)
        assert len(store.active_chains[0]._retained_hypotheses()) == n
    assert measured[0] >= 2 * n * n
    assert measured[1] < 6 * n


def test_public_live_view_preserves_later_status_mutations():
    _, chain = started_store()
    assert chain.completed_hypotheses == []  # No mutable hypothesis escaped.
    assert chain.frontier == ("open",)
    assert chain.score == 1.
    assert index_enabled(chain)
    hypothesis = chain.active_hypotheses[0]
    assert not index_enabled(chain)
    hypothesis.status = "completed"
    assert chain.active_hypotheses == []
    assert chain.completed_hypotheses == [hypothesis]
    del hypothesis.status  # The dataclass class default is active.
    assert chain.active_hypotheses == [hypothesis]
    assert chain.completed_hypotheses == []
    hypothesis.status = "expired"
    hypothesis.status = "active"
    assert chain.active_hypotheses == [hypothesis]


def test_public_completed_view_preserves_reactivation():
    fsm = branch_fsm()
    fsm.states["open"] = replace(fsm.states["open"], is_terminal=True, kind="RETURN_SEGMENT")
    store = ExplicitHypothesisStore({"handle": fsm}, index_hypotheses=True)
    store.process("start x", observed_call_node_id="l-start")
    chain = store.active_chains[0]
    assert index_enabled(chain)
    hypothesis = chain.completed_hypotheses[0]
    assert not index_enabled(chain)
    hypothesis.status = "active"
    assert chain.active_hypotheses == [hypothesis]


def mutate_mapping(mapping):
    first = next(iter(mapping.values()))
    second = replace(first, hypothesis_id=7, status="completed")
    dict.__setitem__(mapping, 99, second)  # Even a base-class mutation is safe after exposure.
    mapping.update([(8, first)])
    mapping.setdefault(9, second)
    mapping |= {10: first}
    assert mapping.pop(10) is first
    del mapping[8]
    mapping[8] = first
    assert mapping.popitem() == (8, first)
    second.status = "active"


def test_exposed_native_dict_keeps_mutation_and_alias_semantics():
    stores = [started_store(indexed=indexed)[0] for indexed in (False, True)]
    for store in stores:
        chain = store.active_chains[0]
        mapping = chain.hypotheses
        assert type(mapping) is dict
        mutate_mapping(mapping)
    assert chain_snapshot(stores[0].active_chains[0]) == chain_snapshot(stores[1].active_chains[0])
    events = [store.process("choose x", index=1, observed_call_node_id="l-choose") for store in stores]
    assert events[0] == events[1]
    assert chain_snapshot(stores[0].active_chains[0]) == chain_snapshot(stores[1].active_chains[0])
    assert not index_enabled(stores[1].active_chains[0])


def test_direct_mapping_assignment_invalidates_without_prior_exposure():
    stores = [started_store(indexed=indexed, compact=False)[0] for indexed in (False, True)]
    for store in stores:
        chain = store.active_chains[0]
        replacement = {91: PathHypothesis(17, "open", 5., ("external",), ("open",))}
        object.__setattr__(chain, "hypotheses", replacement)
    events = [store.process("choose x", index=1, observed_call_node_id="l-choose") for store in stores]
    assert events[0] == events[1]
    assert chain_snapshot(stores[0].active_chains[0]) == chain_snapshot(stores[1].active_chains[0])
    assert not index_enabled(stores[1].active_chains[0])


def test_shallow_copy_disables_shared_cache_before_mutation():
    _, chain = started_store()
    copied = copy(chain)
    assert copied._hypothesis_index is chain._hypothesis_index
    hypothesis = copied.active_hypotheses[0]
    assert not index_enabled(chain)
    hypothesis.status = "completed"
    assert chain.completed_hypotheses == [hypothesis]
    assert copied.completed_hypotheses == [hypothesis]


def test_deepcopy_store_has_independent_evidence_and_safe_fallback():
    store, chain = started_store()
    copied = deepcopy(store)
    assert index_enabled(chain)
    copied_chain = copied.active_chains[0]
    assert not index_enabled(copied_chain)
    assert index_enabled(chain)
    copied.process("choose x", index=1, observed_call_node_id="l-choose")
    assert len(copied_chain._retained_hypotheses()) == 3
    assert len(chain._retained_hypotheses()) == 1
    store.process("choose x", index=1, observed_call_node_id="l-choose")
    assert chain_snapshot(copied_chain) == chain_snapshot(chain)
    assert copied.history == store.history
    assert index_enabled(chain)


def test_dataclass_replace_and_asdict_preserve_public_values():
    _, chain = started_store()
    cloned = replace(chain, chain_id=10)
    assert not index_enabled(chain)
    assert not index_enabled(cloned)
    hypothesis = cloned.hypotheses[0]
    hypothesis.status = "completed"
    assert chain.completed_hypotheses == [hypothesis]
    left, right = asdict(chain), asdict(cloned)
    left["chain_id"] = 10
    assert left == right


def test_caller_created_chain_and_default_store_use_scanning():
    h = PathHypothesis(0, "q", 1., ("t",), ("p", "q"))
    mapping = {0: h}
    chain = ActiveChain(0, "f", None, None, mapping)
    assert chain.active_hypotheses == [h]
    h.status = "completed"
    assert chain.active_hypotheses == []
    assert chain.completed_hypotheses == [h]
    assert chain.hypotheses is mapping
    _, default = started_store(indexed=False)
    assert "_hypothesis_index" not in default.__dict__
