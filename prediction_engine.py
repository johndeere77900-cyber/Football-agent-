"""
Authoritative football prediction engine.

This module contains the shared mathematical prediction path used by
production prediction and chronological historical backtesting.

Pipeline:
DATA → VALIDATION → POINT-IN-TIME FEATURES → MODEL → RAW PROBABILITIES → PROBABILITY VALIDATION → CALIBRATION → CALIBRATED VALIDATION → MARKET PROBABILITIES → ODDS / IMPLIED PROBABILITY → EDGE / EV → UNCERTAINTY → QUALITY GATE → SIGNAL / PASS → PERSISTENCE
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import calibration
import config
import elo
import market_analysis
import poisson_model
import prediction_contract
import probability_validation
import quality_gate
import uncertainty


def _safe_ratio(value: Any, denominator: float) -> Optional[float]:
    """Return value / denominator when both values are valid."""
    if denominator is None or denominator <= 0:
        return None

    if isinstance(value, bool):
        return None

    try:
        value = float(value)
    except (TypeError, ValueError):
        return None

    return value / denominator


def _normalise_market_probabilities(markets: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return a defensive copy of market probabilities.
    """
    if not isinstance(markets, dict):
        raise ValueError("markets must be a dictionary.")

    return dict(markets)


def blend_signal(
    season_ratio: float,
    recent_ratio: float,
    h2h_ratio: float,
) -> float:
    """
    Apply the configured season/recent/H2H weighting.
    """
    return (
        season_ratio * config.SEASON_WEIGHT
        + recent_ratio * config.RECENT_FORM_WEIGHT
        + h2h_ratio * config.HEAD_TO_HEAD_WEIGHT
    )


def blend_elo_match_result(
    match_result: Dict[str, float],
    elo_probabilities: Dict[str, float],
    weight: float,
) -> Dict[str, float]:
    """
    Blend Poisson 1X2 probabilities with Elo probabilities.
    """
    if not 0.0 <= weight <= 1.0:
        raise ValueError("Elo blend weight must be between 0 and 1.")

    required_match_keys = {
        "home_win",
        "draw",
        "away_win",
    }

    required_elo_keys = {
        "home",
        "draw",
        "away",
    }

    if not required_match_keys.issubset(match_result):
        raise ValueError("Incomplete match-result probability distribution.")

    if not required_elo_keys.issubset(elo_probabilities):
        raise ValueError("Incomplete Elo probability distribution.")

    blended = {
        "home_win": (
            match_result["home_win"] * (1.0 - weight)
            + elo_probabilities["home"] * weight
        ),
        "draw": (
            match_result["draw"] * (1.0 - weight)
            + elo_probabilities["draw"] * weight
        ),
        "away_win": (
            match_result["away_win"] * (1.0 - weight)
            + elo_probabilities["away"] * weight
        ),
    }

    total = sum(blended.values())

    if total <= 0:
        raise ValueError("Blended match-result probabilities are invalid.")

    return {
        key: value / total
        for key, value in blended.items()
    }


def derive_double_chance(
    match_result: Dict[str, float],
) -> Dict[str, float]:
    """
    Derive Double Chance probabilities from the authoritative 1X2 result.
    """
    required = {"home_win", "draw", "away_win"}

    if not required.issubset(match_result):
        raise ValueError("Incomplete match-result distribution.")

    return {
        "home_or_draw": (
            match_result["home_win"]
            + match_result["draw"]
        ),
        "away_or_draw": (
            match_result["away_win"]
            + match_result["draw"]
        ),
        "home_or_away": (
            match_result["home_win"]
            + match_result["away_win"]
        ),
    }


def _validate_team_feature(feature: Dict[str, Any], name: str) -> None:
    """Validate a historical team feature snapshot."""
    if not isinstance(feature, dict):
        raise ValueError(f"{name} must be a dictionary.")

    matches = feature.get("matches")

    if matches is None:
        raise ValueError(f"{name} is missing matches.")

    if not isinstance(matches, int) or isinstance(matches, bool):
        raise ValueError(f"{name}.matches must be an integer.")

    if matches <= 0:
        raise ValueError(f"{name}.matches must be positive.")

    for key in ("goals_for", "goals_against"):
        value = feature.get(key)

        if isinstance(value, bool):
            raise ValueError(f"{name}.{key} is invalid.")

        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name}.{key} is invalid.")

        if value < 0:
            raise ValueError(f"{name}.{key} cannot be negative.")


def _historical_team_ratios(
    team_feature: Dict[str, Any],
    league_avg_goals: float,
) -> tuple[float, float]:
    """
    Convert historical team goal averages into attack/defence ratios.
    """
    _validate_team_feature(
        team_feature,
        "team_feature",
    )

    if league_avg_goals <= 0:
        raise ValueError(
            "league_avg_goals must be positive."
        )

    attack = _safe_ratio(
        team_feature["goals_for"],
        league_avg_goals,
    )

    defence = _safe_ratio(
        team_feature["goals_against"],
        league_avg_goals,
    )

    if attack is None or defence is None:
        raise ValueError(
            "Historical team goal features are invalid."
        )

    return attack, defence


def build_historical_features(
    historical_snapshot: Dict[str, Any],
    recent_snapshot: Dict[str, Any],
    h2h_snapshot: Optional[Dict[str, Any]],
    league_avg_goals: float,
) -> Dict[str, Any]:
    """
    Convert historical feature snapshots into ratios required by model.
    """
    if not isinstance(historical_snapshot, dict):
        raise ValueError(
            "historical_snapshot must be a dictionary."
        )

    if not isinstance(recent_snapshot, dict):
        raise ValueError(
            "recent_snapshot must be a dictionary."
        )

    if league_avg_goals <= 0:
        raise ValueError(
            "league_avg_goals must be positive."
        )

    season_home = historical_snapshot.get("home")
    season_away = historical_snapshot.get("away")

    recent_home = recent_snapshot.get("home")
    recent_away = recent_snapshot.get("away")

    if not isinstance(season_home, dict):
        raise ValueError("Historical home feature is missing.")

    if not isinstance(season_away, dict):
        raise ValueError("Historical away feature is missing.")

    if not isinstance(recent_home, dict):
        raise ValueError("Recent-form home feature is missing.")

    if not isinstance(recent_away, dict):
        raise ValueError("Recent-form away feature is missing.")

    season_home_attack, season_home_defence = (
        _historical_team_ratios(
            season_home,
            league_avg_goals,
        )
    )

    season_away_attack, season_away_defence = (
        _historical_team_ratios(
            season_away,
            league_avg_goals,
        )
    )

    recent_home_attack, recent_home_defence = (
        _historical_team_ratios(
            recent_home,
            league_avg_goals,
        )
    )

    recent_away_attack, recent_away_defence = (
        _historical_team_ratios(
            recent_away,
            league_avg_goals,
        )
    )

    h2h_home_attack = 1.0
    h2h_home_defence = 1.0
    h2h_away_attack = 1.0
    h2h_away_defence = 1.0

    h2h_available = False

    if isinstance(h2h_snapshot, dict):
        meetings = h2h_snapshot.get("meetings", 0)

        if isinstance(meetings, int) and meetings > 0:
            h2h_home_attack = _safe_ratio(
                h2h_snapshot.get("goals_for"),
                league_avg_goals,
            )

            h2h_home_defence = _safe_ratio(
                h2h_snapshot.get("goals_against"),
                league_avg_goals,
            )

            h2h_away_attack = _safe_ratio(
                h2h_snapshot.get("goals_against"),
                league_avg_goals,
            )

            h2h_away_defence = _safe_ratio(
                h2h_snapshot.get("goals_for"),
                league_avg_goals,
            )

            if all(
                value is not None
                for value in (
                    h2h_home_attack,
                    h2h_home_defence,
                    h2h_away_attack,
                    h2h_away_defence,
                )
            ):
                h2h_available = True

    home_attack = blend_signal(
        season_home_attack,
        recent_home_attack,
        h2h_home_attack,
    )

    home_defence = blend_signal(
        season_home_defence,
        recent_home_defence,
        h2h_home_defence,
    )

    away_attack = blend_signal(
        season_away_attack,
        recent_away_attack,
        h2h_away_attack,
    )

    away_defence = blend_signal(
        season_away_defence,
        recent_away_defence,
        h2h_away_defence,
    )

    feature_coverage = 1.0 if h2h_available else 0.85

    recent_form_scope = "8_matches"
    h2h_scope = "6_meetings" if h2h_available else "0_meetings"

    return {
        "home_attack": home_attack,
        "home_defence": home_defence,
        "away_attack": away_attack,
        "away_defence": away_defence,
        "league_avg_goals": league_avg_goals,
        "h2h_available": h2h_available,
        "feature_coverage": feature_coverage,
        "recent_form_scope": recent_form_scope,
        "h2h_scope": h2h_scope,
    }


def predict_from_features(
    features: Dict[str, Any],
    elo_probabilities: Optional[Dict[str, float]] = None,
    elo_weight: Optional[float] = None,
    calibrator: Optional[Any] = None,
    odds_data: Optional[Dict[str, Any]] = None,
    odds_timestamp: Optional[str] = None,
    data_cutoff_timestamp: Optional[str] = None,
    prediction_timestamp: Optional[str] = None,
    fixture_id: Optional[Any] = None,
    league_id: Optional[int] = None,
    season: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Authoritative football prediction execution through Phase 3 pipeline.
    """
    if not isinstance(features, dict):
        raise ValueError("features must be a dictionary.")

    required = {
        "home_attack",
        "home_defence",
        "away_attack",
        "away_defence",
        "league_avg_goals",
    }

    if not required.issubset(features):
        missing = sorted(required - set(features))
        raise ValueError(
            f"Missing prediction features: {', '.join(missing)}"
        )

    league_avg_goals = float(
        features["league_avg_goals"]
    )

    if league_avg_goals <= 0:
        raise ValueError(
            "league_avg_goals must be positive."
        )

    if prediction_timestamp is None and data_cutoff_timestamp is None:
        data_cutoff_timestamp = features.get("cutoff_timestamp")

    if prediction_timestamp is None and data_cutoff_timestamp is None:
        raise ValueError("Authoritative prediction_timestamp or data_cutoff_timestamp must be provided.")

    if prediction_timestamp is None:
        prediction_timestamp = data_cutoff_timestamp
    if data_cutoff_timestamp is None:
        data_cutoff_timestamp = prediction_timestamp

    from time_utils import parse_utc_datetime, format_utc_iso
    dt_pred = parse_utc_datetime(prediction_timestamp)
    dt_cutoff = parse_utc_datetime(data_cutoff_timestamp)

    if dt_pred is None or dt_cutoff is None:
        raise ValueError(
            f"Invalid or timezone-naive timestamp: prediction_timestamp={prediction_timestamp!r}, data_cutoff_timestamp={data_cutoff_timestamp!r}"
        )

    prediction_timestamp = format_utc_iso(dt_pred)
    data_cutoff_timestamp = format_utc_iso(dt_cutoff)

    # 1. MODEL -> RAW PROBABILITIES
    home_xg = poisson_model.expected_goals(
        features["home_attack"],
        features["away_defence"],
        league_avg_goals,
        is_home=True,
    )

    away_xg = poisson_model.expected_goals(
        features["away_attack"],
        features["home_defence"],
        league_avg_goals,
        is_home=False,
    )

    raw_markets = poisson_model.market_probabilities(
        home_xg,
        away_xg,
    )

    raw_markets = _normalise_market_probabilities(
        raw_markets
    )

    if elo_probabilities is not None:
        if elo_weight is None:
            elo_weight = config.ELO_BLEND_WEIGHT

        raw_markets["match_result"] = blend_elo_match_result(
            raw_markets["match_result"],
            elo_probabilities,
            elo_weight,
        )

        raw_markets["double_chance"] = derive_double_chance(
            raw_markets["match_result"]
        )

    else:
        raw_markets["double_chance"] = derive_double_chance(
            raw_markets["match_result"]
        )

    # 2. PROBABILITY VALIDATION
    try:
        validated_raw_markets = probability_validation.validate_all_probabilities(
            raw_markets,
            sport="football",
        )
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
            sport="football",
            fixture_id=fixture_id,
            league_id=league_id or 0,
            season=season or 0,
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
            data_cutoff_timestamp=data_cutoff_timestamp,
            prediction_timestamp=prediction_timestamp,
        )
        contract["status"] = "INVALID_PROBABILITY"
        contract["insufficient_data"] = True
        contract["reason"] = f"Model probability validation error: {exc}"
        return contract

    # 3. CALIBRATION
    dataset_identity = f"football_{league_id or 'all'}_{season or 'all'}"
    calibration_res = calibration.apply_calibration_layer(
        raw_markets=validated_raw_markets,
        calibrator=calibrator,
        sport="football",
        dataset_identity=dataset_identity,
        cutoff_timestamp=data_cutoff_timestamp,
        prediction_timestamp=prediction_timestamp,
    )

    calibrated_markets = calibration_res["calibrated_markets"]
    calib_meta = calibration_res["calibration_metadata"]

    # 3b. RE-VALIDATE CALIBRATED MARKETS
    if calib_meta["calibration_status"] == "APPLIED":
        try:
            calibrated_markets = probability_validation.validate_all_probabilities(
                calibrated_markets,
                sport="football",
            )
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
                sport="football",
                fixture_id=fixture_id,
                league_id=league_id or 0,
                season=season or 0,
                raw_markets=validated_raw_markets,
                calibrated_markets={},
                calibration_metadata=calib_meta,
                market_analysis={},
                uncertainty_info=unc_info,
                quality_gate_result=gate_res,
                data_cutoff_timestamp=data_cutoff_timestamp,
                prediction_timestamp=prediction_timestamp,
            )
            contract["status"] = "INVALID_CALIBRATED_PROBABILITY"
            contract["insufficient_data"] = True
            contract["reason"] = f"Calibrated probability validation error: {exc}"
            return contract
    elif calib_meta["calibration_status"] == "ERROR":
        calibrated_markets = {}

    # 4. MARKET ANALYSIS (ODDS / IMPLIED / EDGE / EV)
    m_analysis = market_analysis.analyze_market_odds(
        calibrated_markets=calibrated_markets,
        odds_data=odds_data,
        raw_markets=validated_raw_markets,
        odds_timestamp=odds_timestamp,
        cutoff_timestamp=data_cutoff_timestamp,
    )

    if calib_meta["calibration_status"] == "ERROR":
        for m_key, m_val in m_analysis.items():
            if isinstance(m_val, dict):
                for o_key, o_val in m_val.items():
                    if isinstance(o_val, dict):
                        o_val["edge"] = None
                        o_val["ev"] = None

    odds_status = market_analysis.check_odds_chronology_and_staleness(odds_timestamp, data_cutoff_timestamp) if odds_data else "MISSING"

    # 5. UNCERTAINTY
    if calib_meta["calibration_status"] == "APPLIED" and isinstance(calibrated_markets.get("match_result"), dict) and calibrated_markets.get("match_result"):
        mr = calibrated_markets["match_result"]
        top_p = max(mr.values())
    elif isinstance(validated_raw_markets.get("match_result"), dict) and validated_raw_markets.get("match_result"):
        mr = validated_raw_markets["match_result"]
        top_p = max(mr.values())
    else:
        top_p = 0.0

    feature_coverage = features.get("feature_coverage")
    sample_count = features.get("sample_count", 0)

    unc_info = uncertainty.calculate_uncertainty(
        feature_coverage=feature_coverage,
        sample_count=sample_count,
        top_probability=top_p,
        calibration_status=calib_meta["calibration_status"],
        odds_status=odds_status,
    )

    # 6. QUALITY GATE
    mr_gate = calibrated_markets.get("match_result") or validated_raw_markets.get("match_result", {})
    top_key = max(mr_gate, key=mr_gate.get) if isinstance(mr_gate, dict) and mr_gate else "home_win"
    top_analysis = m_analysis.get("match_result", {}).get(top_key, {})
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

    # 7. STANDARDIZED CONTRACT OUTPUT
    res = prediction_contract.build_prediction_contract(
        sport="football",
        fixture_id=fixture_id,
        league_id=league_id or 0,
        season=season or 0,
        raw_markets=validated_raw_markets,
        calibrated_markets=calibrated_markets,
        calibration_metadata=calib_meta,
        market_analysis=m_analysis,
        uncertainty_info=unc_info,
        quality_gate_result=gate_res,
        data_cutoff_timestamp=data_cutoff_timestamp,
        prediction_timestamp=prediction_timestamp,
        additional_metadata={
            "expected_goals": {
                "home": home_xg,
                "away": away_xg,
            },
            "features": dict(features),
            "elo_probabilities": (
                dict(elo_probabilities)
                if elo_probabilities is not None
                else None
            ),
            "elo_weight": (
                config.ELO_BLEND_WEIGHT
                if elo_probabilities is not None
                and elo_weight is None
                else elo_weight
            ),
        },
    )

    if calib_meta["calibration_status"] == "ERROR":
        res["status"] = "CALIBRATION_ERROR"

    return res


def predict_historical_fixture(
    historical_snapshot: Dict[str, Any],
    recent_snapshot: Dict[str, Any],
    h2h_snapshot: Optional[Dict[str, Any]],
    league_avg_goals: float,
    home_elo: Optional[float] = None,
    away_elo: Optional[float] = None,
    elo_weight: Optional[float] = None,
    calibrator: Optional[Any] = None,
    odds_data: Optional[Dict[str, Any]] = None,
    odds_timestamp: Optional[str] = None,
    data_cutoff_timestamp: Optional[str] = None,
    prediction_timestamp: Optional[str] = None,
    fixture_id: Optional[Any] = None,
    league_id: Optional[int] = None,
    season: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Complete leakage-safe prediction path for a historical fixture.
    """
    features = build_historical_features(
        historical_snapshot=historical_snapshot,
        recent_snapshot=recent_snapshot,
        h2h_snapshot=h2h_snapshot,
        league_avg_goals=league_avg_goals,
    )

    home_matches = historical_snapshot.get("home", {}).get("matches", 0) if isinstance(historical_snapshot, dict) else 0
    away_matches = historical_snapshot.get("away", {}).get("matches", 0) if isinstance(historical_snapshot, dict) else 0
    features["sample_count"] = min(home_matches, away_matches) if (home_matches and away_matches) else (home_matches or away_matches or 0)

    elo_probabilities = None

    if home_elo is not None or away_elo is not None:
        if home_elo is None or away_elo is None:
            raise ValueError(
                "Both home_elo and away_elo must be supplied together."
            )

        elo_probabilities = elo.win_draw_loss_probabilities(
            home_elo,
            away_elo,
        )

    return predict_from_features(
        features,
        elo_probabilities=elo_probabilities,
        elo_weight=elo_weight,
        calibrator=calibrator,
        odds_data=odds_data,
        odds_timestamp=odds_timestamp,
        data_cutoff_timestamp=data_cutoff_timestamp,
        prediction_timestamp=prediction_timestamp,
        fixture_id=fixture_id,
        league_id=league_id,
        season=season,
    )
