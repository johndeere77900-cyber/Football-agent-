# Remaining Historical Acquisition and Enrichment Plan

Prepared for review against production `dark-hall-82163232` / branch `production`.
**No statement in Section 5 or 6 has been applied.** Sections 2-4 record state that
already exists in the database; Sections 5-6 are proposals for authorised review.

## 1. Verified current state

- Football fixtures: **21379**
- Canonical enrichment rows: **17111**, every one statistics-bearing (**17111**)
- Retired provenance-only rows: **3044** (archived in `historical_enrichment_retired`)
- Basketball games: **9584**; enriched games **847** (1694 team rows, 22222 player rows, 6878 period rows)
- Database size: **187 MB**

Enrichment manifest status (stored data, not acquisition state):

- basketball `NONE`: 7
- basketball `PARTIAL`: 1
- football `COMPLETE`: 31
- football `NONE`: 40
- football `PARTIAL`: 25

## 2. Football: remaining unenriched fixtures by league-season

Total remaining: **4268 fixtures across 38 league-seasons**.

| League | Season | Stored | Enriched | Missing | Stored sources |
|---|---|---|---|---|---|
| 78 | 2024 | 614 | 305 | 309 | `api_football+football_data_csv` |
| 2 | 2024 | 279 | 0 | 279 | `api_football` |
| 3 | 2024 | 269 | 0 | 269 | `api_football` |
| 135 | 2019 | 618 | 380 | 238 | `football_data_csv+mixed` |
| 2 | 2022 | 214 | 0 | 214 | `api_football` |
| 2 | 2025 | 189 | 0 | 189 | `football_data_org` |
| 5 | 2024 | 188 | 0 | 188 | `api_football` |
| 3 | 2022 | 175 | 0 | 175 | `api_football` |
| 3 | 2023 | 175 | 0 | 175 | `api_football` |
| 5 | 2020 | 162 | 0 | 162 | `openfootball_internationals` |
| 94 | 2019 | 456 | 306 | 150 | `football_data_csv+mixed` |
| 78 | 2025 | 456 | 306 | 150 | `football_data_csv+football_data_org` |
| 135 | 2020 | 520 | 380 | 140 | `football_data_csv+mixed` |
| 2 | 2019 | 130 | 0 | 130 | `mixed+openfootball_json` |
| 1 | 2022 | 128 | 0 | 128 | `api_football+openfootball_internationals` |
| 88 | 2024 | 430 | 306 | 124 | `football_data_csv+football_data_org` |
| 78 | 2019 | 430 | 306 | 124 | `football_data_csv+mixed` |
| 61 | 2020 | 488 | 380 | 108 | `football_data_csv+mixed` |
| 135 | 2024 | 380 | 272 | 108 | `api_football` |
| 140 | 2025 | 488 | 380 | 108 | `football_data_csv+football_data_org` |
| 61 | 2025 | 406 | 306 | 100 | `football_data_csv+football_data_org` |
| 88 | 2025 | 402 | 306 | 96 | `football_data_csv+football_data_org` |
| 78 | 2020 | 402 | 306 | 96 | `football_data_csv+mixed` |
| 135 | 2025 | 454 | 380 | 74 | `football_data_csv+football_data_org` |
| 2 | 2026 | 54 | 0 | 54 | `football_data_org` |
| 61 | 2024 | 358 | 306 | 52 | `api_football+football_data_csv` |
| 4 | 2024 | 51 | 0 | 51 | `api_football` |
| 4 | 2020 | 51 | 0 | 51 | `openfootball_internationals` |
| 39 | 2025 | 382 | 342 | 40 | `football_data_csv+football_data_org` |
| 39 | 2024 | 380 | 342 | 38 | `api_football+football_data_csv` |
| 88 | 2020 | 340 | 306 | 34 | `football_data_csv+mixed` |
| 88 | 2019 | 258 | 232 | 26 | `football_data_csv+mixed` |
| 78 | 2026 | 97 | 72 | 25 | `football_data_csv+football_data_org` |
| 140 | 2026 | 129 | 108 | 21 | `football_data_csv+football_data_org` |
| 88 | 2026 | 113 | 99 | 14 | `football_data_csv+football_data_org` |
| 61 | 2026 | 93 | 80 | 13 | `football_data_csv+football_data_org` |
| 39 | 2026 | 99 | 89 | 10 | `football_data_csv+football_data_org` |
| 135 | 2026 | 103 | 98 | 5 | `football_data_csv+football_data_org` |

Grouped by competition:

- 2 (UEFA Champions League): **866** missing across 5 seasons
- 78 (Bundesliga): **704** missing across 5 seasons
- 3 (UEFA Europa League): **619** missing across 3 seasons
- 135 (Serie A): **565** missing across 5 seasons
- 5 (UEFA Nations League): **350** missing across 2 seasons
- 88 (Eredivisie): **294** missing across 5 seasons
- 61 (Ligue 1): **273** missing across 4 seasons
- 94 (Primeira Liga): **150** missing across 1 seasons
- 140 (La Liga): **129** missing across 2 seasons
- 1 (FIFA World Cup): **128** missing across 1 seasons
- 4 (UEFA Euro): **102** missing across 2 seasons
- 39 (Premier League): **88** missing across 3 seasons

## 3. Basketball: remaining games by season

Total remaining: **8737 games** of 9584 stored; 847 already enriched.

| Season | Games | Enriched | Outstanding |
|---|---|---|---|
| 2019 | 1218 | 847 | 371 |
| 2020 | 1226 | 0 | 1226 |
| 2021 | 1390 | 0 | 1390 |
| 2022 | 1389 | 0 | 1389 |
| 2023 | 1393 | 0 | 1393 |
| 2024 | 1397 | 0 | 1397 |
| 2025 | 1397 | 0 | 1397 |
| 2026 | 174 | 0 | 174 |

## 4. Source evidence for competitions 1-5 (2,065 fixtures)

The approved CSV provider publishes no division code for any of competitions 1-5,
so `football_data_csv` cannot supply them. Evidence gathered this run:

- `soccerdata` is **not installed** in the environment (`ModuleNotFoundError`), so
  `soccerdata_fbref` and `soccerdata_match_history` cannot execute at all.
- `https://fbref.com/...` returns **HTTP 403** to this environment, so even an
  installed client would fail against the live source.
- OpenFootball, OpenLigaDB and the internationals repository never carried match
  statistics; they supply results only.

Conclusion: the previously blocked competitions 1-5 are recoverable through the newly
verified `espn_soccer_summary` source (see `APPROVED_DATA_SOURCES.md`). It supplies shots,
shots on target, corners, cards, possession, fouls, offsides and saves, but not xG and not
player statistics. A measured reconciliation pass matched 47% of sampled fixtures on
exact dates, so a +-1 day window plus name reconciliation and a dry-run review is
mandatory before any write. Approval criteria below still apply to any further source:

1. Identity: provider publishes stable match ids or (date, both teams, competition,
   season) that reconcile exactly against stored fixtures.
2. Coverage: the provider documents the competitions and seasons it covers.
3. Fields: shots, shots on target, corners, cards, possession and xG where present.
4. Verification: a sample of matches cross-checked against an independent source.
5. Provenance: per-record source, retrieval time and provider version.

`soccerdata_fbref` already appears in the approved order but is non-functional here;
it should be treated as `UNSUPPORTED_COVERAGE` in this environment rather than
silently skipped.

## 5. Proposed basketball batches (NOT APPLIED)

The capability is implemented and committed (`basketball_enrichment.py`). Each batch
is resumable and skips games that already hold a scored team boxscore:

- season 2019: `python basketball_enrichment.py --season 2019 --workers 8` (remaining 371)
- season 2020: `python basketball_enrichment.py --season 2020 --workers 8` (remaining 1226)
- season 2021: `python basketball_enrichment.py --season 2021 --workers 8` (remaining 1390)
- season 2022: `python basketball_enrichment.py --season 2022 --workers 8` (remaining 1389)
- season 2023: `python basketball_enrichment.py --season 2023 --workers 8` (remaining 1393)
- season 2024: `python basketball_enrichment.py --season 2024 --workers 8` (remaining 1397)
- season 2025: `python basketball_enrichment.py --season 2025 --workers 8` (remaining 1397)
- season 2026: `python basketball_enrichment.py --season 2026 --workers 8` (remaining 174)

Expected writes per game: 2 team rows, 2 period-row sets (4 periods, more with
overtime), and roughly 25-30 player rows. Observed on the 847 completed games:
1,694 team rows, 22,222 player rows, 6,878 period rows.

## 6. Proposed football enrichment batches (NOT APPLIED)

- Domestic seasons where the CSV exists but was never attached:
  `python football_csv_enrichment.py --leagues 78,94,135,61,88,140,39 --seasons 2019 2020 2024 2025 2026 --apply`
  The dry-run already proved the matching logic: it planned and wrote 482 rows
  (Premier League 2023 210 + Serie A 2024 272) with zero ambiguous matches.
- Competitions 1-5 are no longer blocked: they are recoverable via
  `espn_soccer_summary` subject to identity reconciliation. See
  `APPROVED_DATA_SOURCES.md` for the evidence and the per-season match rates.
- Series A 2024 still shows 108 unmatched fixtures; Premier League 2024 38 and
  2025 40 remain unmatched. These CSV rows carry no counterpart fixture, so they
  cannot be enriched without new acquisition evidence, not by inference.

## 7. Read-only verification queries for review

```sql
-- Enrichment coverage per league-season (must equal manifest enrichment_status)
with e as (select fixture_id from historical_fixture_enrichment where home_shots is not null group by 1)
select f.league_id, f.season, count(*) total, count(e.fixture_id) enriched
  from historical_fixtures f left join e on e.fixture_id=f.fixture_id
 group by 1,2 order by 1,2;

-- No duplicate fixture statistics, no orphans
select count(*) from (select fixture_id from historical_fixture_enrichment
  group by 1 having count(*)>1) z;
select count(*) from historical_fixture_enrichment e
  left join historical_fixtures f on f.fixture_id=e.fixture_id where f.fixture_id is null;

-- Basketball coverage per season
select g.season, count(*), count(distinct s.game_id) from historical_basketball_games g
  left join historical_basketball_team_statistics s on s.game_id=g.game_id
 group by 1 order by 1;

-- Retired provenance rows retained with full payload
select count(*), count(raw_json) from historical_enrichment_retired;
```

## 8. Integrity results observed

- Football duplicate fixture-statistics groups: **0**
- Football orphan enrichment rows: **0**
- Basketball duplicate team rows per (game, side): **0**
- Basketball duplicate player rows per (game, player): **0**
- Basketball orphan team-statistic rows: **0**
- Football fixtures deleted or rewritten: **0** (still 21379)

## 9. What is persisted versus prepared

Persisted in production: the 17111 statistics rows, the 3044 archived provenance rows,
and the 847 enriched basketball games. Prepared but not applied: every batch in
Sections 5 and 6, and any status promotions beyond those already recorded.
