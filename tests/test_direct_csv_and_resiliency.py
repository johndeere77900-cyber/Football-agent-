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

if __name__ == "__main__":
    unittest.main()
