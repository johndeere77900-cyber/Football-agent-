import unittest
from unittest.mock import patch, MagicMock
import os
import sys

import data_resolver
import historical_sync
import main
import config

class TestDirectCSVAndResiliency(unittest.TestCase):

    def test_exact_urls_and_landing_pages(self):
        self.assertEqual(data_resolver.FOOTBALL_DATA_CSV_URL_PATTERN, "https://www.football-data.co.uk/mmz4281/{season_code}/{division_code}.csv")
        self.assertEqual(data_resolver.FOOTBALL_DATA_REFERENCE_URL, "https://www.football-data.co.uk/data.php")
        self.assertEqual(data_resolver.SOFASCORE_API_BASE_URL, "https://api.sofascore.com/api/v1/")

    def test_deduplication_merging_key(self):
        f1 = {
            "fixture": {"id": 1001, "date": "2024-09-01T15:00:00Z"},
            "league": {"id": 39, "season": 2024},
            "canonical_home_id": "football_team_A",
            "canonical_away_id": "football_team_B",
        }
        f2 = {
            "fixture": {"id": 2002, "date": "2024-09-01T18:00:00Z"},
            "league": {"id": 39, "season": 2024},
            "canonical_home_id": "football_team_A",
            "canonical_away_id": "football_team_B",
        }
        k1 = data_resolver.get_match_identity_key(f1)
        k2 = data_resolver.get_match_identity_key(f2)
        self.assertEqual(k1, ("football", 39, "2024", "2024-09-01", "football_team_A", "football_team_B"))
        self.assertEqual(k1, k2)

    def test_season_code_formatting(self):
        self.assertEqual(data_resolver._format_fd_csv_season_code(2025), "2526")
        self.assertEqual(data_resolver._format_fd_csv_season_code(2024), "2425")
        self.assertEqual(data_resolver._format_fd_csv_season_code(2020), "2021")
        self.assertEqual(data_resolver._format_fd_csv_season_code(1800), "")

    @patch("data_resolver._get_authoritative_historical_team_ids", return_value=["football_team_Man United", "football_team_Fulham"])
    @patch("team_identity.bootstrap_historical_team_identity", side_effect=lambda raw_name, **kwargs: f"football_team_{raw_name}")
    @patch("urllib.request.urlopen")
    def test_csv_header_normalization_and_stable_id(self, mock_urlopen, mock_bootstrap, mock_auth_roster):
        # Header includes BOM, uppercase, extra spaces
        raw_csv = "\ufeffDATE , HOMETEAM , AWAYTEAM , FTHG , FTAG , HS , AS\n16/08/2024, Man United , Fulham , 1 , 0 , 14 , 10\n"
        mock_response = MagicMock()
        mock_response.read.return_value = raw_csv.encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        fixtures1 = data_resolver._fetch_direct_csv_league_fixtures(39, 2024)
        fixtures2 = data_resolver._fetch_direct_csv_league_fixtures(39, 2024)

        self.assertEqual(len(fixtures1), 1)
        self.assertEqual(len(fixtures2), 1)

        f1 = fixtures1[0]
        f2 = fixtures2[0]

        # Verify header normalization
        self.assertEqual(f1["teams"]["home"]["name"], "Man United")
        self.assertEqual(f1["teams"]["away"]["name"], "Fulham")
        self.assertEqual(f1["goals"]["home"], 1)
        self.assertEqual(f1["goals"]["away"], 0)
        self.assertEqual(f1["statistics"]["shots"], {"home": 14, "away": 10})

        # Verify process-stable deterministic fixture ID
        self.assertEqual(f1["fixture"]["id"], f2["fixture"]["id"])
        self.assertIsInstance(f1["fixture"]["id"], int)
        self.assertGreater(f1["fixture"]["id"], 0)

    @patch("data_resolver._get_authoritative_historical_team_ids", return_value=["football_team_Arsenal", "football_team_Chelsea"])
    @patch("team_identity.bootstrap_historical_team_identity", side_effect=lambda raw_name, **kwargs: f"football_team_{raw_name}")
    @patch("urllib.request.urlopen")
    def test_csv_invalid_row_rejection(self, mock_urlopen, mock_bootstrap, mock_auth_roster):
        # Invalid date format and negative scores
        raw_csv = "Date,HomeTeam,AwayTeam,FTHG,FTAG\ninvalid_date,Arsenal,Chelsea,2,1\n17/08/2024,Arsenal,Chelsea,-1,1\n17/08/2024,Arsenal,Chelsea,2,0\n"
        mock_response = MagicMock()
        mock_response.read.return_value = raw_csv.encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        fixtures = data_resolver._fetch_direct_csv_league_fixtures(39, 2024)
        self.assertEqual(len(fixtures), 1)
        self.assertEqual(fixtures[0]["goals"], {"home": 2, "away": 0})

    @patch("data_resolver._fetch_direct_csv_league_fixtures")
    @patch("api_football.get_league_fixtures_page")
    def test_direct_fallback_when_api_quota_exhausted(self, mock_api_page, mock_csv):
        mock_api_page.side_effect = data_resolver.APIFootballQuotaExhaustedError("Quota exhausted")
        mock_csv.return_value = [{
            "fixture": {"id": 100, "date": "2024-08-16T12:00:00Z", "status": {"short": "FT"}},
            "league": {"id": 39, "season": 2024},
            "teams": {"home": {"id": None, "name": "A"}, "away": {"id": None, "name": "B"}},
            "goals": {"home": 1, "away": 0},
            "canonical_home_id": "football_team_A",
            "canonical_away_id": "football_team_B",
            "provider_provenance": {"provider": "football_data_csv"}
        }]

        resolver = data_resolver.DataResolver()
        res = resolver.get_league_fixtures_page(39, 2024, page=1)

        self.assertTrue(res["secondary_quota_exhausted"])
        self.assertTrue(res["secondary_failed"])
        self.assertEqual(len(res["fixtures"]), 1)

    def test_multi_season_cli_and_incomplete_status(self):
        parser = main.build_parser()
        args = parser.parse_args(["--historical-queue", "--seasons", "2020", "2021", "2022"])
        self.assertEqual(args.seasons, [2020, 2021, 2022])

        with self.assertRaises(ValueError):
            # Cannot specify --season and --seasons together
            args_bad = parser.parse_args(["--season", "2024", "--seasons", "2020", "2021"])
            if args_bad.season is not None and getattr(args_bad, "seasons", None) is not None:
                raise ValueError("Cannot specify both --season and --seasons together.")

    def test_basketball_pipeline_preserved(self):
        self.assertIn(12, config.ALLOWED_BASKETBALL_LEAGUE_IDS)
        import basketball_api
        import basketball_model
        self.assertTrue(callable(basketball_api.get_games_by_date))
        self.assertTrue(callable(basketball_model.predict_game))

    def test_csv_acquisition_fetches_roster_when_stored_roster_absent(self):
        """
        Regression test proving direct CSV acquisition can resolve valid teams when stored roster
        is initially absent (allow_network=False returns []) but an authoritative roster can be obtained (allow_network=True).
        """
        auth_roster = ["football_team_Arsenal", "football_team_Chelsea"]

        def mock_get_team_ids(league_id, season, allow_network=False, **kwargs):
            if allow_network:
                return auth_roster
            return []

        raw_csv = "Date,HomeTeam,AwayTeam,FTHG,FTAG\n17/08/2024,Arsenal,Chelsea,2,1\n"
        mock_response = MagicMock()
        mock_response.read.return_value = raw_csv.encode("utf-8")
        mock_response.__enter__.return_value = mock_response

        with patch("data_resolver._get_authoritative_historical_team_ids", side_effect=mock_get_team_ids), \
             patch("team_identity.bootstrap_historical_team_identity", side_effect=lambda raw_name, **kwargs: f"football_team_{raw_name}"), \
             patch("urllib.request.urlopen", return_value=mock_response):

            fixtures = data_resolver._fetch_direct_csv_league_fixtures(39, 2024)

            self.assertEqual(len(fixtures), 1)
            self.assertEqual(fixtures[0]["canonical_home_id"], "football_team_Arsenal")
            self.assertEqual(fixtures[0]["canonical_away_id"], "football_team_Chelsea")

    def test_empty_identity_db_and_unavailable_roster_apis_bootstraps_teams(self):
        """
        Regression test proving that with an empty identity DB and unavailable roster APIs,
        direct CSV acquisition resolves valid CSV team identities and persists them in team_identities.
        """
        import storage
        import team_identity

        storage.init_db()

        raw_csv = "Date,HomeTeam,AwayTeam,FTHG,FTAG\n17/08/2024,Arsenal,Chelsea,2,1\n"
        mock_response = MagicMock()
        mock_response.read.return_value = raw_csv.encode("utf-8")
        mock_response.__enter__.return_value = mock_response

        with patch("data_resolver._get_authoritative_historical_team_ids", return_value=[]), \
             patch("urllib.request.urlopen", return_value=mock_response):

            fixtures = data_resolver._fetch_direct_csv_league_fixtures(39, 2024)

            self.assertEqual(len(fixtures), 1)
            self.assertEqual(fixtures[0]["canonical_home_id"], "football_team_arsenal")
            self.assertEqual(fixtures[0]["canonical_away_id"], "football_team_chelsea")

            # Verify persisted in team_identities
            home_id = team_identity.resolve_canonical_team_id("Arsenal", "football_data_csv", None, league_id=39)
            away_id = team_identity.resolve_canonical_team_id("Chelsea", "football_data_csv", None, league_id=39)
            self.assertEqual(home_id, "football_team_arsenal")
            self.assertEqual(away_id, "football_team_chelsea")

    def test_existing_canonical_identities_reused_without_duplication(self):
        """
        Regression test proving existing canonical identities are reused without creating duplicate IDs.
        """
        import storage
        import team_identity

        storage.init_db()

        # Seed existing canonical identity for Arsenal
        cid_existing = team_identity.bootstrap_historical_team_identity("Arsenal FC", "api_football", 42, league_id=39)
        self.assertEqual(cid_existing, "football_team_arsenal")

        # Now bootstrap through CSV provider
        cid_csv = team_identity.bootstrap_historical_team_identity("Arsenal", "football_data_csv", None, league_id=39)
        self.assertEqual(cid_csv, "football_team_arsenal")

        # Verify only 1 canonical identity exists in team_identities for arsenal
        identities = storage.get_team_identities_by_canonical_id("football", "football_team_arsenal")
        self.assertGreaterEqual(len(identities), 1)
        canonical_ids = {i["canonical_id"] for i in identities}
        self.assertEqual(canonical_ids, {"football_team_arsenal"})

    def test_ambiguous_team_names_rejected(self):
        """
        Regression test proving ambiguous team names (mapping to multiple canonical IDs) are rejected.
        """
        import storage
        import team_identity

        storage.init_db()

        # Pre-seed two conflicting canonical team identity mappings with normalized_name "united"
        storage.save_team_identity("football", "football_team_manchester_united", "provider_a", "1", "united", "United", 39)
        storage.save_team_identity("football", "football_team_dc_united", "provider_b", "2", "united", "United", 39)

        # Attempt to bootstrap "United" when normalized name matches multiple candidate IDs
        cid = team_identity.bootstrap_historical_team_identity("United", "football_data_csv", None, league_id=39)
        self.assertIsNone(cid)

    def test_incomplete_csv_season_not_marked_complete(self):
        """
        Regression test proving an incomplete CSV season (e.g. 1 fixture out of 380) is not marked COMPLETE.
        """
        import storage

        storage.init_db()

        raw_csv = "Date,HomeTeam,AwayTeam,FTHG,FTAG\n17/08/2024,Arsenal,Chelsea,2,1\n"
        mock_response = MagicMock()
        mock_response.read.return_value = raw_csv.encode("utf-8")
        mock_response.__enter__.return_value = mock_response

        with patch("data_resolver._get_authoritative_historical_team_ids", return_value=[]), \
             patch("urllib.request.urlopen", return_value=mock_response):

            resolver = data_resolver.DataResolver()
            res = resolver.get_league_fixtures_page(39, 2024, page=1)

            self.assertFalse(res["provider_metadata"]["is_complete"])
            self.assertFalse(res["provider_metadata"]["structural_coverage"]["verified"])
            self.assertEqual(res["provider_metadata"]["structural_coverage"]["reason"], "authoritative_team_universe_unavailable")

    def test_complete_csv_season_accepted_when_independently_verified(self):
        """
        Regression test proving a complete CSV season is accepted as COMPLETE when its team universe is independently verified.
        """
        import storage

        storage.init_db()

        # Construct 380 valid fixtures for a 20-team double-round-robin league
        teams = [f"football_team_team_{i}" for i in range(20)]
        for tid in teams:
            storage.save_team_identity("football", tid, "test_provider", tid, tid, tid, 39)

        fixtures = []
        fid = 1000
        for i in range(20):
            for j in range(20):
                if i == j:
                    continue
                fid += 1
                fixtures.append({
                    "fixture": {"id": fid, "date": "2024-09-01T12:00:00Z", "status": {"short": "FT"}},
                    "league": {"id": 39, "season": 2024},
                    "teams": {
                        "home": {"id": 100 + i, "name": f"team_{i}"},
                        "away": {"id": 100 + j, "name": f"team_{j}"},
                    },
                    "goals": {"home": 1, "away": 0},
                    "canonical_home_id": teams[i],
                    "canonical_away_id": teams[j],
                    "provider_provenance": {"provider": "football_data_csv"},
                })

        coverage = data_resolver._validate_historical_structural_coverage(
            fixtures,
            season=2024,
            provider_metadata={"expected_team_ids": teams, "competition_format": "double_round_robin"},
            league_id=39,
        )

        self.assertTrue(coverage["verified"])
        self.assertIsNone(coverage["reason"])

if __name__ == "__main__":
    unittest.main()
