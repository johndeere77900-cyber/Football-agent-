from pathlib import Path
from unittest.mock import patch

import pytest

import backtest
import config
import storage


WORKFLOW_PATH = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "backtest.yml"


def _parse_steps_from_workflow_yml(content: str):
    """
    Parse step blocks from GitHub Actions YAML without third-party dependencies.
    Extracts name, run script, and env dictionary for each step under jobs.run.steps.
    """
    steps = []
    lines = content.splitlines()

    current_step = None
    in_steps = False
    in_env = False

    for line in lines:
        stripped = line.strip()

        if line.startswith("    steps:"):
            in_steps = True
            continue

        if not in_steps:
            continue

        if line.startswith("      - name:") or line.startswith("      - uses:") or line.startswith("      - run:"):
            if current_step:
                steps.append(current_step)
            current_step = {"name": "", "run": "", "env": {}}
            in_env = False

            if line.startswith("      - name:"):
                current_step["name"] = stripped.split("name:", 1)[1].strip().strip("'\"")
            elif line.startswith("      - run:"):
                current_step["run"] = stripped.split("run:", 1)[1].strip()
            continue

        if current_step is None:
            continue

        if line.startswith("        env:"):
            in_env = True
            continue

        if in_env and line.startswith("          "):
            if ":" in stripped:
                k, v = stripped.split(":", 1)
                current_step["env"][k.strip()] = v.strip().strip("'\"")
            continue

        if line.startswith("        run: |") or line.startswith("        run:"):
            in_env = False
            continue

        if line.startswith("          python3 ") or line.startswith("          pytest "):
            current_step["run"] += "\n" + stripped

    if current_step:
        steps.append(current_step)

    return steps


def test_workflow_yaml_structure_and_neon_wiring():
    assert WORKFLOW_PATH.is_file(), f"Workflow file not found at {WORKFLOW_PATH}"

    content = WORKFLOW_PATH.read_text(encoding="utf-8")
    steps = _parse_steps_from_workflow_yml(content)

    expected_db_modes = {
        "Run backtest",
        "Check coverage",
        "Find league",
        "Raw debug",
    }

    found_modes = set()

    for step in steps:
        name = step.get("name")
        if name in expected_db_modes:
            found_modes.add(name)
            env = step.get("env", {})

            assert "NEON_DATABASE_URL" in env, f"Step '{name}' is missing NEON_DATABASE_URL in env"
            assert env["NEON_DATABASE_URL"] == "${{ secrets.NEON_DATABASE_URL }}", (
                f"Step '{name}' NEON_DATABASE_URL expected secrets.NEON_DATABASE_URL, got {env['NEON_DATABASE_URL']}"
            )

            assert "REQUIRE_NEON" in env, f"Step '{name}' is missing REQUIRE_NEON in env"
            assert env["REQUIRE_NEON"] == "true", f"Step '{name}' REQUIRE_NEON expected 'true', got {env['REQUIRE_NEON']}"

            # Ensure secret is not echoed or passed as CLI param
            run_cmd = step.get("run", "")
            assert "NEON_DATABASE_URL" not in run_cmd, f"Step '{name}' exposes NEON_DATABASE_URL in run command text"
            assert "echo" not in run_cmd.lower() or "secret" not in run_cmd.lower(), f"Step '{name}' echoes sensitive info"

    assert found_modes == expected_db_modes, f"Missing database workflow mode steps: {expected_db_modes - found_modes}"


def test_require_neon_blocks_sqlite_fallback(tmp_path, monkeypatch):
    db_path = tmp_path / "predictions.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    monkeypatch.setattr(config, "REQUIRE_NEON", True)
    monkeypatch.setattr(config, "NEON_DATABASE_URL", None)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    monkeypatch.setenv("REQUIRE_NEON", "true")

    with pytest.raises(RuntimeError) as exc_info:
        storage.init_db()

    assert "NEON_DATABASE_URL is required" in str(exc_info.value)
    assert "Silent SQLite fallback is disabled" in str(exc_info.value)


def test_canonical_backtest_with_complete_dataset_zero_api_calls(tmp_path, monkeypatch):
    db_path = tmp_path / "predictions.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    storage.init_db()

    fixtures = [
        {
            "fixture": {"id": 8801 + i, "date": f"2025-01-{i+1:02d}T15:00:00+00:00", "status": {"short": "FT"}},
            "teams": {
                "home": {"id": (i % 2) + 1, "name": f"Team {(i % 2) + 1}"},
                "away": {"id": (i % 2) + 3, "name": f"Team {(i % 2) + 3}"},
            },
            "goals": {"home": 1, "away": 0},
        }
        for i in range(10)
    ]

    storage.save_historical_fixtures(fixtures, league_id=39, season=2025)
    storage.mark_historical_dataset_complete(league_id=39, season=2025, fixture_count=len(fixtures))

    def fail_if_called(*args, **kwargs):
        raise AssertionError("API-Football should NOT be called during canonical backtest!")

    import data_resolver
    monkeypatch.setattr(data_resolver.api_football, "get_league_fixtures", fail_if_called)
    monkeypatch.setattr(data_resolver.api_football, "get_league_fixtures_with_metadata", fail_if_called)
    monkeypatch.setattr(data_resolver.api_football, "get_enriched_fixtures", fail_if_called)

    res = backtest.run_real_backtest(
        league_id=39,
        season=2025,
        sample_size=2,
        min_prior_matches=1,
    )

    assert res["fixtures_fetched"] == 10
    assert res["graded"] > 0
