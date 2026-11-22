"""An evaluation cohort comes from ground truth, including failed runs."""

import json

import pytest

from eval import metrics


def write_case(directory, incident, field, values):
    path = directory / f"{incident}.json"
    path.write_text(json.dumps({"return": {field: values}}), encoding="utf-8")


@pytest.fixture
def pair_dirs(tmp_path):
    predictions = tmp_path / "predictions"
    truth = tmp_path / "truth"
    predictions.mkdir()
    truth.mkdir()
    for incident, cause in (("one", "payment"), ("two", "shipping")):
        write_case(truth, incident, "real_root_cause", [cause])
    return predictions, truth


def test_missing_prediction_is_a_failed_incident(pair_dirs):
    predictions, truth = pair_dirs
    write_case(predictions, "one", "potential_root_cause", ["payment"])

    report = metrics.run(metrics.load_pair_dirs(predictions, truth), [1, 3])

    assert report["aggregate"]["bug_count"] == 2
    assert report["aggregate"]["ac@1"] == 0.5
    assert report["aggregate"]["mrr"] == 0.5
    assert report["aggregate"]["recall@3"] == 0.5
    assert report["aggregate"]["missing_prediction_count"] == 1
    assert report["aggregate"]["completion_rate"] == 0.5
    assert report["per_bug"]["two"]["prediction_available"] is False


def test_all_missing_predictions_still_produce_a_report(pair_dirs):
    predictions, truth = pair_dirs

    report = metrics.run(metrics.load_pair_dirs(predictions, truth), [1])

    assert report["aggregate"]["bug_count"] == 2
    assert report["aggregate"]["ac@1"] == 0
    assert report["aggregate"]["mrr"] == 0
    assert report["aggregate"]["not_found_count"] == 2
    assert report["aggregate"]["missing_prediction_count"] == 2
    assert report["aggregate"]["completion_rate"] == 0


def test_present_empty_prediction_is_an_abstention(pair_dirs):
    predictions, truth = pair_dirs
    write_case(predictions, "one", "potential_root_cause", [])
    write_case(predictions, "two", "potential_root_cause", ["shipping"])

    report = metrics.run(metrics.load_pair_dirs(predictions, truth), [1])

    assert report["aggregate"]["ac@1"] == 0.5
    assert report["aggregate"]["completion_rate"] == 1
    assert report["aggregate"]["missing_prediction_count"] == 0


def test_prediction_without_ground_truth_is_rejected(pair_dirs):
    predictions, truth = pair_dirs
    write_case(predictions, "typo", "potential_root_cause", ["payment"])

    with pytest.raises(ValueError, match="without ground truth.*typo"):
        metrics.load_pair_dirs(predictions, truth)


@pytest.mark.parametrize("which", [0, 1])
def test_missing_directory_is_rejected(pair_dirs, which):
    paths = list(pair_dirs)
    paths[which] = paths[which] / "does-not-exist"

    with pytest.raises(ValueError, match="directory does not exist"):
        metrics.load_pair_dirs(*paths)


def test_complete_json_input_keeps_existing_scores():
    report = metrics.run({
        "one": {"prediction": ["cache", "Payment"], "ground_truth": ["payment"]},
        "two": {"prediction": ["shipping"], "ground_truth": ["shipping", "queue"]},
    }, [1, 3])

    assert report["aggregate"]["mrr"] == 0.75
    assert report["aggregate"]["ac@1"] == 0.5
    assert report["aggregate"]["ac@3"] == 1
    assert report["aggregate"]["recall@3"] == 0.75
    assert report["aggregate"]["completion_rate"] == 1


def test_cli_reports_missing_prediction(pair_dirs, tmp_path, capsys):
    predictions, truth = pair_dirs
    write_case(predictions, "one", "potential_root_cause", ["payment"])
    output = tmp_path / "report.json"

    assert metrics.main([
        "--predictions-dir", str(predictions), "--ground-truth-dir", str(truth),
        "--output", str(output),
    ]) == 0

    assert json.loads(output.read_text())["aggregate"]["ac@1"] == 0.5
    assert "Missing predictions: 1/2" in capsys.readouterr().out


def test_cli_rejects_incomplete_dataset(pair_dirs, capsys):
    predictions, truth = pair_dirs
    write_case(predictions, "typo", "potential_root_cause", ["payment"])

    assert metrics.main([
        "--predictions-dir", str(predictions), "--ground-truth-dir", str(truth),
    ]) == 1
    assert "without ground truth" in capsys.readouterr().err


@pytest.mark.parametrize("prediction,truth", [
    ("payment", "payment"), ([""], [""]), ([None], ["payment"]),
    (["payment"], []), (["payment"], [123]),
])
def test_invalid_rankings_do_not_produce_scores(prediction, truth):
    with pytest.raises(ValueError, match="Incident one"):
        metrics.run({"one": {"prediction": prediction, "ground_truth": truth}}, [1])


def test_unavailable_prediction_cannot_contain_a_ranking():
    with pytest.raises(ValueError, match="unavailable prediction must be empty"):
        metrics.run({"one": {
            "prediction": ["payment"], "ground_truth": ["payment"],
            "prediction_available": False,
        }}, [1])


def test_cli_rejects_malformed_prediction(pair_dirs, capsys):
    predictions, truth = pair_dirs
    (predictions / "one.json").write_text('{"return": null}', encoding="utf-8")

    assert metrics.main([
        "--predictions-dir", str(predictions), "--ground-truth-dir", str(truth),
    ]) == 1
    assert "one.json" in capsys.readouterr().err


@pytest.mark.parametrize("ks", [[], [0], [-1], [True], [1.5]])
def test_invalid_metric_cutoffs_are_rejected(ks):
    with pytest.raises(ValueError, match="positive integers"):
        metrics.run({"one": {"prediction": ["payment"], "ground_truth": ["payment"]}}, ks)


def test_cli_reports_invalid_metric_cutoff(pair_dirs, capsys):
    predictions, truth = pair_dirs
    assert metrics.main([
        "--predictions-dir", str(predictions), "--ground-truth-dir", str(truth), "--ks", "x",
    ]) == 1
    assert "Cannot load evaluation inputs" in capsys.readouterr().err
