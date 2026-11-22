"""
stateful_chain_store.py
=======================

Explicit-hypothesis online matcher for StaticLogFSM.

Each runtime chain contains explicit FSM path hypotheses:
    h = (q, p, s)
where q is one current state, p is the ordered transition/event path, and s is
its cumulative score. Ambiguous transitions create separate hypotheses so every
surviving candidate preserves an exact structural path for later FLOW slicing.

Trie integration
----------------
`process()` accepts `observed_call_node_ids`: a set of CPG logger CALL ids
returned by src.online.trie_matcher. When present, FSM transitions are matched
exactly by CALL id. An empty set means that no CALL matched.
Only None permits lexical matching for callers without a Trie catalog.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from itertools import zip_longest
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

from src.log_text import normalize_text, template_tokens as _template_tokens
from src.offline.finite_state_machine import LogTransition, StaticLogFSM


@dataclass(frozen=True)
class TransitionMatch:
    fsm_key: str
    transition_id: str
    source_state: str
    target_state: str
    log_call_node_id: str
    template: str
    score: float


@dataclass(frozen=True)
class ChainLog:
    index: int
    timestamp: Optional[pd.Timestamp]
    bucket: str
    message: str
    transition_id: str
    template: str
    source_state: str
    target_state: str
    score: float
    correlation_key: Optional[str] = None


@dataclass
class PathHypothesis:
    """One explicit FSM path hypothesis with an auditable transition history."""

    hypothesis_id: int
    current_state: str
    score: float
    transition_ids: Tuple[str, ...]
    state_path: Tuple[str, ...]
    logs: List[ChainLog] = field(default_factory=list)
    status: str = "active"  # active | completed | incomplete | expired | pruned | superseded
    termination_reason: str = ""

    def _path_length(self) -> int:
        return len(self.transition_ids)

    def _path_key(self):
        return self.transition_ids


@dataclass(frozen=True, slots=True, eq=False)
class _HistoryPrefix:
    """A field snapshot made by dataclasses.replace or explicit construction.

    Path fields can change independently. Do not derive transition_ids or
    state_path from logs here. Normal store appends do not make this snapshot.
    """

    transition_ids: Tuple[str, ...]
    state_path: Tuple[str, ...]
    log_values: Tuple[ChainLog, ...]
    path_hash: int = field(init=False)

    def __post_init__(self) -> None:
        value = 0
        for transition_id in self.transition_ids:
            value = hash((value, transition_id))
        object.__setattr__(self, "path_hash", value)

    @property
    def length(self) -> int:
        return len(self.transition_ids)

    def logs(self) -> List[ChainLog]:
        return list(self.log_values)


@dataclass(frozen=True, slots=True, eq=False)
class _HistoryStep:
    """One immutable event and a shared prefix. Never hold a mutable hypothesis."""

    parent: Optional[_HistoryStep | _HistoryPrefix]
    log: ChainLog
    length: int
    path_hash: int

    @classmethod
    def append(cls, parent: Optional[_HistoryStep | _HistoryPrefix], log: ChainLog) -> _HistoryStep:
        return cls(
            parent, log, (parent.length if parent else 0) + 1,
            hash((parent.path_hash if parent else 0, log.transition_id)),
        )

    def logs(self) -> List[ChainLog]:
        result = []
        step = self
        while isinstance(step, _HistoryStep):
            result.append(step.log)
            step = step.parent
        if step is not None:
            prefix_logs = step.logs()
            prefix_logs.extend(reversed(result))
            return prefix_logs
        result.reverse()
        return result

    def __deepcopy__(self, memo):
        # Copy the chain from its first uncopied prefix. Recursion depth must
        # not depend on history length. memo also preserves branch sharing
        # when a complete store or several hypotheses are copied together.
        pending = []
        step = self
        while isinstance(step, _HistoryStep) and id(step) not in memo:
            pending.append(step)
            step = step.parent
        parent = deepcopy(step, memo)
        for source in reversed(pending):
            parent = _HistoryStep(parent, deepcopy(source.log, memo), source.length, source.path_hash)
            memo[id(source)] = parent
        return memo[id(self)]


def _history_parts(tail):
    steps = []
    while isinstance(tail, _HistoryStep):
        steps.append(tail)
        tail = tail.parent
    steps.reverse()
    return tail, steps


def _reverse_transition_ids(tail):
    while isinstance(tail, _HistoryStep):
        yield tail.log.transition_id
        tail = tail.parent
    if tail is not None:
        yield from reversed(tail.transition_ids)


class _TransitionPathKey:
    """Hash a shared transition path in O(1); compare collisions exactly."""

    __slots__ = ("tail",)

    def __init__(self, tail: _HistoryStep | _HistoryPrefix) -> None:
        self.tail = tail

    def __hash__(self) -> int:
        return self.tail.path_hash

    def __eq__(self, other) -> bool:
        if not isinstance(other, _TransitionPathKey):
            return NotImplemented
        left, right = self.tail, other.tail
        if left.length != right.length or left.path_hash != right.path_hash:
            return False
        while left is not right:
            if isinstance(left, _HistoryPrefix) or isinstance(right, _HistoryPrefix):
                missing = object()
                return all(a == b for a, b in zip_longest(
                    _reverse_transition_ids(left), _reverse_transition_ids(right), fillvalue=missing,
                ))
            if left is None or right is None or left.log.transition_id != right.log.transition_id:
                return False
            left, right = left.parent, right.parent
        return True


class _CompactPathHypothesis(PathHypothesis):
    """Read-compatible hypothesis with tuple/list snapshots of shared history.

    The inherited dataclass fields keep asdict() and existing reports unchanged.
    Reading logs returns a new list. Editing this list does not edit the history.
    """

    def __init__(
        self, hypothesis_id: int, current_state: str, score: float,
        transition_ids: Optional[Tuple[str, ...]] = None,
        state_path: Optional[Tuple[str, ...]] = None,
        logs: Optional[List[ChainLog]] = None,
        status: str = "active", termination_reason: str = "", *,
        history: Optional[_HistoryStep | _HistoryPrefix] = None,
    ) -> None:
        if history is None:
            if transition_ids is None or state_path is None:
                raise TypeError("Supply transition_ids and state_path, or a shared history.")
            history = _HistoryPrefix(tuple(transition_ids), tuple(state_path), tuple(logs or ()))
        elif transition_ids is not None or state_path is not None or logs is not None:
            raise TypeError("Supply public path fields or a shared history, not both.")
        self.hypothesis_id = hypothesis_id
        self.current_state = current_state
        self.score = score
        self._history = history
        self.status = status
        self.termination_reason = termination_reason

    @property
    def transition_ids(self) -> Tuple[str, ...]:
        prefix, steps = _history_parts(self._history)
        return (prefix.transition_ids if prefix is not None else ()) + tuple(
            step.log.transition_id for step in steps
        )

    @property
    def state_path(self) -> Tuple[str, ...]:
        prefix, steps = _history_parts(self._history)
        start = prefix.state_path if prefix is not None else (steps[0].log.source_state,)
        return start + tuple(step.log.target_state for step in steps)

    @property
    def logs(self) -> List[ChainLog]:
        return self._history.logs()

    def _path_length(self) -> int:
        return self._history.length

    def _path_key(self) -> _TransitionPathKey:
        return _TransitionPathKey(self._history)


@dataclass
class ActiveChain:
    """Candidate request chain containing multiple explicit path hypotheses."""

    chain_id: int
    fsm_key: str
    created_at: Optional[pd.Timestamp]
    last_timestamp: Optional[pd.Timestamp]
    hypotheses: Dict[int, PathHypothesis] = field(default_factory=dict)
    status: str = "active"
    termination_reason: str = ""
    correlation_key: Optional[str] = None

    @property
    def score(self) -> float:
        return max((h.score for h in self.hypotheses.values()), default=0.0)

    @property
    def active_hypotheses(self) -> List[PathHypothesis]:
        return [h for h in self.hypotheses.values() if h.status == "active"]

    @property
    def completed_hypotheses(self) -> List[PathHypothesis]:
        return [h for h in self.hypotheses.values() if h.status == "completed"]

    @property
    def frontier(self) -> Tuple[str, ...]:
        """Compatibility view of current states for active hypotheses."""
        return tuple(sorted({h.current_state for h in self.active_hypotheses}))


@dataclass(frozen=True)
class HypothesisEvent:
    index: int
    timestamp: Optional[pd.Timestamp]
    bucket: str
    message: str
    chain_id: Optional[int]
    hypothesis_id: Optional[int]
    fsm_key: Optional[str]
    verdict: str  # new_chain | continue | completed | unknown
    source_state: Optional[str]
    target_state: Optional[str]
    transition_id: Optional[str]
    template: Optional[str]
    step_score: float
    cumulative_score: float
    correlation_key: Optional[str] = None
    reason_code: str = ""


ChainEvent = HypothesisEvent


def normalize_correlation_key(value: Any) -> Optional[str]:
    """Keep an explicit request key as text; missing values have no key."""
    if value is None or pd.isna(value):
        return None
    return str(value).strip() or None


def parse_optional_call_id(value: Any) -> Optional[str]:
    """Read an optional CSV/DataFrame CALL cell; blank cells have no ID.

    Direct process() calls keep their exact-ID semantics. This helper is used
    only at tabular input boundaries.
    """
    if value is None or pd.isna(value):
        return None
    return str(value).strip() or None


def template_score(message: str, template: str) -> float:
    """Legacy lexical score used when no exact candidate set was supplied."""
    message_tokens = normalize_text(message).split()
    static_tokens = [token for token in _template_tokens(template) if token != "<*>"]
    if not message_tokens or not static_tokens:
        return 0.0

    position = 0
    matched = 0
    for token in static_tokens:
        while position < len(message_tokens) and message_tokens[position] != token:
            position += 1
        if position == len(message_tokens):
            break
        matched += 1
        position += 1

    if matched == len(static_tokens):
        specificity = len(static_tokens) / max(len(message_tokens), len(static_tokens))
        return 0.90 + 0.10 * specificity

    message_set = set(message_tokens)
    template_set = set(static_tokens)
    union = message_set | template_set
    return len(message_set & template_set) / len(union) if union else 0.0


class ExplicitHypothesisStore:
    """Beam-search store of explicit FSM paths.

    With require_correlation=True, only events with the same nonempty request
    key can continue a chain. Missing keys produce unexplained events.

    Experimental compact_history=True shares immutable history prefixes.
    Public history fields remain tuple/list snapshots. Do not mutate them.
    """

    def __init__(
        self,
        fsms: Dict[str, StaticLogFSM],
        threshold: float = 0.55,
        time_gap_sec: float = 30,
        max_active_chains: int = 512,
        max_hypotheses_per_chain: int = 32,
        max_total_active_hypotheses: int = 2048,
        allow_new_chains: bool = True,
        require_correlation: bool = False,
        compact_history: bool = False,
    ) -> None:
        self.fsms = {key: fsm for key, fsm in fsms.items() if fsm.transitions}
        self.threshold = threshold
        self.time_gap_sec = time_gap_sec
        self.max_active_chains = max_active_chains
        self.max_hypotheses_per_chain = max_hypotheses_per_chain
        self.max_total_active_hypotheses = max_total_active_hypotheses
        self.allow_new_chains = allow_new_chains
        self.require_correlation = require_correlation
        self._compact_history = compact_history
        self.active_chains: Dict[int, ActiveChain] = {}
        self.history: List[HypothesisEvent] = []
        self._next_chain_id = 0
        self._next_hypothesis_id = 0
        self._outgoing = {key: self._build_outgoing(fsm) for key, fsm in self.fsms.items()}
        self._starts = {
            key: [transition for state_id in fsm.start_states for transition in self._outgoing[key].get(state_id, [])]
            for key, fsm in self.fsms.items()
        }

    @staticmethod
    def _build_outgoing(fsm: StaticLogFSM) -> Dict[str, List[LogTransition]]:
        result: Dict[str, List[LogTransition]] = defaultdict(list)
        for transition in fsm.transitions:
            result[transition.source_segment_id].append(transition)
        return dict(result)

    def _new_hypothesis_id(self) -> int:
        value = self._next_hypothesis_id
        self._next_hypothesis_id += 1
        return value

    def _matches(
        self,
        fsm_key: str,
        candidates: Iterable[LogTransition],
        message: str,
        observed_call_node_ids: Optional[set[str]],
    ) -> List[TransitionMatch]:
        result: List[TransitionMatch] = []
        for transition in candidates:
            if observed_call_node_ids is not None and str(transition.log_call_node_id) not in observed_call_node_ids:
                continue
            score = 1.0 if observed_call_node_ids is not None else template_score(message, transition.template)
            if score < self.threshold:
                continue
            result.append(TransitionMatch(
                fsm_key=fsm_key,
                transition_id=transition.id,
                source_state=transition.source_segment_id,
                target_state=transition.target_segment_id,
                log_call_node_id=transition.log_call_node_id,
                template=transition.template,
                score=score,
            ))
        return result

    def _expire(self, timestamp: Optional[pd.Timestamp]) -> None:
        if timestamp is None:
            return
        for chain in self.active_chains.values():
            if chain.status != "active" or chain.last_timestamp is None:
                continue
            if timestamp - chain.last_timestamp <= pd.Timedelta(seconds=self.time_gap_sec):
                continue
            for hypothesis in chain.active_hypotheses:
                hypothesis.status = "expired"
                hypothesis.termination_reason = "timeout"
            chain.status = "expired"
            chain.termination_reason = "timeout"

    def _refresh_chain_status(self, chain: ActiveChain) -> None:
        if chain.active_hypotheses:
            chain.status = "active"
            chain.termination_reason = ""
        elif chain.completed_hypotheses:
            chain.status = "completed"
            chain.termination_reason = "terminal_state"
        elif any(h.status == "incomplete" for h in chain.hypotheses.values()):
            chain.status = "incomplete"
            chain.termination_reason = "incomplete_static_graph"
        elif any(h.status == "pruned" for h in chain.hypotheses.values()):
            chain.status = "pruned"
            chain.termination_reason = "hypothesis_limit"
        elif chain.hypotheses:
            chain.status = "expired"
            chain.termination_reason = "timeout"

    def _enforce_chain_beam(self, chain: ActiveChain) -> None:
        active = chain.active_hypotheses
        if len(active) <= self.max_hypotheses_per_chain:
            return
        active.sort(key=lambda h: (h.score, h._path_length()), reverse=True)
        for hypothesis in active[self.max_hypotheses_per_chain:]:
            hypothesis.status = "pruned"
            hypothesis.termination_reason = "beam_width"
        self._refresh_chain_status(chain)

    def _enforce_global_beam(self) -> None:
        active = [
            (chain, hypothesis)
            for chain in self.active_chains.values()
            if chain.status == "active"
            for hypothesis in chain.active_hypotheses
        ]
        if len(active) <= self.max_total_active_hypotheses:
            return
        active.sort(key=lambda item: (item[1].score, item[0].last_timestamp is not None, item[0].last_timestamp), reverse=True)
        for chain, hypothesis in active[self.max_total_active_hypotheses:]:
            hypothesis.status = "pruned"
            hypothesis.termination_reason = "global_hypothesis_limit"
            self._refresh_chain_status(chain)

    def _enforce_chain_limit(self) -> None:
        chains = [chain for chain in self.active_chains.values() if chain.status == "active"]
        if len(chains) <= self.max_active_chains:
            return
        chains.sort(key=lambda chain: (chain.score, chain.last_timestamp is not None, chain.last_timestamp), reverse=True)
        for chain in chains[self.max_active_chains:]:
            for hypothesis in chain.active_hypotheses:
                hypothesis.status = "pruned"
                hypothesis.termination_reason = "active_chain_limit"
            chain.status = "pruned"
            chain.termination_reason = "active_chain_limit"

    def _make_child(
        self,
        chain: ActiveChain,
        parent: Optional[PathHypothesis],
        match: TransitionMatch,
        index: int,
        timestamp: Optional[pd.Timestamp],
        bucket: str,
        message: str,
    ) -> PathHypothesis:
        fsm = self.fsms[chain.fsm_key]
        terminal = match.target_state in fsm.terminals
        incomplete = fsm.states[match.target_state].kind == "INCOMPLETE_SEGMENT"
        log = ChainLog(
            index=index,
            timestamp=timestamp,
            bucket=bucket,
            message=message,
            transition_id=match.transition_id,
            template=match.template,
            source_state=match.source_state,
            target_state=match.target_state,
            score=match.score,
            correlation_key=chain.correlation_key,
        )
        if self._compact_history:
            if parent is not None and not isinstance(parent, _CompactPathHypothesis):
                raise TypeError("A compact store requires compact parent histories.")
            return _CompactPathHypothesis(
                hypothesis_id=self._new_hypothesis_id(),
                current_state=match.target_state,
                score=(parent.score if parent else 0.0) + match.score,
                history=_HistoryStep.append(parent._history if parent else None, log),
                status="incomplete" if incomplete else "completed" if terminal else "active",
                termination_reason="incomplete_static_graph" if incomplete else "terminal_state" if terminal else "",
            )
        return PathHypothesis(
            hypothesis_id=self._new_hypothesis_id(),
            current_state=match.target_state,
            score=(parent.score if parent else 0.0) + match.score,
            transition_ids=(parent.transition_ids if parent else ()) + (match.transition_id,),
            state_path=(parent.state_path if parent else (match.source_state,)) + (match.target_state,),
            logs=(parent.logs if parent else []) + [log],
            status="incomplete" if incomplete else "completed" if terminal else "active",
            termination_reason="incomplete_static_graph" if incomplete else "terminal_state" if terminal else "",
        )

    def _advance_chain(
        self,
        chain: ActiveChain,
        index: int,
        timestamp: Optional[pd.Timestamp],
        bucket: str,
        message: str,
        observed_call_node_ids: Optional[set[str]],
    ) -> List[HypothesisEvent]:
        events: List[HypothesisEvent] = []
        children: List[PathHypothesis] = []
        parents_expanded: List[PathHypothesis] = []

        for parent in list(chain.active_hypotheses):
            matches = self._matches(
                chain.fsm_key,
                self._outgoing[chain.fsm_key].get(parent.current_state, []),
                message,
                observed_call_node_ids,
            )
            if not matches:
                continue
            parents_expanded.append(parent)
            for match in matches:
                child = self._make_child(chain, parent, match, index, timestamp, bucket, message)
                children.append(child)
                events.append(HypothesisEvent(
                    index=index, timestamp=timestamp, bucket=bucket, message=message,
                    chain_id=chain.chain_id, hypothesis_id=child.hypothesis_id,
                    fsm_key=chain.fsm_key,
                    verdict="completed" if child.status == "completed" else "continue",
                    source_state=match.source_state, target_state=match.target_state,
                    transition_id=match.transition_id, template=match.template,
                    step_score=match.score, cumulative_score=child.score,
                    correlation_key=chain.correlation_key,
                ))

        if not children:
            return []

        # Explicit histories make every child unique by (state, transition sequence).
        best: Dict[tuple, PathHypothesis] = {}
        for child in children:
            key = (child.current_state, child._path_key())
            if key not in best or child.score > best[key].score:
                best[key] = child
        for child in best.values():
            chain.hypotheses[child.hypothesis_id] = child
        for parent in parents_expanded:
            parent.status = "superseded"
            parent.termination_reason = "expanded"

        chain.last_timestamp = timestamp
        self._enforce_chain_beam(chain)
        self._refresh_chain_status(chain)
        return events

    def _start_chain(
        self,
        fsm_key: str,
        matches: List[TransitionMatch],
        index: int,
        timestamp: Optional[pd.Timestamp],
        bucket: str,
        message: str,
        correlation_key: Optional[str] = None,
    ) -> tuple[ActiveChain, List[HypothesisEvent]]:
        chain = ActiveChain(
            chain_id=self._next_chain_id,
            fsm_key=fsm_key,
            created_at=timestamp,
            last_timestamp=timestamp,
            correlation_key=correlation_key,
        )
        self._next_chain_id += 1
        events: List[HypothesisEvent] = []
        for match in matches:
            hypothesis = self._make_child(chain, None, match, index, timestamp, bucket, message)
            chain.hypotheses[hypothesis.hypothesis_id] = hypothesis
            events.append(HypothesisEvent(
                index=index, timestamp=timestamp, bucket=bucket, message=message,
                chain_id=chain.chain_id, hypothesis_id=hypothesis.hypothesis_id,
                fsm_key=fsm_key,
                verdict="completed" if hypothesis.status == "completed" else "new_chain",
                source_state=match.source_state, target_state=match.target_state,
                transition_id=match.transition_id, template=match.template,
                step_score=match.score, cumulative_score=hypothesis.score,
                correlation_key=chain.correlation_key,
            ))
        self._enforce_chain_beam(chain)
        self._refresh_chain_status(chain)
        self.active_chains[chain.chain_id] = chain
        return chain, events

    def process(
        self,
        message: str,
        timestamp: Optional[pd.Timestamp] = None,
        bucket: str = "",
        index: int = -1,
        observed_call_node_id: Optional[str] = None,
        observed_call_node_ids: Optional[set[str]] = None,
        correlation_key: Optional[str] = None,
    ) -> List[HypothesisEvent]:
        """Process exact candidates; only None enables legacy lexical matching."""
        candidates = (
            None if observed_call_node_ids is None
            else {str(value) for value in observed_call_node_ids}
        )
        if observed_call_node_id is not None:
            if candidates is None:
                candidates = set()
            candidates.add(str(observed_call_node_id))

        self._expire(timestamp)
        correlation_key = normalize_correlation_key(correlation_key) if self.require_correlation else None
        if self.require_correlation and correlation_key is None:
            event = HypothesisEvent(
                index=index, timestamp=timestamp, bucket=bucket, message=message,
                chain_id=None, hypothesis_id=None, fsm_key=None, verdict="unknown",
                source_state=None, target_state=None, transition_id=None, template=None,
                step_score=0.0, cumulative_score=0.0, reason_code="missing_correlation_key",
            )
            self.history.append(event)
            return [event]

        events: List[HypothesisEvent] = []
        for chain in list(self.active_chains.values()):
            if chain.status == "active" and (
                not self.require_correlation or chain.correlation_key == correlation_key
            ):
                events.extend(self._advance_chain(chain, index, timestamp, bucket, message, candidates))

        if self.allow_new_chains:
            for fsm_key, start_transitions in self._starts.items():
                matches = self._matches(fsm_key, start_transitions, message, candidates)
                if matches:
                    _, started = self._start_chain(
                        fsm_key, matches, index, timestamp, bucket, message, correlation_key
                    )
                    events.extend(started)

        if not events:
            events.append(HypothesisEvent(
                index=index, timestamp=timestamp, bucket=bucket, message=message,
                chain_id=None, hypothesis_id=None, fsm_key=None, verdict="unknown",
                source_state=None, target_state=None, transition_id=None, template=None,
                step_score=0.0, cumulative_score=0.0,
                correlation_key=correlation_key,
            ))

        self.history.extend(events)
        self._enforce_global_beam()
        self._enforce_chain_limit()
        return events

    def process_frame(
        self,
        logs: pd.DataFrame,
        timestamp_column: str = "timestamp",
        message_column: str = "message",
        bucket_column: str = "bucket",
        call_node_column: Optional[str] = None,
        correlation_column: Optional[str] = None,
    ) -> pd.DataFrame:
        """Compatibility batch API; Trie integration is handled by stream_processor."""
        if correlation_column is not None and not self.require_correlation:
            raise ValueError(
                "Set require_correlation=True when creating the store before passing correlation_column."
            )
        if correlation_column is not None and correlation_column not in logs:
            raise ValueError(f"Missing correlation column: {correlation_column}")
        rows: List[Dict[str, Any]] = []
        for index, row in logs.sort_values(timestamp_column).reset_index(drop=True).iterrows():
            timestamp = row.get(timestamp_column)
            timestamp = timestamp if pd.notna(timestamp) else None
            call_id = parse_optional_call_id(row.get(call_node_column)) if call_node_column is not None else None
            for event in self.process(
                message=str(row.get(message_column, "")), timestamp=timestamp,
                bucket=str(row.get(bucket_column, "")), index=index,
                observed_call_node_id=call_id,
                correlation_key=row.get(correlation_column) if correlation_column is not None else None,
            ):
                rows.append({
                    "log_index": event.index, "timestamp": event.timestamp,
                    "bucket": event.bucket, "message": event.message,
                    "chain_id": event.chain_id, "hypothesis_id": event.hypothesis_id,
                    "entrypoint": event.fsm_key, "verdict": event.verdict,
                    "source_state": event.source_state, "target_state": event.target_state,
                    "transition_id": event.transition_id, "template": event.template,
                    "step_score": event.step_score, "score": event.cumulative_score,
                    "correlation_key": event.correlation_key, "reason_code": event.reason_code,
                })
        return pd.DataFrame(rows)

    def hypotheses_frame(self) -> pd.DataFrame:
        rows: List[Dict[str, Any]] = []
        for chain in self.active_chains.values():
            for hypothesis in chain.hypotheses.values():
                rows.append({
                    "chain_id": chain.chain_id, "entrypoint": chain.fsm_key,
                    "chain_status": chain.status, "hypothesis_id": hypothesis.hypothesis_id,
                    "status": hypothesis.status, "termination_reason": hypothesis.termination_reason,
                    "current_state": hypothesis.current_state, "score": hypothesis.score,
                    "transition_count": len(hypothesis.transition_ids),
                    "transition_ids": " | ".join(hypothesis.transition_ids),
                    "state_path": " | ".join(hypothesis.state_path),
                    "log_count": len(hypothesis.logs),
                })
        return pd.DataFrame(rows).sort_values(["chain_id", "score"], ascending=[True, False]) if rows else pd.DataFrame()

    def chains_frame(self) -> pd.DataFrame:
        rows: List[Dict[str, Any]] = []
        for chain in self.active_chains.values():
            rows.append({
                "chain_id": chain.chain_id, "entrypoint": chain.fsm_key,
                "status": chain.status, "termination_reason": chain.termination_reason,
                "created_at": chain.created_at, "last_timestamp": chain.last_timestamp,
                "score": chain.score, "hypotheses_total": len(chain.hypotheses),
                "hypotheses_active": len(chain.active_hypotheses),
                "hypotheses_completed": len(chain.completed_hypotheses),
                "frontier_size": len(chain.frontier), "frontier": " | ".join(chain.frontier),
            })
        return pd.DataFrame(rows).sort_values(["status", "score"], ascending=[True, False]) if rows else pd.DataFrame()


ChainStore = ExplicitHypothesisStore


def classify_logs_with_chain_store(
    logs: pd.DataFrame,
    fsms: Dict[str, StaticLogFSM],
    threshold: float = 0.55,
    time_gap_sec: float = 30,
    max_active_chains: int = 512,
    max_hypotheses_per_chain: int = 32,
    max_total_active_hypotheses: int = 2048,
    call_node_column: Optional[str] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, ExplicitHypothesisStore]:
    store = ExplicitHypothesisStore(
        fsms, threshold, time_gap_sec, max_active_chains,
        max_hypotheses_per_chain, max_total_active_hypotheses,
    )
    return store.process_frame(logs, call_node_column=call_node_column), store.chains_frame(), store


classifylogswithchainstore = classify_logs_with_chain_store


def extract_logs(dataset_path: str, service: str, start_time: int, end_time: int) -> List[Tuple[pd.Timestamp, str, str]]:
    """Read timestamp, service, message CSV rows for one service and time window."""
    logs: List[Tuple[pd.Timestamp, str, str]] = []
    try:
        with open(dataset_path, "r", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            next(reader, None)
            for row in reader:
                if len(row) < 3:
                    continue
                try:
                    seconds = int(float(row[0]))
                except (ValueError, IndexError):
                    continue
                if row[1].strip().lower() == service.lower() and start_time <= seconds <= end_time:
                    timestamp = pd.to_datetime(seconds, unit="s", utc=True)
                    logs.append((timestamp, f"{row[1].strip()}@{seconds}", row[2].strip()))
    except FileNotFoundError:
        print(f"ERROR: Dataset file not found: {dataset_path}")
    return logs


def logs_to_df(logs: List[Tuple[pd.Timestamp, str, str]]) -> pd.DataFrame:
    return pd.DataFrame(logs, columns=["timestamp", "bucket", "message"])
