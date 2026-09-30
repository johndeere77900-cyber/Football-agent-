# Historical Data Layer & Backtest Architecture

This document describes the historical data architecture for football backtesting.

## Overview

The historical backtesting pipeline is decoupled from live API acquisition to prevent unnecessary credit consumption on API-Football:

```
    API-Football
          ↓
    historical_sync.py (controlled historical collector)
          ↓
    Neon PostgreSQL / SQLite (`historical_fixtures`, `historical_fixture_enrichment`, `historical_datasets`)
          ↓
    backtest.py (database-first backtest engine)
          ↓
    prediction / reconstruction / evaluation
```

## Dataset Completion States & Integrity Invariants

1. **Strict Pagination Completion Criteria**: A dataset is marked `COMPLETE` in `historical_datasets` ONLY IF:
   - All expected pagination pages (`expected_pages`) have been successfully retrieved (`pages_completed == expected_pages`);
   - Pagination metadata is valid and `acquisition_complete` is `True`;
   - Valid fixture records have been persisted;
   - No quota exhaustion occurred;
   - No API/network/acquisition errors occurred.
2. **Zero-API Acquisition on COMPLETE Datasets**: When `status == 'COMPLETE'`, `historical_sync.py` **skips API acquisition entirely (0 API requests made)** unless `--refresh` is explicitly passed.
3. **Backtest Integrity Check**: Before executing a backtest, `backtest.py` verifies both:
   - `dataset_status["status"] == "COMPLETE"`
   - `dataset_status["fixture_count"] == actual_stored_count`
   If the manifest count differs from actual stored database rows, `backtest.py` fails closed with a clear `"historical dataset integrity mismatch"` error.
4. **Explicit Refresh**: Passing `--refresh` explicitly re-fetches fixtures according to quota protections without deleting pre-existing data until successful completion.

## Quota Protections & Header Fail-Safes

- **Global Limit**: The global 100 daily request limit (`API_FOOTBALL_DAILY_CREDIT_LIMIT`) remains authoritative and atomic across all network attempts.
- **Historical Daily Budget**: Historical acquisition enforces `API_FOOTBALL_HISTORICAL_DAILY_BUDGET = 50` (configurable via environment variable) per network attempt (including pagination and retries).
- **Provider Header Fail-Safe**: Dynamic rate-limit headers (`x-ratelimit-requests-remaining` / `X-RateLimit-Remaining`) are inspected on every API response. If the provider reports remaining credits $\le 0$, an `APIFootballQuotaExhaustedError` is raised immediately to block further network attempts.

## Point-in-Time Integrity

- For any fixture at kickoff time $T$, feature reconstruction (recent form, H2H, Elo, league averages) strictly uses fixtures where `kickoff < T`.
- Future fixtures and target fixture outcomes are excluded from prediction feature snapshots.

## Running Historical Data Sync

To acquire historical fixtures for a league and season:

```bash
python historical_sync.py --league-id 39 --season 2024
```

To include optional statistical enrichment (corners and cards):

```bash
python historical_sync.py --league-id 39 --season 2024 --with-enrichment
```

To force re-acquisition on a COMPLETE dataset:

```bash
python historical_sync.py --league-id 39 --season 2024 --refresh
```

## Running Backtests

Once the historical dataset status is `COMPLETE` in storage, run backtests normally:

```bash
python backtest.py --league-id 39 --season 2024 --sample 50
```
