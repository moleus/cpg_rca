"""Compare offline and online matching on generated messages from real templates.

This is a contract check. Messages are made from the artifact tokens, with empty,
one-token, and multi-token wildcard values. It does not measure runtime coverage,
call-site precision, or RCA accuracy. Candidate sets are compared before ranking.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", type=Path, nargs="+", help="trie.json files or artifact directories")
    parser.add_argument("--code-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    paths = set()
    for path in args.artifacts:
        if not path.exists():
            parser.error(f"Artifact path does not exist: {path}")
        paths.update([path] if path.is_file() else path.rglob("trie.json"))
    if not paths:
        parser.error("No trie.json files found")

    sys.path.insert(0, str(args.code_root.resolve()))
    from src.offline import log as offline
    from src.online.trie_matcher import ServiceTrieMatcher

    report = {
        "experiment": "matcher_contract_parity",
        "messages": "synthetic expansions of stored templates",
        "wildcard_values": ["", "value", "value with spaces"],
        "services": [],
    }
    try:
        report["code_commit"] = subprocess.check_output(
            ["git", "-C", str(args.code_root), "rev-parse", "HEAD"], text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        report["code_commit"] = None

    for path in sorted(paths):
        raw_bytes = path.read_bytes()
        payload = json.loads(raw_bytes)
        templates = [offline.LogTemplateWithMethod(
            call_node_id=str(row["call_node_id"]),
            method_name=str(row.get("method_name", "unknown")),
            method_node_id=str(row.get("method_node_id", "unknown")),
            raw_template=row["raw_template"], tokens=row["tokens"],
            static_count=row["static_count"],
        ) for row in payload["templates"] if row["static_count"] > 0]
        trie = offline.build_trie(templates)
        matcher = ServiceTrieMatcher(path)
        result = {
            "service": path.parent.name,
            "artifact_sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "templates": len(templates), "cases": 0,
            "same_candidate_sets": 0, "offline_origin_matched": 0,
            "online_origin_matched": 0, "failures": [],
        }
        for template in templates:
            for value in report["wildcard_values"]:
                message = " ".join(value if token == "<*>" else token for token in template.tokens)
                candidates = []
                offline._trie_match(
                    trie, offline._clean_text_selective(message).split(), 0, candidates,
                )
                offline_ids = {row.call_node_id for row in candidates if row.static_count > 0}
                online_ids = matcher.match_call_node_ids(message)
                result["cases"] += 1
                result["same_candidate_sets"] += offline_ids == online_ids
                offline_origin = template.call_node_id in offline_ids
                online_origin = template.call_node_id in online_ids
                result["offline_origin_matched"] += offline_origin
                result["online_origin_matched"] += online_origin
                failed = offline_ids != online_ids or not offline_origin or not online_origin
                if failed and len(result["failures"]) < 20:
                    result["failures"].append({
                        "template": template.raw_template, "message": message,
                        "origin_call_node_id": template.call_node_id,
                        "offline_origin_matched": offline_origin,
                        "online_origin_matched": online_origin,
                        "offline_ids": sorted(offline_ids), "online_ids": sorted(online_ids),
                    })
        report["services"].append(result)

    report["totals"] = {
        key: sum(service[key] for service in report["services"])
        for key in ("templates", "cases", "same_candidate_sets", "offline_origin_matched", "online_origin_matched")
    }
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    totals = report["totals"]
    return 0 if all(
        totals[key] == totals["cases"]
        for key in ("same_candidate_sets", "offline_origin_matched", "online_origin_matched")
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
