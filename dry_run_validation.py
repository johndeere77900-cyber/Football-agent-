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

def run_dry_run_validation():
    print("=== DRY-RUN HISTORICAL ACQUISITION PROVIDER SELECTION VALIDATION ===\n")
    season = 2024
    for league_id, name in TARGET_LEAGUES:
        cov_status, cov_reason = main.check_competition_coverage(league_id, season)
        fd_code = data_resolver.LEAGUE_TO_FD_CODE.get(league_id)

        if cov_status == "coverage_available":
            selected_provider = "API-Football (Primary)"
            reason = "API-Football provides full coverage for this league and season."
        elif fd_code:
            selected_provider = f"football-data.org (Secondary, Code: {fd_code})"
            reason = f"API-Football coverage unavailable ({cov_reason}); competition is mapped to secondary provider."
        else:
            selected_provider = "NONE (Failed Closed)"
            reason = f"API-Football unavailable and league {league_id} has no secondary mapping."

        print(f"League: {name:<20} (ID: {league_id:<3}) | Season: {season}")
        print(f"  Selected Provider: {selected_provider}")
        print(f"  Reason:            {reason}\n")

if __name__ == "__main__":
    run_dry_run_validation()
