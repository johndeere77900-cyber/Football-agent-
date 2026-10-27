# Historical Data Layer & Backtest Architecture

This document describes the unified historical data and backtest architecture for football and basketball.

## Overview

The historical acquisition and backtesting pipeline is decoupled from live prediction execution to ensure point-in-time integrity and protect API quota:

```
    Football:  Direct Football-Data.co.uk CSV
               -> SoccerData FBref -> SoccerData MatchHistory -> SoccerData Sofascore
    Basketball: ESPN URL scoreboards (monthly acquisition units)
               -> API-Basketball (fallback)
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

API-Football and football-data.org are **not** historical football fixture acquisition providers. Their clients and normalizers are retained because live prediction, live fixture retrieval, team form/statistics, standings recovery and grading still use them; those live call sites are intentionally unchanged.

## Invariants & Rules

1. **NORMAL BACKTEST INVARIANT**:
   - `Neon/SQLite historical dataset` -> `zero external network calls`.
   - Backtests read strictly from database storage and fail closed if a dataset is missing, incomplete, or count-mismatched.

2. **HISTORICAL ACQUISITION ROUTES**:
   - Step 1 (Read-first persistence): Reuse a stored DB dataset when its manifest is `COMPLETE` **and** the persisted records independently pass structural coverage validation on a revalidation run, with no external calls.
   - Step 2 (Approved football route): Direct Football-Data.co.uk CSV -> SoccerData FBref -> SoccerData MatchHistory -> SoccerData Sofascore. Acquisition stops at the first source that supplies independently validated, sufficient coverage and continues to the next approved source when a source fails or its data is structurally insufficient.
   - Step 3 (Approved basketball route): ESPN URL scoreboards (primary, one independent coverage unit per NBA season month) -> API-Basketball (fallback, coverage tracked from its own response pagination).
   - A successful HTTP request, a non-empty response, or the exhaustion of every approved provider never establishes complete historical coverage on its own.

3. **DISTINCT PROVIDER OUTCOMES**:
   - Each provider attempt records a distinct outcome: `SUCCESS`, `FAILED`, `MALFORMED`, `VALID_EMPTY`, `QUOTA_EXHAUSTED`, `INSUFFICIENT`.
   - A provider failure does not prevent the next approved provider from being attempted, and an insufficient dataset stays `INCOMPLETE` even when every approved provider has been exhausted.

4. **COMPETITION LIFECYCLE vs ACQUISITION STATE**:
   - Competition lifecycle: `UPCOMING`, `ONGOING`, `COMPLETED`, `UNKNOWN`.
   - Provider acquisition state: `PENDING`, `RUNNING`, `INCOMPLETE`, `VALIDATED`, `FAILED`.
   - The two concepts are tracked separately and are never conflated. `acquisition_complete=true` is **not** equivalent to lifecycle completion.
   - Declared season windows and competition formats live in `config.COMPETITION_SEASON_WINDOWS` together with `COMPETITION_SEASON_COMPLETION_GRACE_DAYS`. Competitions absent from that map fail closed with lifecycle `UNKNOWN`, so a dataset is never promoted to `COMPLETE` on the strength of a generic calendar boundary plus non-empty records.

5. **COVERAGE UNIT METADATA**:
   - `expected_pages` / `pages_completed` are generic coverage units. The manifest also stores the `coverage_unit` label that describes what was actually counted:
     - `single_shot` - one acquisition pass for the persisted football dataset,
     - `espn_months` - one unit per ESPN monthly scoreboard request (nine per NBA season),
     - `api_pages` - the API-Basketball response pagination total.
   - Per-provider coverage is persisted alongside the aggregate so one provider's progress can never be presented with another provider's semantics. Nine ESPN monthly requests are never represented as one API page, and API pagination totals are never invented for ESPN.

6. **PROVIDER-SPECIFIC COMPLETION SEMANTICS**:
   - An ESPN month counts as successfully covered only when its request succeeded and its payload (including a genuinely empty payload) was validated; a failed or incomplete month leaves the dataset incomplete.
   - Successful ESPN coverage is resumable and preserved; a later failing month never discards prior valid coverage or persisted games.
   - Persisted games are re-validated after acquisition before a manifest is promoted to `COMPLETE`.

7. **API-FOOTBALL HISTORICAL BUDGET CEILING**:
   - `API_FOOTBALL_HISTORICAL_DAILY_BUDGET` (default 50, hard cap 50) bounds the API-Football requests issued by the historical *statistics/standings/form backfill* paths. Historical football fixture acquisition itself issues zero API-Football requests.
   - Omitted, invalid, zero, or negative `max_budget` parameters clamp to 50 for historical acquisition.
   - Basketball historical acquisition is bounded separately by `API_BASKETBALL_HISTORICAL_DAILY_BUDGET`.

8. **COMPLETE Datasets & Exact Budget Completion**:
   - A dataset marked `COMPLETE` bypasses acquisition completely (0 requests) unless the persisted data fails validation or `--refresh` is explicitly specified.
   - Reaching the exact final allowed credit quota slot on a successful final response allows a dataset to become `COMPLETE` provided all expected coverage units/games were successfully retrieved.

9. **Match Status & Score Policy (`historical_match_policy.py`)**:
   - Football: `FT`, `AET`, and `PEN` matches are completed historical matches.
   - Basketball: `FT` and `AOT` games are completed historical games.

### API-Football Score Field Semantics & Settlement Policy
API-Football payloads contain both top-level `goals` and nested `score` objects:
- `goals`: `{home, away}` — The authoritative total match goals after 90 or 120 minutes of play (excluding penalty shootout kicks). Used for Over/Under totals, BTTS, team goals, Elo ratings, and H2H/recent form calculations.
- `score.fulltime`: `{home, away}` — The 90-minute regulation-time score. Used strictly for 1X2 market settlement.
- `score.extratime`: `{home, away}` — Goals scored specifically during extra time in knockout fixtures.
- `score.penalty`: `{home, away}` — Goals scored during penalty shootouts. **Penalty shootout kicks are never counted as match goals** for 1X2, Totals, BTTS, Elo, or form calculations.

10. **Multi-Sport Identity**:
    - Manifests are keyed by `(sport, league_id, season)` preventing collision between football league 12 and basketball league 12.

11. **Empty-Page Acquisition Policy**:
    - An empty payload (`response: []`) cannot mark a dataset `COMPLETE` by itself. `valid_fixtures_count > 0` and `final_stored_count > 0` remain strictly required, so empty queries stay `INCOMPLETE`.
    - Conversely, a legitimate empty period is not a failure when the source request succeeded and coverage is known: empty counters are persisted in the manifest and preserved across resumable runs, and are only treated as legitimate for months/pages whose request outcome was recorded as successful.

12. **Backtest Experiment Recording**:
    - Backtest results, market-level metrics (Brier score, log loss, calibration/ECE), sample sizes, and seed details are permanently recorded in `backtest_runs` and `backtest_market_metrics`.

## CLI Commands

To verify persisted historical datasets:
```bash
# Queue mode verification (checks all target seasons across configured leagues)
python3 main.py --verify-historical-datasets --historical-queue --seasons 2020 2021 2022 2023 2024 2025 2026

# Single dataset verification (requires --sport and --league)
python3 main.py --verify-historical-datasets --sport football --league 39 --seasons 2024
python3 main.py --verify-historical-datasets --sport basketball --league 12 --seasons 2024
```

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

Football historical sync requires no football API keys: it uses the direct downloadable CSV source and SoccerData. Basketball historical sync uses ESPN scoreboards by default and only needs `API_BASKETBALL_KEY` for the API-Basketball fallback.

To run database-first backtests:
```bash
python3 main.py --backtest --sport football --league 39 --season 2024 --sample 50
python3 main.py --backtest --sport basketball --league 12 --season 2024 --sample 50
```

To view past backtest run history:
```bash
python3 main.py --backtest-history
```
