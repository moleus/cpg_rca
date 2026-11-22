import csv
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_coverage_keeps_duplicates_ambiguity_and_unknown_services(tmp_path):
    logs = tmp_path / "logs.csv"
    with logs.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["container_name", "message"])
        writer.writerows([
            ("svc", "alpha"), ("svc", "alpha"), ("svc", "beta"),
            ("other", "alpha"), ("svc", "gamma"),
        ])
    artifacts = tmp_path / "artifacts"
    service = artifacts / "svc"
    service.mkdir(parents=True)
    templates = [
        {"call_node_id": call_id, "raw_template": text, "tokens": [text], "static_count": 1}
        for call_id, text in (("a", "alpha"), ("g1", "gamma"), ("g2", "gamma"))
    ]
    (service / "trie.json").write_text(json.dumps({"templates": templates}))
    output, outcomes = tmp_path / "result.json", tmp_path / "outcomes.json"
    result = subprocess.run([
        sys.executable, "-m", "eval.runtime_match_coverage", str(logs), str(artifacts),
        "--output", str(output), "--outcomes", str(outcomes),
    ], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    totals = json.loads(output.read_text())["totals"]
    assert totals == {
        "rows": 5, "distinct_messages": 4, "matched_rows": 3,
        "unique_candidate_rows": 2, "ambiguous_rows": 1,
        "unmatched_rows": 2, "error_rows": 0, "missing_artifact_rows": 1,
    }
    rows = json.loads(outcomes.read_text())
    assert len(rows) == 4
    assert sum(row["rows"] for row in rows) == 5
    assert any(row["call_node_ids"] == ["g1", "g2"] for row in rows)
    assert not any("message" in row for row in rows)
