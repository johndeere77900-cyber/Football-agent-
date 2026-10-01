# Historical Data Layer & Backtest Architecture

This document describes the unified historical data and backtest architecture for football and basketball.

## Overview

The historical acquisition and backtesting pipeline is decoupled from live prediction execution to ensure point-in-time integrity and protect API quota:

```
    API-Football / API-Basketball
                ↓
    historical_sync.py (controlled multi-sport historical collector)
                ↓
    Neon PostgreSQL / SQLite Storage
      - `historical_fixtures` / `historical_basketball_games`
      - `historical_fixture_enrichment`
      - `historical_datasets` (manifests keyed by sport, league_id, season)
      - `backtest_runs` & `backtest_market_metrics`
                ↓
    backtest.py (database-first backtest engine)
                ↓
    prediction / reconstruction / market evaluation & experiment persistence
```

## Invariants & Rules

1. **NORMAL BACKTEST INVARIANT**:
   - `Neon/SQLite historical dataset` -> `zero API-Football/API-Basketball network calls`.
   - Backtests read strictly from database storage and fail closed if a dataset is missing, incomplete, or count-mismatched.
2. **HISTORICAL ACQUISITION INVARIANT**:
   - `Provider API` -> `persistent quota / cache` -> `validated storage` -> `manifest`.
3. **COMPLETE Datasets**:
   - A dataset marked `COMPLETE` bypasses API calls completely (0 requests made) unless `--refresh` is explicitly specified.
4. **Exact Budget Completion**:
   - Reaching the exact final allowed credit quota slot on a successful final response allows a dataset to become `COMPLETE` provided all expected pages/games were successfully retrieved.
5. **Match Status & Score Policy (`historical_match_policy.py`)**:
   - Football: `FT`, `AET`, and `PEN` matches are completed historical matches.
   - Basketball: `FT` and `AOT` games are completed historical games.

### API-Football Score Field Semantics & Settlement Policy
API-Football payloads contain both top-level `goals` and nested `score` objects:
- `goals`: `{home, away}` — The authoritative total match goals after 90 or 120 minutes of play (excluding penalty shootout kicks). Used for Over/Under totals, BTTS, team goals, Elo ratings, and H2H/recent form calculations.
- `score.fulltime`: `{home, away}` — The 90-minute regulation-time score. Used strictly for 1X2 market settlement.
- `score.extratime`: `{home, away}` — Goals scored specifically during extra time in knockout fixtures.
- `score.penalty`: `{home, away}` — Goals scored during penalty shootouts. **Penalty shootout kicks are never counted as match goals** for 1X2, Totals, BTTS, Elo, or form calculations.
6. **Multi-Sport Identity**:
   - Manifests are keyed by `(sport, league_id, season)` preventing collision between football league 12 and basketball league 12.
7. **Backtest Experiment Recording**:
   - Backtest results, market-level metrics (Brier score, log loss, calibration/ECE), sample sizes, and seed details are permanently recorded in `backtest_runs` and `backtest_market_metrics`.

## CLI Commands

To check historical dataset manifest status:
```bash
python3 main.py --dataset-status --sport football --league 39 --season 2024
python3 main.py --dataset-status --sport basketball --league 12 --season 2024
```

To sync historical data:
```bash
python3 main.py --historical-sync --sport football --league 39 --season 2024 --with-enrichment
python3 main.py --historical-sync --sport basketball --league 12 --season 2024
```

To run database-first backtests:
```bash
python3 main.py --backtest --sport football --league 39 --season 2024 --sample 50
python3 main.py --backtest --sport basketball --league 12 --season 2024 --sample 50
```

To view past backtest run history:
```bash
python3 main.py --backtest-history
```
