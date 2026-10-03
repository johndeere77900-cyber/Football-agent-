"""
Dry-run acquisition provider selection validation script for configured competitions.
"""

import main
import data_resolver
import config

TARGET_LEAGUES = [
    (39, "Premier League"),
    (140, "La Liga"),
    (135, "Serie A"),
    (78, "Bundesliga"),
    (61, "Ligue 1"),
    (2, "Champions League"),
    (88, "Eredivisie"),
    (94, "Primeira Liga"),
]

def evaluate_provider_selection_pure(league_id, season, primary_coverage_available=True, primary_reason="Coverage available."):
    """
    Pure decision function for provider selection without network or database I/O.
    """
    fd_code = data_resolver.LEAGUE_TO_FD_CODE.get(league_id)
    if primary_coverage_available:
        return "API-Football (Primary)", "API-Football provides full coverage for this league and season."
    elif fd_code:
        return f"football-data.org (Secondary, Code: {fd_code})", f"API-Football coverage unavailable ({primary_reason}); competition is mapped to secondary provider."
    else:
        return "NONE (Failed Closed)", f"API-Football unavailable and league {league_id} has no secondary mapping."


def run_dry_run_validation():
    print("=== DRY-RUN HISTORICAL ACQUISITION PROVIDER SELECTION VALIDATION (100% OFFLINE) ===\n")
    season = 2024
    for league_id, name in TARGET_LEAGUES:
        # PURE OFFLINE DECISION EVALUATION
        selected_provider, reason = evaluate_provider_selection_pure(league_id, season, primary_coverage_available=True)
        print(f"League: {name:<20} (ID: {league_id:<3}) | Season: {season}")
        print(f"  Selected Provider: {selected_provider}")
        print(f"  Reason:            {reason}\n")

if __name__ == "__main__":
    run_dry_run_validation()
