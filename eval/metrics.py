"""
Ranking-quality metrics for root-cause localization: AC@k, MRR, Recall@k.

Metric definitions
-------------------
AC@k      : fraction of incidents where AT LEAST ONE ground-truth item
            is within the top-k predicted items ("hit@k" elsewhere).
MRR       : mean over incidents of 1 / rank(first ground-truth item
            found in the prediction); 0 if none appear in the prediction.
Recall@k : mean over incidents of |ground_truth intersect top-k| / |ground_truth|

Input format
------------
A JSON file mapping incident_id -> {"prediction": [...], "ground_truth": [...]}
(or --predictions-dir/--ground-truth-dir for two directories of one JSON
file per incident instead -- see load_pair_dirs()). In directory mode, ground
truth defines the cohort. A missing prediction gets an empty ranking and stays
in every metric denominator. A present empty ranking is an abstention.
Completion rate measures available predictions, including abstentions. It does
not measure diagnostic accuracy. Rankings must be lists of nonblank strings;
ground truth must contain at least one item.

Usage
-----
    python3 metrics.py --input results.json --ks 1,3,5
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def normalize(items: list[str]) -> list[str]:
    return [str(item).strip().lower() for item in items]


def rank_of_best_match(prediction: list[str], ground_truth: list[str]) -> int | None:
    pred_norm = normalize(prediction)
    gt_norm = set(normalize(ground_truth))
    for rank, item in enumerate(pred_norm, start=1):
        if item in gt_norm:
            return rank
    return None


def hit_at_k(prediction: list[str], ground_truth: list[str], k: int) -> bool:
    gt_norm = set(normalize(ground_truth))
    top_k = set(normalize(prediction[:k]))
    return bool(gt_norm & top_k)


def recall_at_k(prediction: list[str], ground_truth: list[str], k: int) -> float:
    gt_norm = set(normalize(ground_truth))
    if not gt_norm:
        return 0.0
    top_k = set(normalize(prediction[:k]))
    return len(gt_norm & top_k) / len(gt_norm)


def reciprocal_rank(prediction: list[str], ground_truth: list[str]) -> float:
    rank = rank_of_best_match(prediction, ground_truth)
    return 1.0 / rank if rank is not None else 0.0


def evaluate_bug(prediction: list[str], ground_truth: list[str], ks: list[int]) -> dict[str, Any]:
    return {
        "rank_of_best_match": rank_of_best_match(prediction, ground_truth),
        "reciprocal_rank": reciprocal_rank(prediction, ground_truth),
        **{f"hit@{k}": hit_at_k(prediction, ground_truth, k) for k in ks},
        **{f"recall@{k}": recall_at_k(prediction, ground_truth, k) for k in ks},
    }


def aggregate(per_bug: dict[str, dict[str, Any]], ks: list[int]) -> dict[str, Any]:
    n = len(per_bug)
    if n == 0:
        return {"bug_count": 0}

    agg: dict[str, Any] = {"bug_count": n}
    agg["mrr"] = sum(v["reciprocal_rank"] for v in per_bug.values()) / n
    for k in ks:
        agg[f"ac@{k}"] = sum(1 for v in per_bug.values() if v[f"hit@{k}"]) / n
        agg[f"recall@{k}"] = sum(v[f"recall@{k}"] for v in per_bug.values()) / n
    found = [v["rank_of_best_match"] for v in per_bug.values() if v["rank_of_best_match"] is not None]
    agg["avg_rank_when_found"] = sum(found) / len(found) if found else None
    agg["not_found_count"] = n - len(found)
    return agg


def _read_ranking(path: Path, field: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))["return"][field]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(f"Invalid evaluation file {path}: expected return.{field}") from error


def load_pair_dirs(predictions_dir: Path, ground_truth_dir: Path) -> dict[str, dict[str, Any]]:
    """Score every labelled incident, including runs with no prediction file."""
    for directory in (predictions_dir, ground_truth_dir):
        if not directory.is_dir():
            raise ValueError(f"Input directory does not exist: {directory}")
    prediction_files = {p.stem: p for p in predictions_dir.glob("*.json") if p.is_file()}
    ground_truth_files = {p.stem: p for p in ground_truth_dir.glob("*.json") if p.is_file()}
    unlabelled = sorted(prediction_files.keys() - ground_truth_files.keys())
    if unlabelled:
        raise ValueError(f"Predictions without ground truth: {', '.join(unlabelled)}")

    results: dict[str, dict[str, Any]] = {}
    for incident_id, gt_file in sorted(ground_truth_files.items()):
        pred_file = prediction_files.get(incident_id)
        prediction = (
            _read_ranking(pred_file, "potential_root_cause")
            if pred_file else []
        )
        ground_truth = _read_ranking(gt_file, "real_root_cause")
        results[incident_id] = {
            "prediction": prediction,
            "ground_truth": ground_truth,
            "prediction_available": pred_file is not None,
        }
    return results


def load_json_input(path: Path) -> dict[str, dict[str, list[str]]]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_results(results: dict[str, dict[str, Any]]) -> None:
    if not isinstance(results, dict):
        raise ValueError("Evaluation input must map incident IDs to prediction and ground truth")
    for incident_id, data in results.items():
        if not isinstance(data, dict):
            raise ValueError(f"Incident {incident_id}: expected prediction and ground truth")
        for field in ("prediction", "ground_truth"):
            values = data.get(field)
            if not isinstance(values, list) or any(
                not isinstance(item, str) or not item.strip() for item in values
            ):
                raise ValueError(f"Incident {incident_id}: {field} must be a list of nonblank strings")
            if field == "ground_truth" and not values:
                raise ValueError(f"Incident {incident_id}: ground_truth must not be empty")
        available = data.get("prediction_available", True)
        if not isinstance(available, bool):
            raise ValueError(f"Incident {incident_id}: prediction_available must be a boolean")
        if not available and data["prediction"]:
            raise ValueError(f"Incident {incident_id}: unavailable prediction must be empty")


def run(results: dict[str, dict[str, Any]], ks: list[int]) -> dict[str, Any]:
    validate_results(results)
    if not ks or any(type(k) is not int or k < 1 for k in ks):
        raise ValueError("Metric cutoffs must be positive integers")
    per_bug = {
        bug_id: {
            **evaluate_bug(data["prediction"], data["ground_truth"], ks),
            "prediction_available": data.get("prediction_available", True),
        }
        for bug_id, data in results.items()
    }
    summary = aggregate(per_bug, ks)
    missing = sum(not v["prediction_available"] for v in per_bug.values())
    summary["missing_prediction_count"] = missing
    summary["completion_rate"] = (len(per_bug) - missing) / len(per_bug) if per_bug else None
    return {"per_bug": per_bug, "aggregate": summary}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, help="JSON file: {incident_id: {prediction, ground_truth}}")
    parser.add_argument("--predictions-dir", type=Path, help="directory of one {incident_id}.json prediction file per incident")
    parser.add_argument("--ground-truth-dir", type=Path, help="directory of one {incident_id}.json ground-truth file per incident")
    parser.add_argument("--ks", default="1,3,5", help="comma-separated k values for AC@k/Recall@k")
    parser.add_argument("--output", type=Path, help="optional path to write the full JSON report")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        ks = [int(k) for k in args.ks.split(",")]
        if args.input:
            results = load_json_input(args.input)
        elif args.predictions_dir and args.ground_truth_dir:
            results = load_pair_dirs(args.predictions_dir, args.ground_truth_dir)
        else:
            print("Provide either --input or both --predictions-dir and --ground-truth-dir", file=sys.stderr)
            return 1
        report = run(results, ks)
    except (OSError, ValueError, KeyError) as error:
        print(f"Cannot load evaluation inputs: {error}", file=sys.stderr)
        return 1

    if not results:
        print("No ground-truth incidents found.", file=sys.stderr)
        return 1

    print(f"Evaluated {report['aggregate']['bug_count']} bugs")
    print(f"  MRR            : {report['aggregate']['mrr']:.3f}")
    for k in ks:
        print(f"  AC@{k}          : {report['aggregate'][f'ac@{k}']:.3f}")
        print(f"  Recall@{k}      : {report['aggregate'][f'recall@{k}']:.3f}")
    print(f"  Avg rank (found): {report['aggregate']['avg_rank_when_found']}")
    print(f"  Not found       : {report['aggregate']['not_found_count']}/{report['aggregate']['bug_count']}")
    print(f"  Missing predictions: {report['aggregate']['missing_prediction_count']}/{report['aggregate']['bug_count']}")
    print()
    for bug_id, v in report["per_bug"].items():
        print(f"  {bug_id}: rank={v['rank_of_best_match']} RR={v['reciprocal_rank']:.3f}")

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2))
        print(f"\nFull report written to {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
