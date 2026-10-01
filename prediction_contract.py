"""
Standardized Prediction Result Contract for Football and Basketball predictions.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import config


def build_prediction_contract(
    sport: str,
    fixture_id: Any,
    league_id: int,
    season: int,
    raw_markets: Dict[str, Any],
    calibrated_markets: Dict[str, Any],
    calibration_metadata: Dict[str, Any],
    market_analysis: Dict[str, Any],
    uncertainty_info: Dict[str, Any],
    quality_gate_result: Dict[str, Any],
    data_cutoff_timestamp: Optional[str] = None,
    prediction_timestamp: Optional[str] = None,
    home_team: Optional[str] = None,
    away_team: Optional[str] = None,
    league_name: Optional[str] = None,
    additional_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Build the authoritative Phase 3 prediction contract output.
    """
    sport_clean = str(sport).lower()
    now_ts = prediction_timestamp or data_cutoff_timestamp or datetime.now(timezone.utc).isoformat()
    cutoff_ts = data_cutoff_timestamp or now_ts

    # Extract top pick across markets for selected_market summary
    top_outcome = None
    top_raw_p = None
    top_cal_p = None
    top_odds = None
    top_implied_p = None
    top_edge = None
    top_ev = None

    if sport_clean == "football":
        mr = calibrated_markets.get("match_result", {})
        if isinstance(mr, dict) and mr:
            top_key = max(mr, key=mr.get)
            top_outcome = top_key
            top_cal_p = mr.get(top_key)
            top_raw_p = raw_markets.get("match_result", {}).get(top_key) if isinstance(raw_markets.get("match_result"), dict) else None

            analysis_item = market_analysis.get("match_result", {}).get(top_key, {})
            if isinstance(analysis_item, dict):
                top_odds = analysis_item.get("odds")
                top_implied_p = analysis_item.get("implied_probability")
                top_edge = analysis_item.get("edge")
                top_ev = analysis_item.get("ev")

    elif sport_clean == "basketball":
        ml = calibrated_markets.get("moneyline", {})
        if isinstance(ml, dict) and ml:
            top_key = max(ml, key=ml.get)
            top_outcome = top_key
            top_cal_p = ml.get(top_key)
            top_raw_p = raw_markets.get("moneyline", {}).get(top_key) if isinstance(raw_markets.get("moneyline"), dict) else None

            analysis_item = market_analysis.get("moneyline", {}).get(top_key, {})
            if isinstance(analysis_item, dict):
                top_odds = analysis_item.get("odds")
                top_implied_p = analysis_item.get("implied_probability")
                top_edge = analysis_item.get("edge")
                top_ev = analysis_item.get("ev")

    contract = {
        "sport": sport_clean,
        "fixture_id": fixture_id,
        "game_id": fixture_id,
        "league_id": league_id,
        "season": season,
        "home_team": home_team,
        "away_team": away_team,
        "league": league_name,
        "prediction_timestamp": now_ts,
        "data_cutoff_timestamp": cutoff_ts,
        "model_version": getattr(config, "MODEL_VERSION", "v3.0.0"),
        "feature_version": getattr(config, "FEATURE_VERSION", "v3.0.0"),
        "calibration_version": getattr(config, "CALIBRATION_VERSION", "v3.0.0"),
        "raw_probabilities": raw_markets,
        "calibrated_probabilities": calibrated_markets,
        "markets": calibrated_markets,  # Backward compatibility
        "calibration_metadata": calibration_metadata,
        "market_analysis": market_analysis,
        "selected_market": {
            "outcome": top_outcome,
            "raw_probability": top_raw_p,
            "calibrated_probability": top_cal_p,
        },
        "odds": top_odds,
        "implied_probability": top_implied_p,
        "edge": top_edge,
        "ev": top_ev,
        "uncertainty": uncertainty_info,
        "quality_gate": quality_gate_result.get("decision", "PASS"),
        "reason_codes": quality_gate_result.get("reason_codes", []),
        "feature_data_coverage": uncertainty_info.get("feature_coverage", 1.0),
        "status": "VALID",
        "insufficient_data": (quality_gate_result.get("decision") == "PASS" and "insufficient_data" in quality_gate_result.get("reason_codes", [])),
    }

    if additional_metadata and isinstance(additional_metadata, dict):
        for k, v in additional_metadata.items():
            if k not in contract:
                contract[k] = v

    return contract
