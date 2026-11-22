"""Measure candidate coverage on a CSV log corpus.

This checks log-to-CALL candidates. It does not measure call-site precision,
execution-path accuracy, or root-cause accuracy. Each distinct message is
matched once. Its count weights the row totals. Runtime order is not used.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", type=Path)
    parser.add_argument("artifacts", type=Path)
    parser.add_argument("--code-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--service-column", default="container_name")
    parser.add_argument("--message-column", default="message")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--outcomes", type=Path, help="Optional per-message hashes and candidate sets")
    args = parser.parse_args(argv)
    if not args.logs.is_file() or not args.artifacts.is_dir():
        parser.error("The log file and artifact directory must exist")

    counts = defaultdict(Counter)
    with args.logs.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {args.service_column, args.message_column}
        if not required.issubset(reader.fieldnames or []):
            parser.error(f"Missing required CSV columns: {sorted(required)}")
        for row_number, row in enumerate(reader, start=2):
            service, message = row[args.service_column], row[args.message_column]
            if service is None or not service.strip() or message is None:
                parser.error(f"Missing service or message at CSV row {row_number}")
            counts[service][message] += 1
    if not counts:
        parser.error("The CSV has no log rows")

    code_root = args.code_root.resolve()
    sys.path.insert(0, str(code_root))
    from src.online.trie_matcher import ServiceTrieMatcher

    commit = subprocess.run(
        ["git", "-C", str(code_root), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.strip() or None
    report = {
        "schema_version": 1,
        "experiment": "runtime_log_candidate_coverage",
        "limits": [
            "Candidate coverage is not call-site precision or RCA accuracy.",
            "Source and runtime version agreement must be checked separately.",
            "Messages are matched without using event order or request identity.",
            "Duplicate rows weight the totals; each distinct message is matched once.",
        ],
        "input": {"file": args.logs.name, "sha256": digest(args.logs)},
        "code_commit": commit,
        "matcher_files": {
            name: digest(code_root / name)
            for name in ("src/online/trie_matcher.py", "src/log_text.py")
            if (code_root / name).is_file()
        },
        "services": {},
    }
    outcomes = []
    fields = ("rows", "distinct_messages", "matched_rows", "unique_candidate_rows",
              "ambiguous_rows", "unmatched_rows", "error_rows", "missing_artifact_rows")
    for service, messages in sorted(counts.items()):
        path = args.artifacts / service / "trie.json"
        # Service names come from the CSV. Do not permit paths outside artifacts.
        if path.resolve().parent.parent != args.artifacts.resolve():
            parser.error(f"Invalid service name: {service!r}")
        matcher = ServiceTrieMatcher(path) if path.is_file() else None
        summary = dict.fromkeys(fields, 0)
        summary["rows"] = sum(messages.values())
        summary["distinct_messages"] = len(messages)
        summary["artifact_sha256"] = digest(path) if matcher is not None else None
        summary["examples"] = {"unmatched": [], "ambiguous": [], "error": []}
        for message, count in sorted(messages.items()):
            candidates, error = set(), None
            if matcher is None:
                summary["missing_artifact_rows"] += count
            else:
                try:
                    candidates = matcher.match_call_node_ids(message)
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    summary["error_rows"] += count
            if candidates:
                summary["matched_rows"] += count
                category = "unique_candidate_rows" if len(candidates) == 1 else "ambiguous_rows"
                summary[category] += count
            elif error is None:
                summary["unmatched_rows"] += count
            category = "error" if error else "ambiguous" if len(candidates) > 1 else "unmatched"
            if category != "ambiguous" and candidates:
                category = None
            if category and len(summary["examples"][category]) < 5:
                summary["examples"][category].append({
                    "message": message[:512], "rows": count,
                    "call_node_ids": sorted(candidates), "error": error,
                })
            outcomes.append({
                "service": service,
                "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
                "rows": count, "call_node_ids": sorted(candidates), "error": error,
            })
        report["services"][service] = summary
    report["totals"] = {
        key: sum(service[key] for service in report["services"].values()) for key in fields
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.outcomes:
        args.outcomes.parent.mkdir(parents=True, exist_ok=True)
        args.outcomes.write_text(json.dumps(outcomes, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["totals"], sort_keys=True))
    return 1 if report["totals"]["error_rows"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
