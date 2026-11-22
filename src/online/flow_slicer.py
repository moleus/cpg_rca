"""
flow_slicer.py
==============

Build runtime-constrained slices of builder.py FLOW JSON artifacts.

A completed PathHypothesis supplies an FSM transition/state path. FSM segments
contain candidate methods and condition labels from static analysis. This module
applies those labels only to the method named by each segment. The result is a
FLOW-compatible JSON subgraph for LLM input.

Current precision
-----------------
ExecutionSegment records direct method names and candidate conditions, but not
exact semantic-unit ids traversed inside each interval. A condition label does
not prove a runtime branch decision. The slice retains unlabeled CFG edges and
all branches of methods without condition metadata. Exact node-level slicing
requires more precise evidence.
"""

from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
from typing import Any, Iterable


def build_method_call_index(flow: dict[str, Any]) -> dict[str, set[str]]:
    """Return internal method-call adjacency extracted from FLOW semantic units."""
    adjacency: dict[str, set[str]] = defaultdict(set)
    methods = flow.get("methods", {})
    for method_name, method in methods.items():
        for unit in method.get("nodes", []):
            for callee in unit.get("internal_callee_full_names", []):
                if callee in methods and callee != method_name:
                    adjacency[method_name].add(callee)
    return dict(adjacency)


def expand_methods(
    flow: dict[str, Any],
    roots: Iterable[str],
    max_call_depth: int = 3,
) -> set[str]:
    """Expand selected direct methods through internal calls with cycle protection."""
    methods = flow.get("methods", {})
    adjacency = build_method_call_index(flow)
    selected: set[str] = set()
    queue = deque((method_name, 0) for method_name in roots if method_name in methods)

    while queue:
        method_name, depth = queue.popleft()
        if method_name in selected:
            continue
        selected.add(method_name)
        if depth >= max_call_depth:
            continue
        for callee in adjacency.get(method_name, set()):
            if callee not in selected:
                queue.append((callee, depth + 1))
    return selected


def _segment_conditions(segments: Iterable[Any]) -> set[str]:
    """Return the union for the existing observed_conditions metadata field."""
    conditions: set[str] = set()
    for segment in segments:
        for condition in getattr(segment, "conditions", ()):
            normalized = str(condition).strip()
            if normalized:
                conditions.add(normalized)
    return conditions


def _segment_conditions_by_method(segments: Iterable[Any]) -> dict[str, set[str]]:
    """Keep static condition labels within the method that supplied them."""
    by_method: dict[str, set[str]] = defaultdict(set)
    for segment in segments:
        method_name = str(getattr(segment, "method_full_name", "") or "").strip()
        if method_name:
            by_method[method_name].update(_segment_conditions((segment,)))
    return dict(by_method)


def _segment_methods(segments: Iterable[Any]) -> set[str]:
    methods: set[str] = set()
    for segment in segments:
        methods.update(getattr(segment, "direct_methods", ()) or ())
    return methods


def _logger_call_ids(transitions: Iterable[Any]) -> set[str]:
    return {
        str(getattr(transition, "log_call_node_id", ""))
        for transition in transitions
        if getattr(transition, "log_call_node_id", None)
    }


def _reachable_nodes(method: dict[str, Any], allowed_edges: list[dict[str, Any]]) -> set[str]:
    """Keep only nodes reachable from synthetic start through retained CFG edges."""
    start = method.get("start_node_id")
    if not start:
        return {node["node_id"] for node in method.get("nodes", [])}

    adjacency: dict[str, set[str]] = defaultdict(set)
    for edge in allowed_edges:
        adjacency[str(edge["source_id"])].add(str(edge["target_id"]))

    reachable = {start}
    queue = deque([start])
    while queue:
        source = queue.popleft()
        for target in adjacency.get(source, set()):
            if target not in reachable:
                reachable.add(target)
                queue.append(target)
    return reachable


def slice_method(
    method: dict[str, Any],
    observed_conditions: set[str],
    observed_log_call_ids: set[str],
) -> dict[str, Any]:
    """
    Slice one semantic method graph while preserving structural soundness.

    Unlabeled edges stay because they represent ordinary sequential CFG flow.
    Labeled branch edges stay when their label is a static condition candidate
    for this method. Without such candidates, all branches stay. Logger anchor nodes
    are always retained if their CPG CALL ids occur in matched transitions.
    """
    sliced = deepcopy(method)
    all_edges = list(method.get("edges", []))

    if observed_conditions:
        allowed_edges = [
            edge for edge in all_edges
            if not edge.get("condition") or str(edge.get("condition")) in observed_conditions
        ]
    else:
        # No condition metadata for this method: preserve its CFG.
        allowed_edges = all_edges

    reachable = _reachable_nodes(method, allowed_edges)
    return_ids = set(method.get("return_node_ids", []))
    keep_ids = reachable | return_ids

    for node in method.get("nodes", []):
        call_ids = {str(call_id) for call_id in node.get("call_node_ids", [])}
        if call_ids & observed_log_call_ids:
            keep_ids.add(str(node["node_id"]))

    # Keep only edges whose endpoints are retained. Synthetic start is valid.
    retained_edges = [
        edge for edge in allowed_edges
        if str(edge["source_id"]) in keep_ids | {str(method.get("start_node_id", ""))}
        and str(edge["target_id"]) in keep_ids
    ]
    sliced["nodes"] = [node for node in method.get("nodes", []) if str(node["node_id"]) in keep_ids]
    sliced["edges"] = retained_edges
    sliced["return_node_ids"] = [node_id for node_id in method.get("return_node_ids", []) if node_id in keep_ids]
    return sliced


def slice_flow_for_hypothesis(
    flow: dict[str, Any],
    segments: Iterable[Any],
    transitions: Iterable[Any],
    max_call_depth: int = 3,
) -> dict[str, Any]:
    """
    Produce a FLOW-compatible constrained subgraph for one completed hypothesis.

    The returned object retains builder.py's top-level FLOW schema and adds a
    `slice_metadata` section explaining evidence and approximation level.
    """
    methods = flow.get("methods", {})
    entrypoint_full_name = str(flow.get("entrypoint_full_name", ""))
    segments = list(segments)
    direct_methods = _segment_methods(segments)
    direct_methods.add(entrypoint_full_name)
    observed_conditions = _segment_conditions(segments)
    conditions_by_method = _segment_conditions_by_method(segments)
    observed_log_call_ids = _logger_call_ids(transitions)
    selected_methods = expand_methods(flow, direct_methods, max_call_depth=max_call_depth)

    sliced_methods = {
        method_name: slice_method(
            methods[method_name],
            observed_conditions=conditions_by_method.get(method_name, set()),
            observed_log_call_ids=observed_log_call_ids,
        )
        for method_name in sorted(selected_methods)
        if method_name in methods
    }

    result = {
        "entrypoint_node_id": flow.get("entrypoint_node_id"),
        "entrypoint_name": flow.get("entrypoint_name"),
        "entrypoint_full_name": flow.get("entrypoint_full_name"),
        "methods": sliced_methods,
        "external_calls": [
            call for call in flow.get("external_calls", [])
            if call.get("caller_full_name") in selected_methods
        ],
        "slice_metadata": {
            "scope": "method_condition_approximation",
            "max_call_depth": max_call_depth,
            "direct_methods_from_fsm": sorted(direct_methods),
            "selected_methods": sorted(selected_methods),
            "observed_conditions": sorted(observed_conditions),
            "condition_scope": "method_full_name",
            "condition_semantics": "static_path_candidates",
            "candidate_conditions_by_method": {
                name: sorted(conditions) for name, conditions in sorted(conditions_by_method.items())
            },
            "observed_log_call_node_ids": sorted(observed_log_call_ids),
            "original_method_count": len(methods),
            "retained_method_count": len(sliced_methods),
            "original_node_count": sum(len(method.get("nodes", [])) for method in methods.values()),
            "retained_node_count": sum(len(method.get("nodes", [])) for method in sliced_methods.values()),
            "original_edge_count": sum(len(method.get("edges", [])) for method in methods.values()),
            "retained_edge_count": sum(len(method.get("edges", [])) for method in sliced_methods.values()),
        },
    }
    return result
