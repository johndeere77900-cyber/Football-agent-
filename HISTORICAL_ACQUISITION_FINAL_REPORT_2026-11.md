# Historical Acquisition — Final Report (November 2026)

Project: Football Agent · Neon `dark-hall-82163232` / `neondb` / production branch `br-long-mountain-b5zwly7k`.
Every figure below was produced by a query against that database, not from provider counts,
manifests or job logs. The queries are reproduced in §11 so each number can be re-derived.

## Summary of the change

| Metric | Before this cycle | After this cycle |
| --- | --- | --- |
| Football fixture rows | 21,379 | **22,472** |
| Football fixtures with statistics | 17,111 | **21,427** (95.3 %) |
| Football fixtures without statistics | 4,268 | **1,045** |
| Fixtures carrying possession data | 0 | **2,873** |
| Basketball games with team/player/period stats | 847 | **5,366** (job still running, resumable) |

No row was deleted. No primary key, provider id or source label was rewritten. No schema change
was made. The only code changes are three acquisition jobs, one duplicate-aware guard, one new
enrichment source label, and a kickoff-field preference fix (all listed in §9).

---

## 1. Football fixtures acquired

**1,093 fixture rows** were inserted from the official UEFA match API (`match.uefa.com/v5`,
`source = 'uefa_match_v5'`), covering the qualifying rounds that the dataset did not hold:

| Competition | Season | Fetched | Inserted | Of which duplicate real-world events |
| --- | --- | --- | --- | --- |
| Champions League | 2019/20 | 210 | 210 | 8 |
| Champions League | 2025/26 | 281 | 281 | 91 |
| European Championship | 2020 | 313 | 313 | 34 |
| European Championship | 2024 | 289 | 289 | 46 |
| **Total** | | **1,093** | **1,093** | **179** |

Each row carries the official match id (hashed deterministically into the `fixture_id`), the 120-minute
score (a shoot-out is retained in `raw_json` and never folded into the scoreline), competition, season,
stage, and both team names.

**Honest qualification:** 179 of these rows describe a real-world match that was already stored under
another provider (`football_data_org` 91, `api_football` 46, `openfootball_internationals` 34,
`openfootball_json` 8). The dataset therefore contains **914 genuinely new fixtures** and 179 duplicate
rows. The duplicate arose because UEFA club names resolved to *new* canonical ids
(`football_team_paris_saint_germain`) instead of the ids the earlier feeds had registered
(`football_team_psg`), so the writer's canonical-id dedup could not see the existing row. Deletion is
prohibited, so the duplicates are kept and their statistics reconciled (§10). A real-world identity
guard was added to the backfill so a rerun cannot repeat this (§9).

## 2. Football fixtures already present or reconciled

* 21,379 fixtures already existed; **none was re-inserted**. The writer's validation rejected nothing
  (0 rejections across all four batches) and every submitted fixture resolved a canonical identity.
* **1,518 additional enrichment rows** were reconciled this cycle by real-world identity — 1,443 in the
  first pass (duplicate provider rows lacking stats, e.g. Bundesliga 2024/25 stored twice) plus 75 more
  once ESPN supplied donors. Examples verified: fixture 498993 (`Groningen v NAC`, 4-1) ← 450237341;
  fixture 1223980 (`FC Augsburg v Werder Bremen`, 2-2) ← 1332119992.
* All 179 UEFA duplicate rows received statistics through this same reconciliation once a donor existed.

## 3. Genuine unavailable football fixtures

None were proven unavailable this cycle. The previously reported "687 missing fixtures" estimate was
**not** correct as a missing count: 1,093 qualifying fixtures existed at the official source, of which
914 were genuinely absent and 179 duplicated an existing row. No competition-season was found where a
permitted source has fixtures the database lacks:

* UEFA v5 returned complete match lists for all four competition-seasons above (210 / 281 / 313 / 289).
* Europa League and Conference League qualifying were **not** acquired: the UEFA v5 competition ids for
  those editions were never located (ids 1–40 probed for 2019–2022 returned no match), so this remains
  **UNRESOLVED**, not UNAVAILABLE.
* No fixture was imported merely because a provider id was absent: every insert carries competition,
  season, both teams, kickoff date and a published score.

## 4. Football statistics acquired and remaining

**Acquired this cycle: 2,798 new ESPN statistics rows + 1,518 reconciled rows.**

| Source label | Fixtures with stats | What it carries |
| --- | --- | --- |
| `football_data_csv` | 11,907 | shots, shots on target, corners, cards (pre-existing) |
| `reconciled` | 6,722 | copied from the identical real-world match (same competition, season, date, score, alias-matched names) |
| `espn_soccer_summary` | 2,798 | possession, total shots, shots on target, corners, yellow and red cards |
| **Distinct fixtures covered** | **21,427** | |

* **Possession** now exists for 2,873 rows — previously **zero** rows in the database carried possession.
* **Expected goals: 0 rows.** ESPN's soccer summary does not publish xG, football-data.co.uk does not
  publish it for these seasons, and no permitted source was found that supplies it historically. xG is
  therefore **UNAVAILABLE** for the stored seasons and was never estimated.
* Remaining 1,045 statless fixtures by competition: Champions League 476, Europa League 194,
  European Championship 114, Serie A 100, Bundesliga 50, La Liga 45, Nations League 20, Ligue 1 15,
  Eredivisie 15, Premier League 9, World Cup 5, Primeira Liga 2.
* Of the 1,508 fixtures ESPN was asked about in the final run: 463 matched and were written, 831 had no
  matching ESPN event (**UNRESOLVED** — not proven unavailable), 211 had an ESPN event with no published
  statistics (**UNAVAILABLE** from that source), 3 summary fetch errors.

## 5. Basketball team statistics acquired

`historical_basketball_team_statistics`: **5,366 games / 10,732 rows** (home and away per game),
all `source='espn_boxscore'`. This cycle added **4,519 games** over the 847 present at audit time.

## 6. Basketball player statistics acquired

`historical_basketball_player_statistics`: **5,366 games / 142,794 player rows**. Per-player minutes,
points, FG, 3PT, FT, rebounds (incl. offensive/defensive), assists, turnovers, steals, blocks, fouls
and plus/minus as published.

## 7. Basketball period scores acquired

`historical_basketball_period_scores`: **5,366 games / 43,578 period rows** (per-quarter and overtime
lines), matching the team-statistics game set exactly, so no game has one category without the others.

## 8. Remaining unavailable basketball statistics

**In progress, not unavailable.** 4,218 of 9,584 games still lack statistics. The job
(`basketball_enrichment.py`) is resumable and idempotent — it skips any game that already has
`espn_boxscore` points — and was running when this report was written. Nothing indicates those games
are unobtainable; they simply have not been fetched yet. No basketball credential, configuration or
existing API path was modified.

## 9. Sources and acquisition methods

| Source | Method | Status |
| --- | --- | --- |
| UEFA official match API (`match.uefa.com/v5/matches`) | paginated `competitionId` + `seasonYear` fetch → deterministic id → `storage.save_historical_fixtures` | used (1,093 fixtures) |
| ESPN soccer (`site.api.espn.com/.../soccer/{league}/scoreboard`, `/summary`) | scoreboard lookup on kickoff date ±1 day, match accepted only on team-name agreement **and** identical final score; summary parsed for possession/shots/corners/cards | used (2,798 rows) |
| ESPN NBA (`site.api.espn.com/.../basketball/nba/summary`) | per-game summary → team/player/period tables | used (4,519 games this cycle) |
| football-data.co.uk CSV | already applied in earlier cycles; re-verified as exhaustively applied (dry-run planned 0) | no new rows |
| basketball-reference | **rejected** — site terms prohibit automated scraping; not used | not used |

Code added/changed (no schema change):

* `uefa_qualifying_backfill.py` — new; dry-run by default, `--apply` writes; now includes the
  real-world duplicate guard and prefers `kickOffTime.dateTime` over the date-only field.
* `espn_football_stats.py` — new; batched writes (one connection per 25 fixtures), qualifying-round
  competitions (`uefa.champions_qual`, `uefa.europa_qual`, `uefa.euroq`), ±1 day scoreboard fallback.
* `football_reconcile_fixture_stats.py` — new; alias-aware duplicate reconciliation.
* `storage.py` — one line: `espn_soccer_summary` registered in the enrichment source allow-list
  (an unregistered label previously hit an undefined `logger` reference and aborted the write).

## 10. Duplicate and identity reconciliation results

* **250 duplicate event groups** exist (same competition, season, both team names, kickoff date).
  These are duplicate rows of real-world matches, retained because deletion is prohibited; 1,518 of
  them now carry reconciled statistics and are therefore classified as duplicates rather than gaps.
* Reconciliation is deliberately conservative: a copy requires the same competition, season, UTC
  kickoff date, both canonical/alias-matched team names (exact, containment of a ≥3-character fragment,
  or ≥0.80 similarity) and an identical final score. Each copy records
  `provider_version = 'reconciled_from_<donor fixture id>'`, so the donor is always recoverable.
  0 ambiguous pairings were accepted; ambiguous buckets were skipped.
* **179 newly introduced duplicate rows** (the UEFA overlap in §1) are documented rather than deleted;
  the guard added to the backfill prevents recurrence.
* **Known defect: all 1,093 `uefa_match_v5` rows store kickoff time 00:00 UTC.** The UEFA `date` field is
  date-only and the parser preferred it over `dateTime`; the parser is now fixed, but the writer never
  rewrites a stored kickoff, so these rows keep date-accurate, time-inaccurate kickoffs. Dates and
  results are correct; kickoff times for these four competition-seasons should be treated as date-only.

## 11. Final Neon row counts and coverage

Queries used (all executed against production):

```sql
SELECT count(*) FROM historical_fixtures;                                  -- 22,472
SELECT source, count(*) FROM historical_fixtures GROUP BY 1;               -- csv 16,630 · api_football 2,193
                                                                           -- football_data_org 1,233
                                                                           -- uefa_match_v5 1,093 · mixed 927
                                                                           -- openfootball_internationals 277
                                                                           -- openfootball_json 119
SELECT source, count(*) FROM historical_fixture_enrichment GROUP BY 1;     -- csv 11,907 · reconciled 6,722
                                                                           -- espn_soccer_summary 2,798
SELECT count(DISTINCT fixture_id) FROM historical_fixture_enrichment;      -- 21,427
SELECT count(*) FROM historical_fixtures f LEFT JOIN historical_fixture_enrichment e
  ON e.fixture_id = f.fixture_id WHERE e.fixture_id IS NULL;               -- 1,045
SELECT count(*) FROM historical_fixture_enrichment WHERE home_possession IS NOT NULL; -- 2,873
SELECT count(*) FROM historical_fixture_enrichment WHERE home_xg IS NOT NULL;         -- 0
SELECT count(DISTINCT game_id), count(*) FROM historical_basketball_team_statistics;  -- 5,366 / 10,732
SELECT count(DISTINCT game_id), count(*) FROM historical_basketball_player_statistics;-- 5,366 / 142,794
SELECT count(DISTINCT game_id), count(*) FROM historical_basketball_period_scores;    -- 5,366 / 43,578
```

International competition coverage after this cycle (fixtures / with statistics):

| Competition | Season | Fixtures | With statistics |
| --- | --- | --- | --- |
| World Cup | 2022 | 128 | 123 |
| Champions League | 2019/20 | 340 | 232 |
| Champions League | 2022/23 | 214 | 124 |
| Champions League | 2024/25 | 279 | 182 |
| Champions League | 2025/26 | 470 | 303 |
| Champions League | 2026/27 | 54 | 40 |
| Europa League | 2022/23 | 175 | 119 |
| Europa League | 2023/24 | 175 | 129 |
| Europa League | 2024/25 | 269 | 177 |
| European Championship | 2020 | 364 | 307 |
| European Championship | 2024 | 340 | 283 |
| Nations League | 2020 | 162 | 148 |
| Nations League | 2024 | 188 | 182 |

## 12. Final dataset completion statuses

| Dataset | Status | Coverage | What remains |
| --- | --- | --- | --- |
| Football fixtures — UCL/Euro qualifying | **COMPLETE** | 1,093 official matches held (914 new, 179 reconciled duplicates) | none known at the official source |
| Football fixtures — Europa/Conference qualifying | **UNRESOLVED** | 0 | official competition ids for those editions not located; not proven unavailable |
| Football fixture statistics | **PARTIAL — RECOVERABLE REMAINS** | 21,427 / 22,472 (95.3 %), possession 2,873 | 1,045 statless; 831 of the last run's 1,508 unmatched at ESPN (UNRESOLVED), 211 confirmed without published stats (UNAVAILABLE) |
| Expected goals | **UNAVAILABLE** | 0 rows | no permitted historical source publishes xG for these seasons |
| Basketball team statistics | **IN PROGRESS — RECOVERABLE** | 5,366 / 9,584 (56.0 %) | 4,218 games queued in the resumable job |
| Basketball player statistics | **IN PROGRESS — RECOVERABLE** | 5,366 / 9,584 (56.0 %) | same 4,218 games |
| Basketball period scores | **IN PROGRESS — RECOVERABLE** | 5,366 / 9,584 (56.0 %) | same 4,218 games |

The historical acquisition is **not** declared finished: football statistics retain 1,045 unresolved
fixtures and basketball has 4,218 games still queueing. "COMPLETE" above is used only where a permitted
source has been exhausted.

## 13. Unresolved issues and exact reasons

1. **1,045 football fixtures remain statless.** 831 of the final run's candidates had no ESPN event on
   the kickoff date (or ±1 day) in the mapped competition, including the additional qualifying
   competitions; team names and score could not be agreed, so nothing was attached. Reason: no
   candidate event returned for that competition/date — **not** proof of unavailability.
2. **211 ESPN events publish no team statistics.** The match exists at ESPN but the summary carries no
   statistics block (typically early-round qualifying). Reason: source does not publish the data.
3. **Europa League / Conference League qualifying fixtures were not acquired.** UEFA v5 competition ids
   for those editions could not be located in ids 1–40 for 2019–2022. Reason: discovery failure —
   UNRESOLVED, not UNAVAILABLE.
4. **179 duplicate fixture rows were introduced** by the UEFA backfill (91 UCL 2025/26, 46 Euro 2024,
   34 Euro 2020, 8 UCL 2019/20) because UEFA names minted new canonical team ids. Deletion is
   prohibited, so the rows are retained, statistics-reconciled and documented; the duplicate guard now
   in the backfill prevents a repeat.
5. **All 1,093 UEFA rows store kickoff time 00:00 UTC** (date is correct). Cause: the parser preferred
   UEFA's date-only `date` field; fixed going forward, but stored kickoffs are never rewritten by the
   writer.
6. **No expected-goals data exists.** No permitted source publishes historical xG for these
   competition-seasons; no value was estimated or substituted.
7. **Basketball enrichment is incomplete by time, not by evidence.** 4,218 games remain; the job is
   resumable and idempotent, and prior batches survived runner restarts (847 → 5,366 games).
8. **Pull request could not be opened:** `gh` is unauthenticated in this environment
   ("not logged into any GitHub hosts"). The work is committed and pushed to the chat branch instead.
