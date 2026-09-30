# Historical Data Layer & Backtest Architecture

This document describes the historical data architecture for football backtesting.

## Overview

The historical backtesting pipeline is decoupled from live API acquisition to prevent unnecessary credit consumption on API-Football:

```
    API-Football
          ↓
    historical_sync.py (controlled historical collector)
          ↓
    Neon PostgreSQL / SQLite (`historical_fixtures` & `historical_fixture_enrichment`)
          ↓
    backtest.py (database-first backtest engine)
          ↓
    prediction / reconstruction / evaluation
```

## Key Invariants & Safeguards

1. **Database-First Backtests**: `backtest.py` strictly reads historical fixtures and statistical enrichment from `storage` (`historical_fixtures` table). Normal backtests make **ZERO calls** to API-Football. If historical data is missing, the backtest fails explicitly with an actionable message directing the user to run the sync job first.
2. **Quota Protection**:
   - The global 100 daily request limit (`API_FOOTBALL_DAILY_CREDIT_LIMIT`) remains strictly enforced and atomic across all network requests.
   - Historical acquisition has its own ceiling setting: `API_FOOTBALL_HISTORICAL_DAILY_BUDGET = 50` (configurable via environment variable).
   - Dynamic rate-limit headers (`x-ratelimit-requests-remaining`) are captured to fail safe if the provider reports remaining credits are exhausted.
3. **Point-in-Time Integrity**:
   - For any fixture at kickoff time $T$, feature reconstruction (recent form, H2H, Elo, league averages) strictly uses fixtures where `kickoff < T`.
   - Future fixtures and target fixture outcomes are excluded from prediction feature snapshots.
4. **Idempotency**:
   - Sync operations write fixtures with `ON CONFLICT (fixture_id) DO NOTHING` to prevent duplicate rows or corrupted historical data.
5. **Historical Odds API Scope**:
   - Historical Odds API data collection is explicitly excluded from this batch.

## Running Historical Data Sync

To acquire historical fixtures for a league and season:

```bash
python historical_sync.py --league-id 39 --season 2024
```

To include optional statistical enrichment (corners and cards):

```bash
python historical_sync.py --league-id 39 --season 2024 --with-enrichment
```

## Running Backtests

Once historical fixtures are persisted in storage, run backtests normally:

```bash
python backtest.py --league-id 39 --season 2024 --sample 50
```
