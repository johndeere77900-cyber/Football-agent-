from unittest.mock import MagicMock, patch
import pytest

import football_data_api
import storage


@pytest.fixture(autouse=True)
def init_test_db():
    storage.init_db()


@patch("requests.get")
def test_get_competition_matches_cached(mock_get):
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "matches": [
            {
                "id": 101,
                "utcDate": "2025-01-15T20:00:00Z",
                "status": "FINISHED",
                "homeTeam": {"id": 1, "name": "Arsenal", "shortName": "Arsenal"},
                "awayTeam": {"id": 2, "name": "Chelsea", "shortName": "Chelsea"},
                "score": {"fullTime": {"home": 2, "away": 1}},
            }
        ]
    }
    mock_get.return_value = mock_resp

    with patch.object(football_data_api, "_headers", return_value={"X-Auth-Token": "test-key"}):
        res1 = football_data_api.get_competition_matches("PL", season=2024)
        matches1 = res1.get("matches", []) if isinstance(res1, dict) else res1
        assert len(matches1) == 1
        assert matches1[0]["id"] == 101

        # Second call should use cache and not hit network again
        res2 = football_data_api.get_competition_matches("PL", season=2024)
        matches2 = res2.get("matches", []) if isinstance(res2, dict) else res2
        assert len(matches2) == 1
        assert mock_get.call_count == 1


@patch("requests.get")
def test_get_competition_standings(mock_get):
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "standings": [
            {
                "type": "TOTAL",
                "table": [
                    {
                        "position": 1,
                        "team": {"id": 1, "name": "Arsenal", "shortName": "Arsenal"},
                        "playedGames": 10,
                        "won": 7,
                        "draw": 2,
                        "lost": 1,
                        "points": 23,
                        "goalsFor": 20,
                        "goalsAgainst": 8,
                        "goalDifference": 12,
                    }
                ],
            }
        ]
    }
    mock_get.return_value = mock_resp

    with patch.object(football_data_api, "_headers", return_value={"X-Auth-Token": "test-key"}):
        table = football_data_api.get_competition_standings("PL", season=2024)
        assert len(table) == 1
        assert table[0]["position"] == 1
        assert table[0]["team"]["name"] == "Arsenal"
