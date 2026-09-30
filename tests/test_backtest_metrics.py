"""Focused unit tests for backtest evaluation metrics: Brier score, log loss, ECE, and calibration bins."""

import math
import pytest
import backtest


def test_brier_score_empty():
    assert backtest.compute_brier_score([], []) is None


def test_brier_score_perfect_multiclass():
    predictions = [
        {"home_win": 1.0, "draw": 0.0, "away_win": 0.0},
        {"home_win": 0.0, "draw": 1.0, "away_win": 0.0},
    ]
    actuals = ["home_win", "draw"]
    score = backtest.compute_brier_score(predictions, actuals, outcomes=("home_win", "draw", "away_win"))
    assert score == 0.0


def test_brier_score_known_values():
    # Sample 1: pred (0.6, 0.3, 0.1), actual "home_win" (1, 0, 0) -> (0.6-1)^2 + 0.3^2 + 0.1^2 = 0.16 + 0.09 + 0.01 = 0.26
    # Sample 2: pred (0.2, 0.5, 0.3), actual "away_win" (0, 0, 1) -> 0.2^2 + 0.5^2 + (0.3-1)^2 = 0.04 + 0.25 + 0.49 = 0.78
    # Mean = (0.26 + 0.78) / 2 = 0.52
    predictions = [
        {"home_win": 0.6, "draw": 0.3, "away_win": 0.1},
        {"home_win": 0.2, "draw": 0.5, "away_win": 0.3},
    ]
    actuals = ["home_win", "away_win"]
    score = backtest.compute_brier_score(predictions, actuals, outcomes=("home_win", "draw", "away_win"))
    assert score is not None
    assert abs(score - 0.52) < 1e-6


def test_log_loss_empty():
    assert backtest.compute_log_loss([], []) is None


def test_log_loss_perfect():
    predictions = [
        {"home_win": 1.0, "draw": 0.0, "away_win": 0.0},
        {"home_win": 0.0, "draw": 1.0, "away_win": 0.0},
    ]
    actuals = ["home_win", "draw"]
    score = backtest.compute_log_loss(predictions, actuals, outcomes=("home_win", "draw", "away_win"))
    assert score is not None
    assert score < 1e-10


def test_log_loss_epsilon_clipping():
    # Extreme probabilities 0.0 and 1.0 for wrong outcome
    predictions = [{"home_win": 0.0, "draw": 1.0, "away_win": 0.0}]
    actuals = ["home_win"]
    score = backtest.compute_log_loss(predictions, actuals, outcomes=("home_win", "draw", "away_win"))
    assert score is not None
    assert math.isfinite(score)
    expected = -math.log(1e-15)
    assert abs(score - expected) < 1e-4


def test_log_loss_known_values():
    predictions = [
        {"home_win": 0.5, "draw": 0.3, "away_win": 0.2},
        {"home_win": 0.3, "draw": 0.45, "away_win": 0.25},
    ]
    actuals = ["home_win", "away_win"]
    score = backtest.compute_log_loss(predictions, actuals, outcomes=("home_win", "draw", "away_win"))
    assert score is not None
    expected = (-math.log(0.5) - math.log(0.25)) / 2
    assert abs(score - expected) < 1e-6


def test_calibration_bins_empty():
    res = backtest.compute_calibration_bins([])
    assert res["ece"] is None
    assert res["total_samples"] == 0
    assert len(res["bins"]) == 8  # Covers required 8 ranges


def test_calibration_bins_required_ranges_structure():
    # Samples with varying probabilities
    samples = [
        (0.40, True),   # <50%
        (0.52, False),  # 50–55%
        (0.58, True),   # 55–60%
        (0.62, True),   # 60–65%
        (0.68, False),  # 65–70%
        (0.72, True),   # 70–75%
        (0.78, True),   # 75–80%
        (0.85, True),   # 80%+
    ]

    res = backtest.compute_calibration_bins(samples)
    assert res["ece"] is not None
    assert res["total_samples"] == 8

    bin_labels = [b["label"] for b in res["bins"]]
    expected_labels = ["<50%", "50–55%", "55–60%", "60–65%", "65–70%", "70–75%", "75–80%", "80%+"]
    assert bin_labels == expected_labels

    # Verify each bin has required keys
    for b in res["bins"]:
        assert "number_of_predictions" in b
        assert "mean_predicted_probability" in b
        assert "actual_empirical_success_rate" in b
        assert "calibration_gap" in b


def test_calibration_bins_exact_gap_calculation():
    # 2 predictions in bin 50-55%: 0.52 and 0.54 -> mean = 0.53
    # Actuals: 1 True, 0 False -> empirical rate = 0.50
    # Gap = |0.53 - 0.50| = 0.03
    samples = [
        (0.52, True),
        (0.54, False),
    ]

    res = backtest.compute_calibration_bins(samples)
    bin_50_55 = [b for b in res["bins"] if b["label"] == "50–55%"][0]

    assert bin_50_55["number_of_predictions"] == 2
    assert bin_50_55["mean_predicted_probability"] == 0.53
    assert bin_50_55["actual_empirical_success_rate"] == 0.50
    assert bin_50_55["calibration_gap"] == 0.03
    assert res["ece"] == 0.03


def test_market_calibration_multiclass():
    predictions = [
        {"home_win": 0.60, "draw": 0.25, "away_win": 0.15},
    ]
    actuals = ["home_win"]

    res = backtest.compute_market_calibration(predictions, actuals, outcomes=("home_win", "draw", "away_win"))
    assert res["total_samples"] == 3  # 3 outcomes evaluated


def test_over_under_and_all_markets_evaluated_in_backtest(monkeypatch, tmp_path):
    from tests.test_backtest_prediction_engine_integration import historical_dataset

    monkeypatch.setattr(backtest.config, "DB_PATH", str(tmp_path / "predictions.db"))
    backtest.storage.init_db()
    fixtures = historical_dataset()
    backtest.storage.save_historical_fixtures(fixtures, league_id=39, season=2025)
    backtest.storage.mark_historical_dataset_complete(39, 2025, len(fixtures))

    res = backtest.run_real_backtest(
        league_id=39,
        season=2025,
        sample_size=10,
        min_prior_matches=5,
        sample_seed=42,
    )

    evaluation = res.get("evaluation", {})
    assert "match_result" in evaluation
    assert "double_chance" in evaluation
    assert "over_under_2_5" in evaluation
    assert "btts" in evaluation

    ou_eval = evaluation["over_under_2_5"]
    assert ou_eval["brier_score"] is not None
    assert ou_eval["log_loss"] is not None
    assert ou_eval["calibration"]["ece"] is not None

    dc_eval = evaluation["double_chance"]
    assert dc_eval["brier_score"] is not None
    assert dc_eval["log_loss"] is not None
    assert dc_eval["calibration"]["ece"] is not None

    btts_eval = evaluation["btts"]
    assert btts_eval["brier_score"] is not None
    assert btts_eval["log_loss"] is not None
    assert btts_eval["calibration"]["ece"] is not None


def test_over_under_2_5_evaluation_consistency():
    # 1. Prediction probabilities: P(over_2_5)=0.45, P(under_2_5)=0.55
    pred_markets = {
        "over_under": {
            "over_2_5": 0.45,
            "under_2_5": 0.55,
        }
    }
    # 2. Actual match with 3 total goals (2-1) -> actual outcome = "over"
    fixture = {
        "goals": {"home": 2, "away": 1}
    }

    grading = backtest._grade_prediction_markets(pred_markets, fixture)
    selected_ou = grading["selected"]["over_under"]["2.5"]

    # Top pick = "under_2_5" (0.55 > 0.45)
    assert selected_ou["pick"] == "under_2_5"
    assert selected_ou["actual"] == "over"
    assert selected_ou["won"] is False  # Top pick "under_2_5" lost because actual was "over"

    # Evaluate Brier and Log Loss for this sample
    preds = [{"over": 0.45, "under": 0.55}]
    acts = ["over"]

    brier = backtest.compute_brier_score(preds, acts, outcomes=("over", "under"))
    # Standard binary Brier score: (p_over - y)^2 = (0.45 - 1.0)^2 = 0.3025
    assert abs(brier - 0.3025) < 1e-6

    log_loss = backtest.compute_log_loss(preds, acts, outcomes=("over", "under"))
    # -ln(0.45) = 0.798507696
    assert abs(log_loss - (-math.log(0.45))) < 1e-6

    accuracy = backtest.compute_binary_accuracy(preds, acts)
    # Top pick = "under" (0.55), actual = "over" -> accuracy = 0.0
    assert accuracy == 0.0


def test_run_multi_season_backtest(monkeypatch, tmp_path):
    from tests.test_backtest_prediction_engine_integration import historical_dataset

    monkeypatch.setattr(backtest.config, "DB_PATH", str(tmp_path / "predictions.db"))
    backtest.storage.init_db()
    fixtures_2024 = historical_dataset()

    # Create 2025 fixtures with distinct IDs
    fixtures_2025 = []
    for item in historical_dataset():
        item_copy = dict(item)
        item_copy["fixture"] = dict(item["fixture"])
        item_copy["fixture"]["id"] = item["fixture"]["id"] + 100000
        fixtures_2025.append(item_copy)

    backtest.storage.save_historical_fixtures(fixtures_2024, league_id=39, season=2024)
    backtest.storage.mark_historical_dataset_complete(39, 2024, len(fixtures_2024))
    backtest.storage.save_historical_fixtures(fixtures_2025, league_id=39, season=2025)
    backtest.storage.mark_historical_dataset_complete(39, 2025, len(fixtures_2025))

    res = backtest.run_multi_season_backtest(
        league_id=39,
        seasons=[2024, 2025],
        sample_size_per_season=10,
        min_prior_matches=5,
        sample_seed=42,
    )

    assert res["seasons_evaluated"] == [2024, 2025]
    assert res["graded"] == 20
    assert "match_result" in res["evaluation"]
    assert res["evaluation"]["match_result"]["brier_score"] is not None
