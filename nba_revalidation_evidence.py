"""Fail-closed evidence checks for persisted ESPN NBA monthly coverage.

The persisted manifest is an assertion. Revalidation must prove that its monthly
labels are the season's exact provider-native acquisition units and that every
accepted, rejected and non-final event is accounted for before it can promote a
season. Historical ``event_not_final`` rejections remain unresolved until a new
provider observation establishes their outcome; this module never invents one.
"""

import espn_basketball_provider


def _non_negative_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def basketball_espn_month_evidence(season, entry):
    """Return validated evidence for one persisted ESPN provider entry.

    Month keys are derived from ``get_season_month_offsets()`` so the 2019-20
    13-month and 2020-21 8-month seasons are checked against their real
    contracts. Missing, duplicated, unexpected or malformed units fail closed.
    Rejection counters and reason breakdowns must be internally consistent.
    """
    entry = entry if isinstance(entry, dict) else {}
    try:
        season_start = int(season)
    except (TypeError, ValueError):
        season_start = None

    expected_keys = []
    if season_start is not None:
        for year_offset, month in espn_basketball_provider.get_season_month_offsets(season_start):
            try:
                expected_keys.append(f"{season_start + int(year_offset)}{int(month):02d}")
            except (TypeError, ValueError):
                expected_keys = []
                break

    raw_details = entry.get("monthly_details")
    details = raw_details if isinstance(raw_details, list) else []
    actual_keys = []
    duplicate_counts = {}
    month_rows = []
    month_key_issues = []
    rejection_issues = []
    month_units_valid = bool(expected_keys)
    rejection_evidence_consistent = True
    total_rejected = 0
    total_accepted = 0
    total_non_final_events = 0
    total_historical_non_final_rejections = 0
    total_other_rejected = 0
    monthly_rejected_totals = {}
    rejection_reasons_by_month = {}
    success_count = 0
    verified_count = 0
    valid_empty_units = 0

    for index, detail in enumerate(details):
        if not isinstance(detail, dict):
            month_units_valid = False
            month_key_issues.append(f"detail_{index}_not_object")
            month_rows.append({"index": index, "month": None, "valid": False})
            continue

        month = detail.get("month")
        month_valid = isinstance(month, str) and bool(month.strip())
        if month_valid:
            actual_keys.append(month)
            duplicate_counts[month] = duplicate_counts.get(month, 0) + 1
        else:
            month_units_valid = False
            month_key_issues.append(f"detail_{index}_invalid_month")

        status_ok = detail.get("status") == "SUCCESS"
        competition_ok = detail.get("competition_verified") is True
        event_count = detail.get("event_count")
        event_count_valid = _non_negative_int(event_count)
        if status_ok:
            success_count += 1
        if status_ok and competition_ok:
            verified_count += 1
        if not (status_ok and competition_ok and event_count_valid):
            month_units_valid = False
        if status_ok and event_count_valid and event_count == 0:
            valid_empty_units += 1

        accepted = detail.get("events_accepted")
        accepted_valid = _non_negative_int(accepted)
        rejected = detail.get("events_rejected")
        rejected_valid = _non_negative_int(rejected)
        non_final_present = "events_non_final" in detail
        non_final = detail.get("events_non_final")
        non_final_valid = (not non_final_present) or _non_negative_int(non_final)
        reasons = detail.get("rejection_reasons")
        reasons_valid = reasons is None or isinstance(reasons, dict)
        normalized_reasons = {}
        if isinstance(reasons, dict):
            for reason, count in reasons.items():
                if (
                    not isinstance(reason, str)
                    or not reason.strip()
                    or not _non_negative_int(count)
                    or count <= 0
                ):
                    reasons_valid = False
                    break
                normalized_reasons[reason] = count
        elif reasons is not None:
            reasons_valid = False

        detail_rejected = rejected if rejected_valid else 0
        detail_accepted = accepted if accepted_valid else 0
        detail_non_final = non_final if non_final_present and non_final_valid else 0
        month_label = month if month_valid else f"detail_{index}"

        if status_ok:
            if not accepted_valid or not rejected_valid:
                rejection_evidence_consistent = False
                rejection_issues.append(f"{month_label}_missing_or_invalid_event_counters")
            if not non_final_valid:
                rejection_evidence_consistent = False
                rejection_issues.append(f"{month_label}_invalid_events_non_final")
            if not reasons_valid:
                rejection_evidence_consistent = False
                rejection_issues.append(f"{month_label}_invalid_rejection_reasons")
            if rejected_valid:
                total_rejected += detail_rejected
                if accepted_valid and event_count_valid:
                    if detail_accepted + detail_rejected > event_count:
                        rejection_evidence_consistent = False
                        rejection_issues.append(f"{month_label}_counters_exceed_event_count")
                if (
                    non_final_present
                    and non_final_valid
                    and accepted_valid
                    and event_count_valid
                    and detail_accepted + detail_rejected + detail_non_final > event_count
                ):
                    rejection_evidence_consistent = False
                    rejection_issues.append(f"{month_label}_counters_include_more_than_event_count")
                if reasons_valid:
                    reason_sum = sum(normalized_reasons.values())
                    if reason_sum != detail_rejected:
                        rejection_evidence_consistent = False
                        rejection_issues.append(f"{month_label}_rejection_reason_sum_mismatch")
                    elif detail_rejected == 0 and normalized_reasons:
                        rejection_evidence_consistent = False
                        rejection_issues.append(f"{month_label}_reasons_without_rejections")
                    historical_non_final = normalized_reasons.get("event_not_final", 0)
                    total_historical_non_final_rejections += historical_non_final
                    total_other_rejected += max(0, detail_rejected - historical_non_final)
                elif detail_rejected > 0:
                    rejection_evidence_consistent = False
                    rejection_issues.append(f"{month_label}_rejections_without_reason_breakdown")
            if accepted_valid:
                total_accepted += detail_accepted
            if non_final_present and non_final_valid:
                total_non_final_events += detail_non_final

        if month_valid:
            monthly_rejected_totals[month] = rejected if rejected_valid else None
            rejection_reasons_by_month[month] = (
                normalized_reasons if reasons_valid else reasons
            )
        month_rows.append({
            "index": index,
            "month": month if month_valid else None,
            "valid": bool(month_valid and status_ok and competition_ok and event_count_valid),
            "status": detail.get("status"),
            "competition_verified": detail.get("competition_verified"),
            "event_count": event_count,
            "events_accepted": accepted,
            "events_rejected": rejected,
            "events_non_final": non_final if non_final_present else None,
            "rejection_reasons": normalized_reasons if reasons_valid else reasons,
        })

    duplicates = sorted(key for key, count in duplicate_counts.items() if count > 1)
    expected_set = set(expected_keys)
    actual_set = set(actual_keys)
    missing = sorted(expected_set - actual_set)
    unexpected = sorted(actual_set - expected_set)
    month_keys_exact = bool(
        expected_keys
        and len(details) == len(expected_keys)
        and len(actual_keys) == len(expected_keys)
        and not duplicates
        and not month_key_issues
        and expected_set == actual_set
    )
    month_units_complete = bool(month_keys_exact and month_units_valid)
    non_final_events_unresolved = bool(total_non_final_events > 0)
    historical_non_final_unresolved = bool(total_historical_non_final_rejections > 0)
    return {
        "expected_month_keys": expected_keys,
        "actual_month_keys": actual_keys,
        "missing_month_keys": missing,
        "unexpected_month_keys": unexpected,
        "duplicate_month_keys": duplicates,
        "month_key_issues": month_key_issues,
        "month_keys_exact": month_keys_exact,
        "month_units_complete": month_units_complete,
        "expected_units": len(expected_keys),
        "success_units": success_count,
        "verified_units": verified_count,
        "valid_empty_units": valid_empty_units,
        "month_rows": month_rows,
        "malformed_details": list(dict.fromkeys(month_key_issues + rejection_issues)),
        "monthly_rejected_totals": monthly_rejected_totals,
        "rejection_reasons_by_month": rejection_reasons_by_month,
        "events_rejected": total_rejected,
        "events_accepted": total_accepted,
        "events_non_final": total_non_final_events,
        "historical_non_final_rejections": total_historical_non_final_rejections,
        "other_rejected_events": total_other_rejected,
        "historical_non_final_rejections_unresolved": historical_non_final_unresolved,
        "non_final_events_unresolved": non_final_events_unresolved,
        "rejection_evidence_consistent": rejection_evidence_consistent,
        "evidence_complete": bool(
            month_units_complete
            and rejection_evidence_consistent
            and total_rejected == 0
            and total_non_final_events == 0
        ),
    }
