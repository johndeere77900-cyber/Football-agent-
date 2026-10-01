"""
Probability validation layer for Football and Basketball prediction models.

Enforces strict rules on model probability outputs:
- Rejects missing values, NaN, infinity, negative values, and values > 1.0.
- Rejects booleans disguised as numbers.
- Checks completeness and validity of outcome probability distributions.
- Deterministically normalizes distributions only when raw values are valid and close to 1.0.
- Rejects mathematically broken outputs and impossible market combinations (fails closed).
"""

import math
from typing import Any, Dict, Optional, Set


class ProbabilityValidationError(ValueError):
    """Raised when a probability or probability distribution fails validation."""
    pass


def validate_single_probability(val: Any, name: str = "probability") -> float:
    """
    Validate that val is a valid, finite probability in the range [0.0, 1.0].

    Rejects:
    - None / missing values
    - Booleans
    - Non-numeric types
    - NaN or Infinity
    - Values < 0.0 or > 1.0
    """
    if val is None:
        raise ProbabilityValidationError(f"Missing value for {name}.")

    if isinstance(val, bool):
        raise ProbabilityValidationError(f"Boolean value provided for {name}.")

    try:
        prob = float(val)
    except (TypeError, ValueError) as exc:
        raise ProbabilityValidationError(f"Non-numeric value provided for {name}: {val!r}") from exc

    if not math.isfinite(prob):
        raise ProbabilityValidationError(f"Non-finite probability for {name}: {prob}")

    if prob < 0.0 or prob > 1.0:
        raise ProbabilityValidationError(f"Probability for {name} out of bounds [0, 1]: {prob}")

    return prob


def validate_and_normalize_distribution(
    distribution: Dict[str, Any],
    required_keys: Optional[Set[str]] = None,
    tolerance: float = 0.2,
) -> Dict[str, float]:
    """
    Validate and deterministically normalize a probability distribution.

    Requirements:
    - distribution must be a non-empty dict.
    - If required_keys is specified, all required keys must be present.
    - All values must pass validate_single_probability.
    - Sum of probabilities must be within [1.0 - tolerance, 1.0 + tolerance].
      If the sum deviates more than tolerance, the model output is considered broken and fails closed.
    """
    if not isinstance(distribution, dict) or not distribution:
        raise ProbabilityValidationError("Distribution must be a non-empty dictionary.")

    if required_keys is not None:
        missing = set(required_keys) - set(distribution.keys())
        if missing:
            raise ProbabilityValidationError(f"Distribution is missing required keys: {sorted(missing)}")

    validated: Dict[str, float] = {}
    for key, value in distribution.items():
        validated[str(key)] = validate_single_probability(value, name=f"distribution[{key}]")

    total = sum(validated.values())

    if total <= 0.0 or abs(total - 1.0) > tolerance:
        raise ProbabilityValidationError(
            f"Distribution sum {total:.4f} deviates significantly from 1.0 (tolerance {tolerance})."
        )

    # Deterministic normalization
    return {k: v / total for k, v in validated.items()}


def validate_market_combinations(markets: Dict[str, Any], sport: str = "football") -> bool:
    """
    Validate that market probabilities within a prediction payload are logically consistent.

    Fails closed (raises ProbabilityValidationError) on impossible market combinations.
    """
    if not isinstance(markets, dict):
        raise ProbabilityValidationError("Markets must be a dictionary.")

    sport_clean = str(sport).lower()

    if sport_clean == "football":
        # 1X2 Consistency check
        match_result = markets.get("match_result")
        double_chance = markets.get("double_chance")

        if isinstance(match_result, dict) and isinstance(double_chance, dict):
            p_home = match_result.get("home_win")
            p_draw = match_result.get("draw")
            p_away = match_result.get("away_win")

            if all(v is not None for v in (p_home, p_draw, p_away)):
                p_hd = double_chance.get("home_or_draw")
                p_ad = double_chance.get("away_or_draw")
                p_ha = double_chance.get("home_or_away")

                if p_hd is not None and abs(p_hd - (p_home + p_draw)) > 0.05:
                    raise ProbabilityValidationError("Inconsistent Double Chance (home_or_draw vs 1X2).")

                if p_ad is not None and abs(p_ad - (p_away + p_draw)) > 0.05:
                    raise ProbabilityValidationError("Inconsistent Double Chance (away_or_draw vs 1X2).")

                if p_ha is not None and abs(p_ha - (p_home + p_away)) > 0.05:
                    raise ProbabilityValidationError("Inconsistent Double Chance (home_or_away vs 1X2).")

        # Over/Under monotonicity check
        ou = markets.get("over_under")
        if isinstance(ou, dict):
            o15 = ou.get("over_1_5")
            o25 = ou.get("over_2_5")
            o35 = ou.get("over_3_5")
            o45 = ou.get("over_4_5")

            if o15 is not None and o25 is not None and o15 < o25 - 1e-6:
                raise ProbabilityValidationError(f"Impossible Totals monotonicity: Over 1.5 ({o15}) < Over 2.5 ({o25}).")

            if o25 is not None and o35 is not None and o25 < o35 - 1e-6:
                raise ProbabilityValidationError(f"Impossible Totals monotonicity: Over 2.5 ({o25}) < Over 3.5 ({o35}).")

            if o35 is not None and o45 is not None and o35 < o45 - 1e-6:
                raise ProbabilityValidationError(f"Impossible Totals monotonicity: Over 3.5 ({o35}) < Over 4.5 ({o45}).")

    elif sport_clean == "basketball":
        moneyline = markets.get("moneyline")
        if isinstance(moneyline, dict):
            hw = moneyline.get("home_win")
            aw = moneyline.get("away_win")
            if hw is not None and aw is not None:
                if abs((hw + aw) - 1.0) > 0.05:
                    raise ProbabilityValidationError(f"Basketball moneyline sum ({hw + aw}) invalid.")

        total_pts = markets.get("total_points")
        if isinstance(total_pts, dict):
            over_p = total_pts.get("over")
            under_p = total_pts.get("under")
            if over_p is not None and under_p is not None:
                if abs((over_p + under_p) - 1.0) > 0.05:
                    raise ProbabilityValidationError(f"Basketball total points sum ({over_p + under_p}) invalid.")

    return True


def validate_all_probabilities(markets: Dict[str, Any], sport: str = "football") -> Dict[str, Any]:
    """
    Validate every market distribution in markets dictionary.

    Returns normalized/validated markets dict. Raises ProbabilityValidationError on failure.
    """
    if not isinstance(markets, dict):
        raise ProbabilityValidationError("Markets payload must be a dictionary.")

    validated_markets = dict(markets)
    sport_clean = str(sport).lower()

    if sport_clean == "football":
        if "match_result" in validated_markets and isinstance(validated_markets["match_result"], dict):
            validated_markets["match_result"] = validate_and_normalize_distribution(
                validated_markets["match_result"],
                required_keys={"home_win", "draw", "away_win"},
            )

        if "btts" in validated_markets and isinstance(validated_markets["btts"], dict):
            validated_markets["btts"] = validate_and_normalize_distribution(
                validated_markets["btts"],
                required_keys={"yes", "no"},
            )

        if "over_under" in validated_markets and isinstance(validated_markets["over_under"], dict):
            ou_dict = validated_markets["over_under"]
            for pair in (("over_1_5", "under_1_5"), ("over_2_5", "under_2_5"), ("over_3_5", "under_3_5"), ("over_4_5", "under_4_5")):
                if pair[0] in ou_dict and pair[1] in ou_dict:
                    sub_dist = {pair[0]: ou_dict[pair[0]], pair[1]: ou_dict[pair[1]]}
                    norm_sub = validate_and_normalize_distribution(sub_dist)
                    ou_dict[pair[0]] = norm_sub[pair[0]]
                    ou_dict[pair[1]] = norm_sub[pair[1]]

    elif sport_clean == "basketball":
        if "moneyline" in validated_markets and isinstance(validated_markets["moneyline"], dict):
            validated_markets["moneyline"] = validate_and_normalize_distribution(
                validated_markets["moneyline"],
                required_keys={"home_win", "away_win"},
            )

        if "total_points" in validated_markets and isinstance(validated_markets["total_points"], dict):
            tp_dict = dict(validated_markets["total_points"])
            if "over" in tp_dict and "under" in tp_dict:
                sub_dist = {"over": tp_dict["over"], "under": tp_dict["under"]}
                norm_sub = validate_and_normalize_distribution(sub_dist)
                tp_dict["over"] = norm_sub["over"]
                tp_dict["under"] = norm_sub["under"]
                validated_markets["total_points"] = tp_dict

    validate_market_combinations(validated_markets, sport=sport_clean)

    return validated_markets
