"""
Basketball prediction model.

Uses team scoring/allowance statistics to estimate expected points and
derive moneyline and total-points probabilities.

Pipeline:
DATA → VALIDATION → POINT-IN-TIME FEATURES → MODEL → RAW PROBABILITIES → PROBABILITY VALIDATION → CALIBRATION → MARKET PROBABILITIES → ODDS / IMPLIED PROBABILITY → EDGE / EV → UNCERTAINTY → QUALITY GATE → SIGNAL / PASS → PERSISTENCE
"""

import math
from typing import Any, Dict, Optional

import basketball_api
import calibration
import config
import confidence
import market_analysis
import prediction_contract
import probability_validation
import quality_gate
import uncertainty


MARGIN_STD_DEV = 12.0
TOTAL_STD_DEV = 15.0
TOTAL_LINE = 224.5


def _valid_number(value):
    if isinstance(value, bool):
        return False

    try:
        value = float(value)
    except (TypeError, ValueError):
        return False

    return math.isfinite(value)


def _extract_scoring(stats):
    if not isinstance(stats, dict):
        return None, None

    try:
        points_for = stats["points"]["for"]["average"]["all"]
        points_against = stats["points"]["against"]["average"]["all"]
    except (KeyError, TypeError, AttributeError):
        return None, None

    if not _valid_number(points_for) or not _valid_number(points_against):
        return None, None

    points_for = float(points_for)
    points_against = float(points_against)

    if points_for < 0 or points_against < 0:
        return None, None

    return points_for, points_against


def _validate_probability(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0.0 <= float(value) <= 1.0
    ):
        raise ValueError("Probability must be a finite number between 0 and 1.")

    return float(value)


def _win_probability(point_diff):
    if not _valid_number(point_diff):
        raise ValueError("point_diff must be a finite number.")

    probability = 0.5 * (
        1 + math.erf(float(point_diff) / (MARGIN_STD_DEV * math.sqrt(2)))
    )

    return _validate_probability(probability)


def _over_probability(expected_total, line):
    if not _valid_number(expected_total):
        raise ValueError("expected_total must be a finite number.")

    if not _valid_number(line):
        raise ValueError("line must be a finite number.")

    diff = float(expected_total) - float(line)

    probability = 0.5 * (
        1 + math.erf(diff / (TOTAL_STD_DEV * math.sqrt(2)))
    )

    return _validate_probability(probability)


def build_basketball_safest_candidates(markets):
    if not isinstance(markets, dict):
        raise ValueError("markets must be a dictionary.")

    moneyline = markets.get("moneyline")
    total_points = markets.get("total_points")

    if not isinstance(moneyline, dict):
        raise ValueError("moneyline market is missing.")

    if not isinstance(total_points, dict):
        raise ValueError("total_points market is missing.")

    candidates = [
        ("Home Win", _validate_probability(moneyline["home_win"])),
        ("Away Win", _validate_probability(moneyline["away_win"])),
        (f"Over {total_points['line']} Points", _validate_probability(total_points["over"])),
        (f"Under {total_points['line']} Points", _validate_probability(total_points["under"])),
    ]

    return candidates


def _insufficient_prediction(game, reason):
    teams = game.get("teams", {})
    home_team = teams.get("home", {})
    away_team = teams.get("away", {})
    league = game.get("league", {})

    now_ts = None
    if isinstance(game, dict):
        now_ts = game.get("date")

    unc_info = uncertainty.calculate_uncertainty(
        feature_coverage=0.0,
        sample_count=0,
        top_probability=0.0,
    )

    gate_res = quality_gate.evaluate_quality_gate(
        uncertainty_info=unc_info,
        probability_valid=False,
        model_error=True,
    )

    contract = prediction_contract.build_prediction_contract(
        sport="basketball",
        fixture_id=game.get("id"),
        league_id=league.get("id", 12),
        season=league.get("season", 2024),
        raw_markets={},
        calibrated_markets={},
        calibration_metadata={
            "calibration_version": getattr(config, "CALIBRATION_VERSION", "v3.0.0"),
            "calibration_method": "NONE",
            "calibration_status": "UNAVAILABLE",
        },
        market_analysis={},
        uncertainty_info=unc_info,
        quality_gate_result=gate_res,
        data_cutoff_timestamp=now_ts,
        home_team=home_team.get("name"),
        away_team=away_team.get("name"),
        league_name=league.get("name", "NBA"),
    )

    contract["insufficient_data"] = True
    contract["reason"] = reason
    contract["confidence"] = None
    contract["safest"] = None
    return contract


def predict_game(
    game: Dict[str, Any],
    home_stats_override: Optional[Dict[str, Any]] = None,
    away_stats_override: Optional[Dict[str, Any]] = None,
    calibrator: Optional[Any] = None,
    odds_data: Optional[Dict[str, Any]] = None,
    odds_timestamp: Optional[str] = None,
    data_cutoff_timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Authoritative basketball prediction path through Phase 3 pipeline.
    """
    if not isinstance(game, dict):
        raise ValueError("game must be a dictionary.")

    teams = game.get("teams")
    if not isinstance(teams, dict):
        raise ValueError("game is missing teams.")

    home_team = teams.get("home")
    away_team = teams.get("away")
    if not isinstance(home_team, dict) or not isinstance(away_team, dict):
        raise ValueError("game is missing the home or away team.")

    league = game.get("league")
    if not isinstance(league, dict):
        raise ValueError("game is missing league information.")

    league_id = league.get("id")
    season = league.get("season")
    home_id = home_team.get("id")
    away_id = away_team.get("id")

    if isinstance(home_id, bool) or not isinstance(home_id, int) or home_id <= 0:
        raise ValueError("Invalid home team ID.")
    if isinstance(away_id, bool) or not isinstance(away_id, int) or away_id <= 0:
        raise ValueError("Invalid away team ID.")
    if isinstance(league_id, bool) or not isinstance(league_id, int) or league_id <= 0:
        raise ValueError("Invalid league ID.")
    if isinstance(season, bool) or not isinstance(season, int) or season <= 0:
        raise ValueError("Invalid season.")

    if home_stats_override is not None and away_stats_override is not None:
        home_stats = home_stats_override
        away_stats = away_stats_override
    else:
        home_stats = basketball_api.get_team_statistics(home_id, league_id, season)
        away_stats = basketball_api.get_team_statistics(away_id, league_id, season)

    home_for, home_against = _extract_scoring(home_stats)
    away_for, away_against = _extract_scoring(away_stats)

    if home_for is None or home_against is None:
        return _insufficient_prediction(game, "Missing or invalid home-team scoring statistics.")

    if away_for is None or away_against is None:
        return _insufficient_prediction(game, "Missing or invalid away-team scoring statistics.")

    home_advantage = getattr(config, "BASKETBALL_HOME_ADVANTAGE_POINTS", 3.0)
    if not _valid_number(home_advantage):
        raise ValueError("BASKETBALL_HOME_ADVANTAGE_POINTS must be a finite number.")

    expected_home = (home_for + away_against) / 2.0 + float(home_advantage)
    expected_away = (home_against + away_for) / 2.0

    if not _valid_number(expected_home) or not _valid_number(expected_away) or expected_home < 0 or expected_away < 0:
        return _insufficient_prediction(game, "Unable to construct valid expected points.")

    # 1. MODEL -> RAW PROBABILITIES
    point_diff = expected_home - expected_away
    p_home = _win_probability(point_diff)
    p_away = _validate_probability(1.0 - p_home)

    total_expected = expected_home + expected_away
    over_prob = _over_probability(total_expected, TOTAL_LINE)
    under_prob = _validate_probability(1.0 - over_prob)

    raw_markets = {
        "expected_points": {
            "home": round(expected_home, 1),
            "away": round(expected_away, 1),
        },
        "moneyline": {
            "home_win": p_home,
            "away_win": p_away,
        },
        "total_points": {
            "line": TOTAL_LINE,
            "expected_total": round(total_expected, 1),
            "over": over_prob,
            "under": under_prob,
        },
    }

    # 2. PROBABILITY VALIDATION ON RAW MARKETS
    try:
        validated_raw_markets = probability_validation.validate_all_probabilities(raw_markets, sport="basketball")
        prob_valid = True
    except probability_validation.ProbabilityValidationError as exc:
        prob_valid = False
        unc_info = uncertainty.calculate_uncertainty(
            feature_coverage=0.0,
            sample_count=0,
            top_probability=0.0,
        )
        gate_res = quality_gate.evaluate_quality_gate(
            uncertainty_info=unc_info,
            probability_valid=False,
            model_error=True,
        )
        contract = prediction_contract.build_prediction_contract(
            sport="basketball",
            fixture_id=game["id"],
            league_id=league_id,
            season=season,
            raw_markets={},
            calibrated_markets={},
            calibration_metadata={
                "calibration_version": getattr(config, "CALIBRATION_VERSION", "v3.0.0"),
                "calibration_method": "NONE",
                "calibration_status": "UNAVAILABLE",
            },
            market_analysis={},
            uncertainty_info=unc_info,
            quality_gate_result=gate_res,
            data_cutoff_timestamp=data_cutoff_timestamp or game.get("date"),
            home_team=home_team["name"],
            away_team=away_team["name"],
            league_name=league.get("name", "NBA"),
        )
        contract["status"] = "INVALID_PROBABILITY"
        contract["insufficient_data"] = True
        contract["reason"] = f"Model probability validation error: {exc}"
        return contract

    # 3. CALIBRATION
    cutoff_ts = data_cutoff_timestamp or game.get("date")
    dataset_identity = f"basketball_{league_id}_{season}"
    calibration_res = calibration.apply_calibration_layer(
        raw_markets=validated_raw_markets,
        calibrator=calibrator,
        sport="basketball",
        dataset_identity=dataset_identity,
        cutoff_timestamp=cutoff_ts,
    )
    calibrated_markets = calibration_res["calibrated_markets"]
    calib_meta = calibration_res["calibration_metadata"]

    # 3b. RE-VALIDATE CALIBRATED MARKETS
    if calib_meta["calibration_status"] == "APPLIED":
        try:
            calibrated_markets = probability_validation.validate_all_probabilities(calibrated_markets, sport="basketball")
        except probability_validation.ProbabilityValidationError as exc:
            unc_info = uncertainty.calculate_uncertainty(
                feature_coverage=0.0,
                sample_count=0,
                top_probability=0.0,
            )
            gate_res = quality_gate.evaluate_quality_gate(
                uncertainty_info=unc_info,
                probability_valid=False,
                model_error=True,
            )
            contract = prediction_contract.build_prediction_contract(
                sport="basketball",
                fixture_id=game["id"],
                league_id=league_id,
                season=season,
                raw_markets=validated_raw_markets,
                calibrated_markets={},
                calibration_metadata=calib_meta,
                market_analysis={},
                uncertainty_info=unc_info,
                quality_gate_result=gate_res,
                data_cutoff_timestamp=cutoff_ts,
                home_team=home_team["name"],
                away_team=away_team["name"],
                league_name=league.get("name", "NBA"),
            )
            contract["status"] = "INVALID_CALIBRATED_PROBABILITY"
            contract["insufficient_data"] = True
            contract["reason"] = f"Calibrated probability validation error: {exc}"
            return contract
    elif calib_meta["calibration_status"] == "ERROR_FALLBACK_RAW":
        calibrated_markets = {}

    calibrated_markets["expected_points"] = raw_markets["expected_points"]
    if "total_points" in calibrated_markets and isinstance(calibrated_markets["total_points"], dict):
        calibrated_markets["total_points"]["line"] = TOTAL_LINE
        calibrated_markets["total_points"]["expected_total"] = round(total_expected, 1)

    # 4. MARKET ANALYSIS
    m_analysis = market_analysis.analyze_market_odds(
        calibrated_markets=calibrated_markets,
        odds_data=odds_data,
        raw_markets=validated_raw_markets,
        odds_timestamp=odds_timestamp,
        cutoff_timestamp=cutoff_ts,
    )

    if calib_meta["calibration_status"] == "ERROR_FALLBACK_RAW":
        for m_key, m_val in m_analysis.items():
            if isinstance(m_val, dict):
                for o_key, o_val in m_val.items():
                    if isinstance(o_val, dict):
                        o_val["edge"] = None
                        o_val["ev"] = None

    odds_status = market_analysis.check_odds_chronology_and_staleness(odds_timestamp, cutoff_ts) if odds_data else "MISSING"

    # 5. UNCERTAINTY (Use actual historical sample counts from stats payload)
    home_matches = home_stats.get("matches", 0) if isinstance(home_stats, dict) else 0
    away_matches = away_stats.get("matches", 0) if isinstance(away_stats, dict) else 0

    if home_matches > 0 and away_matches > 0:
        samples = min(home_matches, away_matches)
    else:
        samples = home_matches or away_matches or 0

    ml = calibrated_markets["moneyline"]
    top_p = max(ml.values())
    unc_info = uncertainty.calculate_uncertainty(
        feature_coverage=1.0 if samples >= 5 else (samples / 5.0),
        sample_count=samples,
        top_probability=top_p,
        calibration_status=calib_meta["calibration_status"],
        odds_status=odds_status,
    )

    # 6. QUALITY GATE
    top_key = "home_win" if ml["home_win"] >= ml["away_win"] else "away_win"
    top_analysis = m_analysis.get("moneyline", {}).get(top_key, {})
    edge_val = top_analysis.get("edge")
    ev_val = top_analysis.get("ev")

    gate_res = quality_gate.evaluate_quality_gate(
        uncertainty_info=unc_info,
        probability_valid=prob_valid,
        edge=edge_val,
        ev=ev_val,
        odds_status=odds_status,
        calibration_status=calib_meta["calibration_status"],
        model_error=(not prob_valid),
    )

    conf = confidence.confidence_flag(calibrated_markets["moneyline"])
    safest = confidence.safest_pick(build_basketball_safest_candidates(calibrated_markets))

    # 7. STANDARDIZED CONTRACT OUTPUT
    contract = prediction_contract.build_prediction_contract(
        sport="basketball",
        fixture_id=game["id"],
        league_id=league_id,
        season=season,
        raw_markets=validated_raw_markets,
        calibrated_markets=calibrated_markets,
        calibration_metadata=calib_meta,
        market_analysis=m_analysis,
        uncertainty_info=unc_info,
        quality_gate_result=gate_res,
        data_cutoff_timestamp=cutoff_ts,
        home_team=home_team["name"],
        away_team=away_team["name"],
        league_name=league.get("name", "NBA"),
        additional_metadata={
            "confidence": conf,
            "safest": safest,
        },
    )

    if calib_meta["calibration_status"] == "ERROR_FALLBACK_RAW":
        contract["status"] = "CALIBRATION_ERROR"

    return contract


def print_prediction(pred):
    if not isinstance(pred, dict):
        raise ValueError("Prediction must be a dictionary.")

    if pred.get("insufficient_data"):
        print(f"\n{pred.get('home_team')} vs {pred.get('away_team')} ({pred.get('league')})")
        print(f"  Prediction unavailable: {pred.get('reason')}")
        return

    m = pred["markets"]
    c = pred.get("confidence") or confidence.confidence_flag(m["moneyline"])
    s = pred.get("safest") or confidence.safest_pick(build_basketball_safest_candidates(m))

    print(f"\n{pred['home_team']} vs {pred['away_team']} ({pred['league']})")
    print(f"  Expected points: {m['expected_points']['home']} - {m['expected_points']['away']}")
    print(f"  Moneyline:       Home {m['moneyline']['home_win']:.0%} | Away {m['moneyline']['away_win']:.0%}")
    print(f"  Total points:    Line {m['total_points']['line']} | Expected {m['total_points']['expected_total']} | Over {m['total_points']['over']:.0%} | Under {m['total_points']['under']:.0%}")
    print(f"  Confidence:      {c['emoji']} {c['label']} (pick: {c['top_pick']}, {c['top_probability']:.0%})")

    if s:
        print(f"  >>> SAFEST PICK: {s['label']} ({s['probability']:.0%}) <<<")
