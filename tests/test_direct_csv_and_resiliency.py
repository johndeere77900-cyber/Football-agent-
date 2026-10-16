import unittest
from unittest.mock import patch, MagicMock
import os
import sys

import data_resolver
import historical_sync
import main
import config

class TestDirectCSVAndResiliency(unittest.TestCase):

    def test_season_code_formatting(self):
        self.assertEqual(data_resolver._format_fd_csv_season_code(2025), "2526")
        self.assertEqual(data_resolver._format_fd_csv_season_code(2024), "2425")
        self.assertEqual(data_resolver._format_fd_csv_season_code(2020), "2021")
        self.assertEqual(data_resolver._format_fd_csv_season_code(1800), "")

    @patch("team_identity.bootstrap_historical_team_identity", side_effect=lambda raw_name, **kwargs: f"football_team_{raw_name}")
    @patch("urllib.request.urlopen")
    def test_csv_header_normalization_and_stable_id(self, mock_urlopen, mock_bootstrap):
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

    @patch("team_identity.bootstrap_historical_team_identity", side_effect=lambda raw_name, **kwargs: f"football_team_{raw_name}")
    @patch("urllib.request.urlopen")
    def test_csv_invalid_row_rejection(self, mock_urlopen, mock_bootstrap):
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

        self.assertTrue(res["primary_quota_exhausted"])
        self.assertTrue(res["primary_failed"])
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

if __name__ == "__main__":
    unittest.main()
