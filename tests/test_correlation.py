"""Explicit request keys prevent mixed chains in the runtime CLI."""

import csv
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from builder import serialize_fsm
from src.offline.finite_state_machine import ExecutionSegment, LogTransition, StaticLogFSM
from src.online.stateful_chain_store import ExplicitHypothesisStore
from src.online.stream_processor import RuntimeEvent, RuntimeStreamProcessor, ServiceCatalog
from src.online.trie_matcher import ServiceTrieMatcher


def two_step_fsm():
    fsm = StaticLogFSM("m", "handle", "svc.handle")
    fsm.states = {
        name: ExecutionSegment(
            name, "svc.handle", "svc.handle", (), (), (), None, None,
            name == "q0", name == "q2", "RETURN_SEGMENT" if name == "q2" else "BETWEEN_LOGS",
        )
        for name in ("q0", "q1", "q2")
    }
    fsm.transitions = [
        LogTransition("t0", "q0", "q1", "start <*>", "l0", "svc.handle", "m"),
        LogTransition("t1", "q1", "q2", "done <*>", "l1", "svc.handle", "m"),
    ]
    return fsm


def run_cli(tmp_path, rows, *, correlation=True, column="request_id", headers=None):
    artifacts = tmp_path / "artifacts"
    for service in {row[1] for row in rows}:
        service_dir = artifacts / service
        service_dir.mkdir(parents=True, exist_ok=True)
        (service_dir / "handle.fsm.json").write_text(json.dumps(serialize_fsm(two_step_fsm())))
        (service_dir / "handle.flow.json").write_text(json.dumps({
            "entrypoint_node_id": "m", "entrypoint_name": "handle",
            "entrypoint_full_name": "svc.handle", "methods": {}, "external_calls": [],
        }))
        (service_dir / "trie.json").write_text(json.dumps({
            "templates": [
                {"call_node_id": "l0", "raw_template": "start <*>", "tokens": ["start", "<*>"], "static_count": 1},
                {"call_node_id": "l1", "raw_template": "done <*>", "tokens": ["done", "<*>"], "static_count": 1},
            ],
        }))
    logs = tmp_path / "logs.csv"
    with logs.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers or ["timestamp", "service", "message", "request_id"])
        writer.writerows(rows)
    output = tmp_path / "incident.json"
    command = [
        sys.executable, str(Path(__file__).resolve().parents[1] / "runtime_analyzer.py"),
        "--artifacts-dir", str(artifacts), "--logs", str(logs), "--output", str(output),
        "--inject-time", "100", "--emit-incomplete",
    ]
    if correlation:
        command += ["--correlation-column", column]
    result = subprocess.run(command, capture_output=True, text=True)
    return result, json.loads(output.read_text()) if output.exists() else None


def test_cli_keeps_interleaved_requests_separate(tmp_path):
    result, report = run_cli(tmp_path, [
        (100, "svc", "start a", "a"),
        (101, "svc", "start b", "b"),
        (102, "svc", "done b", "b"),
    ])
    assert result.returncode == 0, result.stderr
    assert len(report["chains"]) == 1
    chain = report["chains"][0]
    assert chain["correlation_key"] == "b"
    assert [log["message"] for log in chain["runtime_logs"]] == ["start b", "done b"]
    assert {log["correlation_key"] for log in chain["runtime_logs"]} == {"b"}
    assert [log["bucket"] for log in chain["runtime_logs"]] == ["svc@101", "svc@102"]
    assert {h["correlation_key"] for h in report["incomplete_hypotheses"]} == {"a"}


def test_cli_finishes_both_requests_with_their_own_end(tmp_path):
    result, report = run_cli(tmp_path, [
        (100, "svc", "start a", "a"), (101, "svc", "start b", "b"),
        (102, "svc", "done b", "b"), (103, "svc", "done a", "a"),
    ])
    assert result.returncode == 0, result.stderr
    assert {c["correlation_key"]: [log["message"] for log in c["runtime_logs"]]
            for c in report["chains"]} == {"a": ["start a", "done a"], "b": ["start b", "done b"]}


def test_cli_keeps_missing_keys_unexplained(tmp_path):
    result, report = run_cli(tmp_path, [
        (100, "svc", "start a", "a"), (101, "svc", "done unknown", ""),
        (102, "svc", "start unknown", "  "), (103, "svc", "done a", "a"),
    ])
    assert result.returncode == 0, result.stderr
    assert len(report["chains"]) == 1
    assert [log["message"] for log in report["chains"][0]["runtime_logs"]] == ["start a", "done a"]
    assert [r["runtime_event"]["message"] for r in report["unexplained_events"]] == ["done unknown", "start unknown"]
    assert {r["classification"]["reason_code"] for r in report["unexplained_events"]} == {"missing_correlation_key"}
    assert all(r["runtime_event"]["correlation_key"] is None for r in report["unexplained_events"])


def test_cli_requires_the_exact_requested_column(tmp_path):
    result, report = run_cli(tmp_path, [(100, "svc", "start a", "a")], column="Request ID")
    assert result.returncode != 0
    assert "Missing correlation column: Request ID" in result.stderr
    assert report is None


def test_default_does_not_enable_correlation_from_column_name(tmp_path):
    result, report = run_cli(tmp_path, [
        (100, "svc", "start a", "a"), (101, "svc", "start b", "b"),
        (102, "svc", "done b", "b"),
    ], correlation=False)
    assert result.returncode == 0, result.stderr
    assert len(report["chains"]) == 2
    assert {tuple(log["message"] for log in c["runtime_logs"]) for c in report["chains"]} == {
        ("start a", "done b"), ("start b", "done b"),
    }


def test_same_key_does_not_cross_services(tmp_path):
    result, report = run_cli(tmp_path, [
        (100, "svc", "start first", "trace-1"), (101, "other", "start second", "trace-1"),
        (102, "other", "done second", "trace-1"),
    ])
    assert result.returncode == 0, result.stderr
    assert [c["service"] for c in report["chains"]] == ["other"]
    assert [h["service"] for h in report["incomplete_hypotheses"]] == ["svc"]


@pytest.mark.parametrize("key", ["0001", "NA", "a", "A"])
def test_cli_preserves_text_keys(tmp_path, key):
    result, report = run_cli(tmp_path, [(100, "svc", "start work", key), (101, "svc", "done work", key)])
    assert result.returncode == 0, result.stderr
    assert [c["correlation_key"] for c in report["chains"]] == [key]


@pytest.mark.parametrize("missing", [None, pd.NA, float("nan"), "", " \t "])
def test_store_rejects_missing_keys_even_with_exact_call_ids(missing):
    store = ExplicitHypothesisStore({"handle": two_step_fsm()}, require_correlation=True)
    start = store.process("start a", correlation_key="a", observed_call_node_id="l0")
    assert start[0].verdict == "new_chain"
    result = store.process("done missing", correlation_key=missing, observed_call_node_id="l1")
    assert result[0].reason_code == "missing_correlation_key"
    assert store.active_chains[0].status == "active"
    assert len(store.active_chains) == 1


def test_missing_key_from_unknown_service_is_json_safe():
    processor = RuntimeStreamProcessor({})
    result = processor.process(RuntimeEvent(0, None, "missing", "", "start", correlation_key=pd.NA))
    assert result[0]["runtime_event"]["correlation_key"] is None
    json.dumps(result)


def test_stream_frame_passes_request_keys():
    fsm = two_step_fsm()
    store = ExplicitHypothesisStore({"handle": fsm}, require_correlation=True)
    processor = RuntimeStreamProcessor({"svc": ServiceCatalog({"handle": fsm}, {"handle": {}}, store)})
    frame = pd.DataFrame({
        "timestamp": pd.to_datetime([100, 101, 102], unit="s", utc=True),
        "service": "svc", "message": ["start a", "start b", "done b"],
        "call": ["l0", "l0", "l1"], "request": ["a", "b", "b"],
    })
    reports = processor.process_frame(frame, call_node_column="call", correlation_column="request")
    assert [r["chain"]["correlation_key"] for r in reports] == ["b"]
    assert store.active_chains[0].status == "active"


def test_store_frame_rejects_keys_when_correlation_is_disabled():
    store = ExplicitHypothesisStore({"handle": two_step_fsm()})
    frame = pd.DataFrame({"timestamp": [None], "message": ["start a"], "request": ["a"]})
    with pytest.raises(ValueError, match="require_correlation=True"):
        store.process_frame(frame, correlation_column="request")
    assert not store.active_chains
    assert not store.history


def test_stream_frame_requires_correlation_in_every_store():
    fsm = two_step_fsm()
    enabled = ExplicitHypothesisStore({"handle": fsm}, require_correlation=True)
    disabled = ExplicitHypothesisStore({"handle": fsm})
    processor = RuntimeStreamProcessor({
        "enabled": ServiceCatalog({"handle": fsm}, {"handle": {}}, enabled),
        "disabled": ServiceCatalog({"handle": fsm}, {"handle": {}}, disabled),
    })
    frame = pd.DataFrame({
        "timestamp": [None], "service": ["enabled"], "message": ["start a"], "request": ["a"],
    })
    with pytest.raises(ValueError, match="require_correlation=True"):
        processor.process_frame(frame, correlation_column="request")
    assert not enabled.active_chains
    assert not disabled.active_chains
    assert not enabled.history
    assert not disabled.history


@pytest.mark.parametrize("blank", ["", " \t "])
def test_cli_empty_call_cells_use_trie(tmp_path, blank):
    result, report = run_cli(tmp_path, [
        (100, "svc", "start work", "NA", blank),
        (101, "svc", "done work", "NA", blank),
    ], headers=["timestamp", "service", "message", "request_id", "call_node_id"])
    assert result.returncode == 0, result.stderr
    assert len(report["chains"]) == 1
    assert report["chains"][0]["correlation_key"] == "NA"
    assert not report["unexplained_events"]


@pytest.mark.parametrize("api", ["store", "stream"])
@pytest.mark.parametrize("blank", ["", " \t ", None, pd.NA])
def test_frame_empty_call_cells_mean_absent(tmp_path, api, blank):
    fsm = two_step_fsm()
    store = ExplicitHypothesisStore({"handle": fsm}, require_correlation=True)
    frame = pd.DataFrame({
        "timestamp": pd.to_datetime([100, 101], unit="s", utc=True),
        "service": ["svc", "svc"], "message": ["start work", "done work"],
        "request": ["a", "a"], "call": [blank, blank],
    })
    if api == "store":
        events = store.process_frame(frame, call_node_column="call", correlation_column="request")
        assert events["verdict"].tolist() == ["new_chain", "completed"]
    else:
        trie_path = tmp_path / "trie.json"
        trie_path.write_text(json.dumps({"templates": [
            {"call_node_id": call, "raw_template": text + " <*>",
             "tokens": [text, "<*>"], "static_count": 1}
            for call, text in (("l0", "start"), ("l1", "done"))
        ]}))
        processor = RuntimeStreamProcessor({"svc": ServiceCatalog(
            {"handle": fsm}, {"handle": {}}, store, ServiceTrieMatcher(trie_path),
        )})
        reports = processor.process_frame(frame, call_node_column="call", correlation_column="request")
        assert [report["report_type"] for report in reports] == ["completed_execution_hypothesis"]
    assert store.active_chains[0].status == "completed"


def test_cli_nonempty_unknown_call_cell_stays_exact(tmp_path):
    result, report = run_cli(tmp_path, [
        (100, "svc", "start work", "a", "unknown"),
        (101, "svc", "done work", "a", "unknown"),
    ], headers=["timestamp", "service", "message", "request_id", "call_node_id"])
    assert result.returncode == 0, result.stderr
    assert not report["chains"]
    assert len(report["unexplained_events"]) == 2


def test_direct_stream_empty_call_id_stays_exact():
    fsm = two_step_fsm()
    store = ExplicitHypothesisStore({"handle": fsm})
    processor = RuntimeStreamProcessor({"svc": ServiceCatalog({"handle": fsm}, {"handle": {}}, store)})
    reports = processor.process(RuntimeEvent(0, None, "svc", "", "start work", observed_call_node_id=""))
    assert [report["report_type"] for report in reports] == ["structurally_unexplained_event"]
    assert not store.active_chains
