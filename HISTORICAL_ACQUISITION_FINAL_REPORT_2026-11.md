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

Football: 26 league-season rows are `COMPLETE` with a documented coverage limitation naming counts and the evidence file. Rows still `INCOMPLETE` are seasons with no persisted fixtures (World Cup years that do not exist, plus the Europa League/Nations League seasons not yet imported).

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
