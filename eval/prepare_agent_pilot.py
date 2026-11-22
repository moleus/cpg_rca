"""Create two matched input packets for a small, blind agent pilot.

The packets contain no case name, file path, or fault label. One packet has log
counts. The other adds static log candidates. The hidden answer is not read.
Use a fresh agent for each packet. Do not give it access to the source dataset.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
import re

from src.online.trie_matcher import ServiceTrieMatcher


def redact(message):
    message = re.sub(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b", "<id>", message)
    message = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "<email>", message)
    return re.sub(r"\d+", "<number>", message)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", type=Path)
    parser.add_argument("artifacts", type=Path)
    parser.add_argument("--incident-time", type=int, required=True)
    parser.add_argument("--window-seconds", type=int, default=120)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    counts = defaultdict(lambda: {"before": Counter(), "after": Counter()})
    originals = defaultdict(set)
    with args.logs.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            offset = int(row["timestamp"]) - args.incident_time
            if not -args.window_seconds <= offset < args.window_seconds:
                continue
            service, raw = row["container_name"], row["message"]
            message = redact(raw)
            counts[service]["before" if offset < 0 else "after"][message] += 1
            originals[service, message].add(raw)
    packet = {
        "case_id": "case_017",
        "task": "Rank at most three services for investigation. State what the evidence proves and what it does not prove.",
        "window": {"before_seconds": args.window_seconds, "after_seconds": args.window_seconds},
        "selection": "For each service: top three messages per window, plus top three messages first seen after the incident. Ties use text order.",
        "limits": [
            "Timestamps have one-second precision. No request or trace key is supplied.",
            "Numeric values, UUIDs, and email addresses are redacted before counts are grouped.",
            "Static source and runtime versions have not been shown to match.",
            "The service, fault type, and exact mechanism are hidden from this packet.",
        ],
        "services": [],
    }
    static = []
    for service, periods in sorted(counts.items()):
        before, after = periods["before"], periods["after"]
        top = lambda counter: sorted(counter, key=lambda message: (-counter[message], message))[:3]
        selected = set(top(before)) | set(top(after)) | set(top(Counter({k: v for k, v in after.items() if k not in before})))
        packet["services"].append({
            "service": service,
            "rows_before": sum(before.values()), "rows_after": sum(after.values()),
            "messages": [{"text": text, "before": before[text], "after": after[text]} for text in sorted(selected)],
        })
        path = args.artifacts / service / "trie.json"
        if path.resolve().parent.parent != args.artifacts.resolve():
            raise ValueError("Invalid service name")
        matcher = ServiceTrieMatcher(path) if path.is_file() else None
        catalog = json.loads(path.read_text())["templates"] if matcher else []
        for message in sorted(selected):
            candidates = set()
            if matcher:
                for raw in originals[service, message]:
                    candidates.update(matcher.match_call_node_ids(raw))
            static.append({
                "service": service, "text": message, "catalog_available": matcher is not None,
                "candidate_sites": [
                    {key: value for key, value in template.items()
                     if key in ("call_node_id", "method_node_id", "method_name", "raw_template")}
                    for template in catalog if str(template["call_node_id"]) in candidates
                ],
            })
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "logs_only.json").write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n")
    packet["static_candidates"] = static
    packet["static_limits"] = "These are log-to-source candidates. They do not prove an execution path, a branch decision, or a root cause. An empty set can result from source/version or extraction gaps."
    (args.output_dir / "logs_and_static_candidates.json").write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"services": len(counts), "selected_messages": len(static)}))


if __name__ == "__main__":
    main()
