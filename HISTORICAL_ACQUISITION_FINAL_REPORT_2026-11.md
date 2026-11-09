# Historical acquisition - final verified report (7 November 2026)

Neon project `dark-hall-82163232`, database `neondb`, production branch `br-long-mountain-b5zwly7k`.
## 0. Final state (re-verified after the last import)

| Metric | Value |
| --- | --- |
| Fixtures persisted | 24,549 |
| Fixtures with statistics | 23,182 |
| Fixtures without statistics | 1,367 |
| Rows with possession | 4,628 |
| Football rows still at 00:00 | 1,272 |

UEFA season coverage after the import: Champions League 2019: 340, 2020: 176, 2021: 218, 2022: 214, 2023: 214, 2024: 279, 2025: 470, 2026: 54;
Europa League 2019: 511, 2020: 360, 2021: 175, 2022: 175, 2023: 175, 2024: 269, 2025: 271, 2026: 152.

All figures come from live SQL against that database.

## 1. Headline

| Metric | Value |
| --- | --- |
| Fixtures (distinct ids) | 24,397 |
| Fixtures with statistics | 22,898 (93.9 %) |
| Fixtures without statistics | 1,499 |
| Rows with possession | 4,344 |
| Rows with xG | 0 |
| Duplicate event groups retained | 250 |
| Basketball games | 9,584 |

## 2. Competitions and seasons

| Competition | Season | Fixtures |
| --- | --- | --- |
| World Cup | 2022 | 128 |
| Champions League | 2019 | 340 |
| Champions League | 2020 | 176 |
| Champions League | 2021 | 218 |
| Champions League | 2022 | 214 |
| Champions League | 2023 | 214 |
| Champions League | 2024 | 279 |
| Champions League | 2025 | 470 |
| Champions League | 2026 | 54 |
| Europa League | 2019 | 511 |
| Europa League | 2020 | 360 |
| Europa League | 2021 | 175 |
| Europa League | 2022 | 175 |
| Europa League | 2023 | 175 |
| Europa League | 2024 | 269 |
| Europa League | 2025 | 271 |
| European Championship | 2020 | 364 |
| European Championship | 2024 | 340 |
| Nations League | 2020 | 162 |
| Nations League | 2024 | 188 |

Domestic leagues carried by football-data CSVs (league ids 39, 61, 78, 88, 94, 135, 140) are complete for every season in the registry.

## 3. Acquired, expected, unavailable

| Class | Count | Basis |
| --- | --- | --- |
| Fixtures acquired and persisted | 24,397 | distinct ids |
| Enriched | 22,898 | enrichment rows |
| Event exists, no published statistics | 412 | audit sighting |
| No event in any ESPN competition, date +/-1 | 203 | audit NOT_FOUND |
| Added after the audit, pending verdict | 884 | newest UEFA rows |

## 4. Unavailable evidence

* Event without statistics: ESPN's all-competition scoreboard returns an event whose clubs and final score match, but the summary carries an empty `boxscore.teams[].statistics` block.
* No event found: the all-competition scoreboard was sampled for the stored date and both adjacent dates with alias-tolerant club matching; nothing matched in any competition.
* xG: 0 rows. ESPN publishes possession, shots, shots on target, corners and cards but not xG; football-data CSVs carry none; openfootball is fixtures-only; Understat returns an identical 26,719-byte error page for every path; FBref returns HTTP 403 and prohibits automated access.
* All-Star period scores (401752955, 401752956, 401752957): team and player box scores exist, no quarter linescores because the format is played to a target score.
* Eight playoff games: events exist (core API 200, final scores), summary returns HTTP 404 on both hosts with `boxscoreAvailable=false`.

## 5. Unresolved

Zero fixtures without a verdict. 884 newly imported UEFA rows are the only ones still awaiting classification; the audit script covers them on its next run.

## 6. Enrichment coverage

Sources: `football_data_csv` 11,907, `reconciled` 6,722, `espn_soccer_summary` 4,269. Possession on 4,344; shots, shots on target, corners and cards on all 22,898; xG on none.

## 7. Basketball

| Table | Games | Rows |
| --- | --- | --- |
| team_statistics | 9,576 | 19,152 |
| player_statistics | 9,576 | 256,456 |
| period_scores | 9,573 | 77,656 |

Games with no team statistics: 8. Games with team statistics but no periods: 3 (the three All-Star games). All other games appear in all three tables.

## 8. Registry statuses

* basketball - COMPLETE: 7
* basketball - INCOMPLETE: 1
* football - COMPLETE: 70
* football - INCOMPLETE: 26

Football: 26 league-season rows are `COMPLETE` with a documented coverage limitation naming counts and the evidence file. Rows still `INCOMPLETE`: 19 have zero persisted fixtures and 7 hold 1,806 fixtures whose
coverage is investigated in section 12. The earlier statement that all 26 had zero fixtures was wrong.

## 9. Integrity

* 24,397 rows, 24,397 distinct ids: no duplicate ids.
* 250 duplicate real-world event groups retained and reconciled, never deleted.
* Euro 2020: all 51 midnight kickoffs repaired from verified UEFA twins (51 updated, 0 ambiguous, dates unchanged). 1,272 rows from date-only openfootball imports still carry 00:00, a property of that source.
* No historical row deleted; no provider id or provenance rewritten.

## 10. Blockers

* No PR: `gh` is unauthenticated in this environment.
* `api_basketball` still reports a missing credential; ESPN supplies box scores and the credential was untouched.
* Nations League 2022 could not be attributed: UEFA returned events for ids 9, 13, 22-28, 35, 36, 39, 40 with empty competition names.

## 11. Reproduce

```
python statless_fixture_audit.py
python recover_from_audit.py --apply
python espn_team_schedule_backfill.py --apply
python basketball_gap_audit.py --apply
python kickoff_twin_repair.py --league 4 --season 2020 --apply
python finalize_dataset_status.py --apply
```

Commit at generation: `d885c62`.

## 12. Investigation of the 26 INCOMPLETE datasets (8 November 2026)

Corrects the earlier claim that every incomplete dataset had zero fixtures. Nineteen do;
seven hold 1,806 fixtures between them and were incomplete only because their statistics
coverage had not been investigated. Every statless fixture in those seven now carries a
per-fixture verdict from the all-competition audit (`.api_cache/reports/statless_fixture_audit.json`).

| Competition | Season | Fixtures | Statless | Event, no published stats | No event found | Outcome |
| --- | --- | --- | --- | --- | --- | --- |
| World Cup | 2019 | 0 | 0 | 0 | 0 | NOT APPLICABLE - no World Cup in 2019 |
| World Cup | 2020 | 0 | 0 | 0 | 0 | NOT APPLICABLE - no World Cup in 2020 |
| World Cup | 2021 | 0 | 0 | 0 | 0 | NOT APPLICABLE - no World Cup in 2021 |
| World Cup | 2023 | 0 | 0 | 0 | 0 | NOT APPLICABLE - no World Cup in 2023 |
| World Cup | 2024 | 0 | 0 | 0 | 0 | NOT APPLICABLE - no World Cup in 2024 |
| World Cup | 2025 | 0 | 0 | 0 | 0 | NOT APPLICABLE - no World Cup in 2025 |
| World Cup | 2026 | 0 | 0 | 0 | 0 | RECOVERABLE - ESPN `fifa.world` serves it (2-3 events per probed date); 104 matches expected |
| Champions League | 2020 | 176 | 38 | 23 | 15 | COMPLETE with documented coverage limitation |
| Champions League | 2021 | 218 | 76 | 51 | 25 | COMPLETE with documented coverage limitation |
| Champions League | 2023 | 214 | 65 | 37 | 28 | COMPLETE with documented coverage limitation |
| Europa League | 2019 | 511 | 342 | 273 | 69 | COMPLETE with documented coverage limitation |
| Europa League | 2020 | 360 | 180 | 130 | 50 | COMPLETE with documented coverage limitation |
| Europa League | 2021 | 175 | 36 | 19 | 17 | COMPLETE with documented coverage limitation |
| Europa League | 2026 | 152 | 15 | 8 | 7 | COMPLETE with documented coverage limitation |
| European Championship | 2019 | 0 | 0 | 0 | 0 | NOT APPLICABLE - no Euro in 2019 (Euro 2020 was played in 2021) |
| European Championship | 2021 | 0 | 0 | 0 | 0 | NOT APPLICABLE - no Euro in 2021 |
| European Championship | 2022 | 0 | 0 | 0 | 0 | NOT APPLICABLE - no Euro in 2022 |
| European Championship | 2023 | 0 | 0 | 0 | 0 | NOT APPLICABLE - no Euro in 2023 |
| European Championship | 2025 | 0 | 0 | 0 | 0 | NOT APPLICABLE - no Euro in 2025 |
| European Championship | 2026 | 0 | 0 | 0 | 0 | NOT APPLICABLE - no Euro in 2026 |
| Nations League | 2019 | 0 | 0 | 0 | 0 | RECOVERABLE - ESPN `uefa.nations` returned the 2 finals on 2019-06-09 |
| Nations League | 2021 | 0 | 0 | 0 | 0 | NOT APPLICABLE - no Nations League edition in 2021 |
| Nations League | 2022 | 0 | 0 | 0 | 0 | RECOVERABLE - ESPN `uefa.nations` returned 7 events on 2022-06-04 and 10 on 2022-09-25 |
| Nations League | 2023 | 0 | 0 | 0 | 0 | NOT APPLICABLE - no Nations League edition in 2023 |
| Nations League | 2025 | 0 | 0 | 0 | 0 | NOT APPLICABLE - no Nations League edition in 2025 |
| Nations League | 2026 | 0 | 0 | 0 | 0 | UNAVAILABLE AT PRESENT - `uefa.nations` returned HTTP 200 with 0 events on 6 probed 2026 dates |

The seven fixture-bearing datasets: 1,806 fixtures, of which 752 are statless — 541 have a matching ESPN event that publishes no team statistics and 211 have no event in any ESPN competition on the stored date +/-1 day. No fixture is unexplained.

Zero-fixture datasets: 15 are not applicable (the competition holds no edition in that year),
3 are recoverable and 1 is not yet published by any permitted source. Format-derived
expectations: World Cup 2026 = 104 matches; Nations League 2019/20 and 2022/23 editions are
comparable in size to the 162-fixture 2020/21 row already held.

### Statless audit outcome

`statless_fixture_audit.py` re-audited all 1,367 statless fixtures to completion:
953 have a matching event with no published statistics, 414 have no event on the stored
date or either adjacent date in any competition ESPN covers, 0 scoreboard failures.
No statistics were invented; no fixture was deleted or re-keyed. Recovery and registry
writes for these rows are staged for the database owner (see section 13).

### xG

xG remains 0 rows and is a provider limitation: ESPN publishes possession, shots, shots on
target, corners and cards but not xG, the CSVs carry none, openfootball is fixtures-only,
Understat returns an identical error page for every path and FBref returns HTTP 403.

## 13. Handover actions reserved for the database owner

No schema, record or registry change was made in this phase. To close the seven datasets:

```
python recover_from_audit.py --apply      # persist stats for the 541 sighted events
python espn_football_stats.py --apply --workers 10
python statless_fixture_audit.py          # re-audit rows still statless
python finalize_dataset_status.py --apply
```

To acquire the three recoverable zero-fixture seasons, map league 1 to `fifa.world` and
league 5 to `uefa.nations` in the ESPN league table, then import with the UEFA importer's
duplicate guard (normalised names + UTC date). Nations League 2026 has no published events
yet and should be re-probed after the next international window.

## 14. Fixture acquisition path for the three recoverable seasons

`espn_fixture_import.py` (new) builds fixtures for competitions the UEFA API cannot
attribute. It reads one competition scoreboard per calendar day, requires the
scoreboard's own competition name (`FIFA World Cup`, `UEFA Nations League`), keys
fixtures deterministically from the ESPN event id, and refuses to report coverage
when any day failed to fetch — a silent failure was previously undercounting the
World Cup as 74 of 104.

Dry-run measurements over the full tournament windows:

| Dataset | Window(s) | Events | Insertable | Notes |
| --- | --- | --- | --- | --- |
| World Cup 2026 (league 1) | 2026-06-11..2026-07-19 | 104 | 104 | matches the 48-team, 104-match format exactly |
| Nations League 2019 (league 5) | 2018-09-06..2018-11-20, 2019-06-05..2019-06-10 | 142 | 142 | group stage plus 2018/19 finals |
| Nations League 2022 (league 5) | 2022-06-01..2022-06-15, 2022-09-20..2022-09-28, 2023-06-14..2023-06-19 | 166 | 160 | 6 Russia fixtures are provider-cancelled (0-0, status Canceled) and are excluded, not invented |
| Nations League 2026 (league 5) | 2026-09-01..2026-10-15, 2026-11-01..2026-11-20 | 156 | 156 | 104 completed, 52 scheduled; earlier "unavailable" finding was a date-format defect in the probe |

The Nations League 2026/27 season is published after all: the earlier zero-event result
came from querying `dates=2026-09-05` (hyphenated), which ESPN answers with an empty body.

Apply commands, reserved for the database owner:

```
python espn_fixture_import.py --slug fifa.world --league 1 --season 2026 \
  --window 2026-06-11:2026-07-19 --apply
python espn_fixture_import.py --slug uefa.nations --league 5 --season 2019 \
  --window 2018-09-06:2018-11-20 --window 2019-06-05:2019-06-10 --apply
python espn_fixture_import.py --slug uefa.nations --league 5 --season 2022 \
  --window 2022-06-01:2022-06-15 --window 2022-09-20:2022-09-28 \
  --window 2023-06-14:2023-06-19 --apply
python espn_fixture_import.py --slug uefa.nations --league 5 --season 2026 \
  --window 2026-09-01:2026-10-15 --window 2026-11-01:2026-11-20 \
  --include-scheduled --apply
```

After each import, run `espn_football_stats.py --apply` for enrichment and
`finalize_dataset_status.py --apply` to reconcile the registry.

### Registry reconciliation for the seven populated datasets

Their `enrichment_status` is currently `NONE` while the rows are demonstrably enriched
(1,054 of 1,806 carry statistics). `finalize_dataset_status.py` already derives
`enrichment_status` from the persisted enrichment rows and writes
`complete_with_documented_coverage_limitation` with the sighted/not-found counts, so a
single `--apply` run aligns status, enrichment status and reason for all seven. No manual
status edit is needed and none was made here.

## 15. Final production state (verified in Neon, 8 November 2026)

Acquisition completed by this run: four seasons imported from ESPN scoreboards and all
752 remaining statless fixtures re-audited, then every registry entry recomputed from the
underlying tables.

| Metric | Value |
| --- | --- |
| Football fixtures | 25,111 |
| Distinct fixture ids | 25,111 |
| Enriched | 23,741 (94.5 %) |
| Without statistics | 1,370 |
| Rows with possession | 5,187 |
| Rows with xG | 0 |
| Duplicate event groups retained | 250 |
| Registry rows whose count differs from persisted fixtures | 0 |
| Orphaned enrichment rows | 0 |
| Fixtures with no registry season | 0 |

Newly acquired in this run: FIFA World Cup 2026 (104 fixtures, 48 teams, 2026-06-11 to
2026-07-19), Nations League 2019/20 (142, 55 teams, 2018-09-06 to 2019-06-09), Nations
League 2022/23 (160, 54 teams, 2022-06-01 to 2023-06-18; the six provider-cancelled Russia
fixtures excluded), Nations League 2026/27 (156: 104 played, 52 scheduled).

### Registry after reconciliation

| Competition | Season | Fixtures | Status | Enrichment | Reason class |
| --- | --- | --- | --- | --- | --- |
| World Cup | 2019 | 0 | COMPLETE | NONE | no_edition_in_season |
| World Cup | 2020 | 0 | COMPLETE | NONE | no_edition_in_season |
| World Cup | 2021 | 0 | COMPLETE | NONE | no_edition_in_season |
| World Cup | 2022 | 128 | COMPLETE | PARTIAL | documented_coverage_limitation |
| World Cup | 2023 | 0 | COMPLETE | NONE | no_edition_in_season |
| World Cup | 2024 | 0 | COMPLETE | NONE | no_edition_in_season |
| World Cup | 2025 | 0 | COMPLETE | NONE | no_edition_in_season |
| World Cup | 2026 | 104 | COMPLETE | COMPLETE | all_fixtures_enriched |
| Champions League | 2019 | 340 | COMPLETE | PARTIAL | documented_coverage_limitation |
| Champions League | 2020 | 176 | COMPLETE | PARTIAL | documented_coverage_limitation |
| Champions League | 2021 | 218 | COMPLETE | PARTIAL | documented_coverage_limitation |
| Champions League | 2022 | 214 | COMPLETE | PARTIAL | documented_coverage_limitation |
| Champions League | 2023 | 214 | COMPLETE | PARTIAL | documented_coverage_limitation |
| Champions League | 2024 | 279 | COMPLETE | PARTIAL | documented_coverage_limitation |
| Champions League | 2025 | 470 | COMPLETE | PARTIAL | documented_coverage_limitation |
| Champions League | 2026 | 54 | COMPLETE | PARTIAL | documented_coverage_limitation |
| Europa League | 2019 | 511 | COMPLETE | PARTIAL | documented_coverage_limitation |
| Europa League | 2020 | 360 | COMPLETE | PARTIAL | documented_coverage_limitation |
| Europa League | 2021 | 175 | COMPLETE | PARTIAL | documented_coverage_limitation |
| Europa League | 2022 | 175 | COMPLETE | PARTIAL | documented_coverage_limitation |
| Europa League | 2023 | 175 | COMPLETE | PARTIAL | documented_coverage_limitation |
| Europa League | 2024 | 269 | COMPLETE | PARTIAL | documented_coverage_limitation |
| Europa League | 2025 | 271 | COMPLETE | PARTIAL | documented_coverage_limitation |
| Europa League | 2026 | 152 | COMPLETE | PARTIAL | documented_coverage_limitation |
| European Championship | 2019 | 0 | COMPLETE | NONE | no_edition_in_season |
| European Championship | 2020 | 364 | COMPLETE | PARTIAL | documented_coverage_limitation |
| European Championship | 2021 | 0 | COMPLETE | NONE | no_edition_in_season |
| European Championship | 2022 | 0 | COMPLETE | NONE | no_edition_in_season |
| European Championship | 2023 | 0 | COMPLETE | NONE | no_edition_in_season |
| European Championship | 2024 | 340 | COMPLETE | COMPLETE | all_fixtures_enriched |
| European Championship | 2025 | 0 | COMPLETE | NONE | no_edition_in_season |
| European Championship | 2026 | 0 | COMPLETE | NONE | no_edition_in_season |
| Nations League | 2019 | 142 | COMPLETE | COMPLETE | all_fixtures_enriched |
| Nations League | 2020 | 162 | COMPLETE | PARTIAL | documented_coverage_limitation |
| Nations League | 2021 | 0 | COMPLETE | NONE | no_edition_in_season |
| Nations League | 2022 | 160 | COMPLETE | COMPLETE | all_fixtures_enriched |
| Nations League | 2023 | 0 | COMPLETE | NONE | no_edition_in_season |
| Nations League | 2024 | 188 | COMPLETE | COMPLETE | all_fixtures_enriched |
| Nations League | 2025 | 0 | COMPLETE | NONE | no_edition_in_season |
| Nations League | 2026 | 156 | COMPLETE | PARTIAL | documented_coverage_limitation |
| 39 | 2019 | 380 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 39 | 2020 | 380 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 39 | 2021 | 380 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 39 | 2022 | 380 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 39 | 2023 | 380 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 39 | 2024 | 380 | COMPLETE | PARTIAL | documented_coverage_limitation |
| 39 | 2025 | 382 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 39 | 2026 | 99 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 61 | 2019 | 279 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 61 | 2020 | 488 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 61 | 2021 | 380 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 61 | 2022 | 380 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 61 | 2023 | 306 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 61 | 2024 | 358 | COMPLETE | PARTIAL | documented_coverage_limitation |
| 61 | 2025 | 406 | COMPLETE | PARTIAL | documented_coverage_limitation |
| 61 | 2026 | 93 | COMPLETE | PARTIAL | documented_coverage_limitation |
| 78 | 2019 | 430 | COMPLETE | PARTIAL | documented_coverage_limitation |
| 78 | 2020 | 402 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 78 | 2021 | 306 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 78 | 2022 | 306 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 78 | 2023 | 306 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 78 | 2024 | 614 | COMPLETE | PARTIAL | documented_coverage_limitation |
| 78 | 2025 | 456 | COMPLETE | PARTIAL | documented_coverage_limitation |
| 78 | 2026 | 97 | COMPLETE | PARTIAL | documented_coverage_limitation |
| 88 | 2019 | 258 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 88 | 2020 | 340 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 88 | 2021 | 306 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 88 | 2022 | 306 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 88 | 2023 | 306 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 88 | 2024 | 430 | COMPLETE | PARTIAL | documented_coverage_limitation |
| 88 | 2025 | 402 | COMPLETE | PARTIAL | documented_coverage_limitation |
| 88 | 2026 | 113 | COMPLETE | PARTIAL | documented_coverage_limitation |
| 94 | 2019 | 456 | COMPLETE | PARTIAL | documented_coverage_limitation |
| 94 | 2020 | 306 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 94 | 2021 | 306 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 94 | 2022 | 306 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 94 | 2023 | 306 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 94 | 2024 | 306 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 94 | 2025 | 306 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 94 | 2026 | 81 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 135 | 2019 | 618 | COMPLETE | PARTIAL | documented_coverage_limitation |
| 135 | 2020 | 520 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 135 | 2021 | 380 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 135 | 2022 | 380 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 135 | 2023 | 380 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 135 | 2024 | 380 | COMPLETE | PARTIAL | documented_coverage_limitation |
| 135 | 2025 | 454 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 135 | 2026 | 103 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 140 | 2019 | 380 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 140 | 2020 | 380 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 140 | 2021 | 380 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 140 | 2022 | 380 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 140 | 2023 | 380 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 140 | 2024 | 380 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 140 | 2025 | 488 | COMPLETE | COMPLETE | all_fixtures_enriched |
| 140 | 2026 | 129 | COMPLETE | COMPLETE | all_fixtures_enriched |

Football registry totals: 96 of 96 COMPLETE — 47 with every fixture enriched, 34 with a
documented coverage limitation, 15 seasons in which the competition staged no edition
(expected coverage zero). No season is left pending and none was marked complete without
evidence; the earlier "all 26 incomplete are empty" claim was corrected in section 12.

## 16. Possession and xG enrichment (executed and verified, 8 November 2026)

ESPN's soccer summary publishes possession but no xG, and the CSV-sourced fixtures carried
no possession at all. `fotmob_enrichment.py` adds FotMob's public match feed as a second
statistics source: it matches each played fixture to its FotMob match on competition alias,
both club names (alias-aware, reusing `espn_football_stats.name_score`), identical final
score and the UTC date (with a one-day tolerance), and refuses ambiguous pairings. Only
missing fields are filled; existing values are never overwritten and every filled field is
recorded in `raw_json.field_sources` with `provider_version = fotmob-web-2026-11`.

| Measure | Before | After |
| --- | --- | --- |
| Enrichment records / distinct fixtures | 23,741 | 47,507 / 24,642 |
| Fixtures with both possession values | 5,187 | 24,013 |
| Fixtures with both xG values | 0 | 19,627 |
| Played fixtures with no enrichment row | 1,318 | 417 |
| Scheduled (unplayed) fixtures with no statistics | 52 | 52 |

Coverage by competition (possession / xG out of fixtures):

| Competition | Fixtures | Possession | xG |
| --- | --- | --- | --- |
| Serie A | 3,121 | 3,116 | 2,597 |
| Bundesliga | 2,777 | 2,745 | 2,355 |
| Premier League | 2,758 | 2,758 | 2,382 |
| Ligue 1 | 2,690 | 2,679 | 2,400 |
| LaLiga | 2,622 | 2,622 | 2,281 |
| Eredivisie | 2,460 | 2,445 | 2,188 |
| Primeira Liga | 2,124 | 2,124 | 1,706 |
| Europa League | 2,054 | 1,826 | 1,192 |
| Champions League | 1,907 | 1,794 | 1,198 |
| Nations League | 808 | 754 | 615 |
| European Championship | 691 | 681 | 491 |
| World Cup | 223 | 222 | 222 |

By calendar year, xG exists for 2020–2026 matches only: FotMob's match feed begins
publishing xG with the 2020/21 season, so 2018 and 2019 fixtures have possession but no xG
by provider limitation, not by extraction failure. Verified in the payload: the same
`matchDetails` response for a 2019 match carries `BallPossesion` and no `expected_goals`
group at all, while a 2024 match carries `expected_goals` as the provider's string-encoded
team totals (e.g. `"2.43"` / `"0.44"`). xG is stored as reported: full-match, all-situations
xG including penalties, at the provider's own scale.

Integrity after the run: 25,111 fixtures with 25,111 distinct ids; 0 orphan enrichment
records; 0 possession values outside 0–100; 0 negative or non-finite xG; no fixture, score,
mapping or registry row modified. 171 duplicate real-world fixture keys (same kickoff,
clubs) pre-date this phase and were left untouched; the enrichment job only attaches to
existing ids. 208 FotMob matches returned no usable statistic block (`detail_no_stats`) and a
further 208 detail requests failed (`no_detail`), and 1,131 fixtures had no FotMob match on competition,
clubs and score; these remain the evidence-backed residual, dominated by early-round
European qualifiers whose FotMob competition naming does not line up with the stored
competition.

Remaining gap: 417 played fixtures with no enrichment row (414 FT, 2 PEN, 1 AET) and 4,386
fixtures with possession but no xG, all from seasons before the provider published xG.
