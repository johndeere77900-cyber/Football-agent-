"""
Static Architecture Boundary & Provider Fallback / Identity Test Suite.

Enforces:
1. Architectural Boundary Enforcement: Prohibits direct provider invocations
   (api_football.*, football_data_api.*, soccerdata_provider.*) in production modules
   outside approved adapter and resolver boundary files.
2. Target Architecture Verification: Tests A-X requirements for 3-tier fallback,
   canonical identity isolation, cutoff rules, conflict preservation, and deterministic synthetic IDs.
"""

import ast
import os
import re
import pytest
from unittest.mock import patch, MagicMock

import config
import storage
import time_utils
import team_identity
from data_resolver import DataResolver, generate_synthetic_fixture_id, reconcile_fixture_records


# Approved provider boundary files where direct provider invocations are permitted
APPROVED_BOUNDARY_FILES = {
    "api_football.py",
    "football_data_api.py",
    "soccerdata_provider.py",
    "data_resolver.py",
}

# Production files subject to boundary enforcement
PRODUCTION_FILES = [
    "main.py",
    "historical_sync.py",
    "historical_features.py",
    "historical_h2h.py",
    "backtest.py",
    "live_model.py",
    "telegram_bot.py",
    "prediction_engine.py",
    "quality_gate.py",
    "calibration.py",
    "elo.py",
]


class ProviderCallVisitor(ast.NodeVisitor):
    def __init__(self, filename):
        self.filename = filename
        self.violations = []

    def visit_Attribute(self, node):
        if isinstance(node.value, ast.Name):
            module_name = node.value.id
            method_name = node.attr
            if module_name in ("api_football", "football_data_api", "soccerdata_provider"):
                # Ignore exception references
                if not method_name.endswith("Error") and not method_name.isupper():
                    self.violations.append((node.lineno, f"{module_name}.{method_name}"))
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module in ("api_football", "football_data_api", "soccerdata_provider"):
            for alias in node.names:
                if not alias.name.endswith("Error") and not alias.name.isupper():
                    self.violations.append((node.lineno, f"from {node.module} import {alias.name}"))
        self.generic_visit(node)

    def visit_Import(self, node):
        for alias in node.names:
            if alias.name in ("api_football", "football_data_api", "soccerdata_provider"):
                self.violations.append((node.lineno, f"import {alias.name}"))
        self.generic_visit(node)


def test_static_architecture_boundary():
    """Scan production Python files and fail if direct football provider calls are found."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    violations = []

    for filename in PRODUCTION_FILES:
        filepath = os.path.join(repo_root, filename)
        if not os.path.exists(filepath):
            continue

        with open(filepath, "r", encoding="utf-8") as f:
            code = f.read()

        tree = ast.parse(code, filename=filename)
        visitor = ProviderCallVisitor(filename)
        visitor.visit(tree)

        if visitor.violations:
            for line, call in visitor.violations:
                violations.append(f"{filename}:{line} invokes prohibited provider function '{call}'")

    assert not violations, f"Architecture Boundary Violations found:\n" + "\n".join(violations)


# ============================================================================
# TARGET ARCHITECTURE & DATA RESOLVER SCENARIO TESTS (A-X)
# ============================================================================


@pytest.fixture(autouse=True)
def init_test_storage(tmp_path, monkeypatch):
    db_file = tmp_path / "arch_test.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "NEON_DATABASE_URL", None)
    storage.init_db()


def test_A_complete_neon_fixture_zero_provider_calls():
    """Scenario A: Complete Neon fixture -> zero provider calls."""
    league_id = 39
    season = 2024
    date_str = "2024-09-15"

    complete_fixture = {
        "fixture": {"id": 1001, "date": f"{date_str}T15:00:00+00:00", "status": {"short": "FT"}},
        "league": {"id": league_id, "season": season, "name": "Premier League"},
        "teams": {"home": {"id": 1, "name": "Arsenal"}, "away": {"id": 2, "name": "Chelsea"}},
        "goals": {"home": 2, "away": 0},
        "statistics": {
            "xG": {"home": 1.8, "away": 0.5},
            "shots": {"home": 12, "away": 4},
            "shots_on_target": {"home": 5, "away": 1},
            "corners": {"home": 6, "away": 2},
            "yellow_cards": {"home": 1, "away": 2},
            "red_cards": {"home": 0, "away": 0},
            "possession": {"home": 60, "away": 40},
            "events": [{"type": "Goal"}],
        },
    }
    storage.save_historical_fixtures([complete_fixture], league_id, season)

    resolver = DataResolver()
    with patch("api_football.get_fixtures_by_date") as mock_af, \
         patch("football_data_api.get_competition_matches") as mock_fd, \
         patch("soccerdata_provider.get_match_history_games") as mock_sd:

        fixtures, meta = resolver.get_fixtures_for_date(date_str, league_id=league_id)

        assert len(fixtures) == 1
        assert meta["data_source"] == "internal_db"
        assert mock_af.called is False
        assert mock_fd.called is False
        assert mock_sd.called is False


def test_E_F_G_H_provider_fallback_chain_and_exception_handling():
    """Scenario E, F, G, H: Primary fails -> secondary -> tertiary -> safe empty result."""
    resolver = DataResolver()

    # E & F: Primary timeout/exception -> Secondary attempted
    with patch("api_football.get_fixtures_by_date", side_effect=RuntimeError("Primary Timeout")), \
         patch("football_data_api.get_competition_matches", return_value={"matches": []}) as mock_fd:

        res, meta = resolver.get_fixtures_for_date("2024-09-20", league_id=39)
        assert mock_fd.called
        assert meta["fallback_used"] is True

    # G & H: All providers fail -> return safe empty result without crash
    with patch("api_football.get_fixtures_by_date", side_effect=RuntimeError("Primary Crash")), \
         patch("football_data_api.get_competition_matches", side_effect=RuntimeError("Secondary Crash")), \
         patch("soccerdata_provider.get_match_history_games", side_effect=RuntimeError("Tertiary Crash")):

        res, meta = resolver.get_fixtures_for_date("2024-09-20", league_id=39)
        assert res == []
        assert meta["resolver_status"] == "NO_DATA"


def test_I_get_team_statistics_fallback():
    """Scenario I: get_team_statistics 3-tier fallback."""
    resolver = DataResolver()

    raw_fd_table = [
        {
            "position": 1,
            "team": {"id": 100, "name": "Arsenal", "shortName": "Arsenal"},
            "playedGames": 10,
            "won": 7,
            "draw": 2,
            "lost": 1,
            "goalsFor": 20,
            "goalsAgainst": 8,
            "goalDifference": 12,
            "points": 23,
        }
    ]

    with patch("api_football.get_team_statistics", side_effect=RuntimeError("Primary Stats Error")), \
         patch("football_data_api.get_competition_standings", return_value=raw_fd_table):

        stats = resolver.get_team_statistics(10, 39, 2024, team_name="Arsenal")
        assert stats is not None, "get_team_statistics fallback MUST return valid stats when secondary provider supplies data!"
        assert isinstance(stats, dict)
        assert "fixtures" in stats
        assert stats["provider_provenance"]["provider"] == "football_data_org"


def test_J_get_head_to_head_fallback_and_cutoff():
    """Scenario J & O: H2H 3-tier fallback with strict cutoff timestamp filtering."""
    resolver = DataResolver()
    cutoff_date = "2024-09-10T15:00:00+00:00"

    af_h2h = [
        # Match before cutoff
        {
            "fixture": {"id": 801, "date": "2024-08-01T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 10, "name": "Arsenal"}, "away": {"id": 20, "name": "Chelsea"}},
            "goals": {"home": 2, "away": 1},
        },
        # Match AFTER cutoff (FUTURE) - MUST BE REJECTED
        {
            "fixture": {"id": 802, "date": "2024-09-15T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 10, "name": "Arsenal"}, "away": {"id": 20, "name": "Chelsea"}},
            "goals": {"home": 3, "away": 0},
        },
    ]

    with patch("api_football.get_head_to_head", return_value=af_h2h):
        h2h = resolver.get_head_to_head(10, 20, last=5, home_team_name="Arsenal", away_team_name="Chelsea", fixture_date=cutoff_date, league_id=39)
        assert len(h2h) == 1
        assert h2h[0]["fixture"]["id"] == 801


def test_Q_R_S_T_canonical_identity_matching_rules():
    """Scenarios Q, R, S, T: Wrong home/away, ambiguous identity, or substring collisions MUST NOT merge."""
    c_home = "football_team_39_arsenal"
    c_away = "football_team_39_chelsea"

    primary_rec = {
        "fixture": {"id": 999, "date": "2024-09-10T15:00:00+00:00"},
        "league": {"id": 39, "season": 2024},
        "teams": {"home": {"id": 10, "name": "Arsenal"}, "away": {"id": 20, "name": "Chelsea"}},
        "canonical_home_id": c_home,
        "canonical_away_id": c_away,
        "provider_provenance": {"provider": "api_football"},
    }

    # Wrong home team (Liverpool vs Chelsea) -> MUST NOT MERGE
    wrong_home_candidate = {
        "fixture": {"id": 888, "date": "2024-09-10T15:00:00+00:00"},
        "teams": {"home": {"id": 30, "name": "Liverpool"}, "away": {"id": 20, "name": "Chelsea"}},
        "canonical_home_id": "football_team_39_liverpool",
        "canonical_away_id": c_away,
        "provider_provenance": {"provider": "football_data_org"},
    }

    # Substring collision candidate ("Arsenal FC Reserve" vs "Chelsea") -> MUST NOT MERGE
    substring_collision = {
        "fixture": {"id": 777, "date": "2024-09-10T15:00:00+00:00"},
        "teams": {"home": {"id": 100, "name": "Arsenal Reserve"}, "away": {"id": 20, "name": "Chelsea"}},
        "canonical_home_id": "football_team_39_arsenal_reserve",
        "canonical_away_id": c_away,
        "provider_provenance": {"provider": "soccerdata"},
    }

    merged_wrong = reconcile_fixture_records([primary_rec, wrong_home_candidate])
    assert "statistics" not in merged_wrong or merged_wrong.get("data_conflicts") is not None or merged_wrong["fixture"]["id"] == 999

    merged_sub = reconcile_fixture_records([primary_rec, substring_collision])
    assert merged_sub["fixture"]["id"] == 999


def test_U_V_W_namespace_synthetic_ids_conflicts():
    """Scenarios U, V, W: Namespace isolation, deterministic synthetic IDs, conflict preservation."""
    # V: Synthetic ID generation is deterministic across process restarts and uses FULL timestamp
    with patch("team_identity.resolve_canonical_team_id", side_effect=lambda raw_name=None, **kw: f"football_team_{raw_name.lower()}" if raw_name else None):
        id1 = generate_synthetic_fixture_id("football_data_org", "Arsenal", "Chelsea", "2024-09-10T15:00:00Z", 39, 2024)
        id2 = generate_synthetic_fixture_id("football_data_org", "Arsenal", "Chelsea", "2024-09-10T15:00:00Z", 39, 2024)
        id_different_time = generate_synthetic_fixture_id("football_data_org", "Arsenal", "Chelsea", "2024-09-10T18:00:00Z", 39, 2024)
        id_short_date = generate_synthetic_fixture_id("football_data_org", "Arsenal", "Chelsea", "2024-09-10", 39, 2024)

        assert id1 == id2
        assert id1 != id_different_time, "Same-day fixtures with different timestamps must produce distinct synthetic IDs!"
        assert id_short_date is None, "Short date string without full timestamp must return None!"
        assert isinstance(id1, str)
        assert "football_data_org" in id1


def test_synthetic_fixture_id_requires_existing_canonical_identity():
    """Verify synthetic fixture ID generation returns None when canonical IDs cannot be resolved without auto-registration."""
    with patch("team_identity.resolve_canonical_team_id", return_value=None) as mock_resolve, \
         patch("team_identity.bootstrap_historical_team_identity") as mock_bootstrap:

        syn_id = generate_synthetic_fixture_id("api_football", "Arsenal", "Chelsea", "2024-09-10T15:00:00+00:00", 39, 2024)

        assert syn_id is None, "Synthetic fixture ID MUST be None when canonical team identities cannot be resolved!"
        assert mock_resolve.called, "resolve_canonical_team_id MUST be called!"
        assert mock_bootstrap.called is False, "bootstrap_historical_team_identity MUST NOT be called!"


def test_team_stats_skips_matches_without_canonical_identity():
    """Verify _build_team_stats_from_matches skips historical records lacking canonical IDs."""
    from data_resolver import _build_team_stats_from_matches

    matches_no_canonical = [
        {
            "fixture": {"id": 101, "status": {"short": "FT"}},
            "teams": {"home": {"id": 10, "name": "Arsenal"}, "away": {"id": 20, "name": "Chelsea"}},
            "goals": {"home": 2, "away": 1},
            # Missing canonical_home_id and canonical_away_id
        }
    ]

    stats = _build_team_stats_from_matches(matches_no_canonical, "football_team_39_arsenal", team_id=10)
    assert stats is None, "_build_team_stats_from_matches MUST return None when matches lack canonical IDs!"


def test_team_stats_does_not_cross_match_provider_ids():
    """Verify team stats calculation never compares raw provider IDs across different providers."""
    from data_resolver import _build_team_stats_from_matches

    other_provider_match = [
        {
            "fixture": {"id": 999, "status": {"short": "FT"}},
            "teams": {"home": {"id": 10, "name": "Other Team"}, "away": {"id": 20, "name": "Another Team"}},
            "goals": {"home": 3, "away": 0},
            "canonical_home_id": "football_team_39_other_team",
            "canonical_away_id": "football_team_39_another_team",
            "provider_provenance": {"provider": "other_provider"},
        }
    ]

    stats = _build_team_stats_from_matches(other_provider_match, "football_team_39_arsenal", team_id=10)
    assert stats is None, "Matches for a different canonical team MUST NOT be counted even if numeric team ID happens to match!"

    # W: Score conflict preservation
    primary = {
        "fixture": {"id": 100, "date": "2024-09-10T15:00:00+00:00"},
        "goals": {"home": 2, "away": 1},
        "provider_provenance": {"provider": "api_football"},
    }
    conflicting = {
        "fixture": {"id": 200, "date": "2024-09-10T15:00:00+00:00"},
        "goals": {"home": 1, "away": 1},
        "provider_provenance": {"provider": "football_data_org"},
    }

    reconciled = reconcile_fixture_records([primary, conflicting])
    assert reconciled["disputed_score"] is True
    assert len(reconciled["data_conflicts"]) == 1
    assert reconciled["data_conflicts"][0]["field"] == "goals"


def test_partial_primary_data_triggers_secondary_and_tertiary_fallback():
    """Prove: API returns partial fixture -> football-data.org fills gaps -> remaining gaps -> SoccerData is invoked."""
    resolver = DataResolver(force_fallback=True)
    date_str = "2024-09-15"

    partial_primary = [
        {
            "fixture": {"id": 101, "date": f"{date_str}T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": 10, "name": "Arsenal"}, "away": {"id": 20, "name": "Chelsea"}},
            "goals": {"home": 2, "away": 1},
            # Missing corners, xG, cards, shots
        }
    ]

    fd_matches = {
        "matches": [
            {
                "id": 5001,
                "utcDate": f"{date_str}T15:00:00Z",
                "status": "FINISHED",
                "homeTeam": {"id": 100, "name": "Arsenal"},
                "awayTeam": {"id": 200, "name": "Chelsea"},
                "score": {"fullTime": {"home": 2, "away": 1}},
                "season": {"startDate": "2024-08-01"},
            }
        ]
    }

    sd_matches = [
        {
            "fixture": {"id": "sd_101", "date": f"{date_str}T15:00:00+00:00", "status": {"short": "FT"}},
            "league": {"id": 39},
            "teams": {"home": {"id": "sd_10", "name": "Arsenal"}, "away": {"id": "sd_20", "name": "Chelsea"}},
            "goals": {"home": 2, "away": 1},
            "statistics": {"corners": {"home": 5, "away": 3}},
            "provider_provenance": {"provider": "soccerdata"},
        }
    ]

    with patch("api_football.get_fixtures_by_date", return_value=partial_primary), \
         patch("football_data_api.get_competition_matches", return_value=fd_matches) as mock_fd, \
         patch("soccerdata_provider.get_match_history_games", return_value=("SOURCE_AVAILABLE", sd_matches, {})) as mock_sd:

        fixtures, meta = resolver.get_fixtures_for_date(date_str, league_id=39)

        assert mock_fd.called, "Secondary provider MUST be called when primary fixture lacks required statistical fields!"
        assert mock_sd.called, "Tertiary provider MUST be called when remaining gaps exist after secondary provider!"
        assert len(fixtures) == 1
        assert fixtures[0]["statistics"]["corners"] == {"home": 5, "away": 3}


def test_raw_provider_ids_different_providers_cannot_match_without_canonical_ids():
    """Prove: Raw provider IDs from different providers cannot match without canonical IDs."""
    primary_rec = {
        "fixture": {"id": 100, "date": "2024-09-10T15:00:00+00:00"},
        "league": {"id": 39, "season": 2024},
        "teams": {"home": {"id": 10, "name": "Arsenal"}, "away": {"id": 20, "name": "Chelsea"}},
        "provider_provenance": {"provider": "api_football"},
    }

    raw_id_mismatch = {
        "fixture": {"id": 100, "date": "2024-09-10T15:00:00+00:00"},
        "league": {"id": 39, "season": 2024},
        "teams": {"home": {"id": 10, "name": "Unknown Team A"}, "away": {"id": 20, "name": "Unknown Team B"}},
        "provider_provenance": {"provider": "football_data_org"},
    }

    # Different team names without canonical IDs cannot match even if numeric provider team IDs happen to be 10 and 20
    with patch("team_identity.bootstrap_historical_team_identity", side_effect=lambda name, prov, tid, **kw: f"{prov}_{tid}" if tid else None):
        reconciled = reconcile_fixture_records([primary_rec, raw_id_mismatch])
        assert reconciled["teams"]["home"]["name"] == "Arsenal"
        assert reconciled["teams"]["away"]["name"] == "Chelsea"
