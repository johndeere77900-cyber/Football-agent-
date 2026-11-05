# Historical Acquisition: Recovery Assessment and Complete Work Audit

This document separates what was **executed**, what was **prepared**, what was
**verified against real data**, what is **inferred** and what was **not attempted**.
Companion documents: `APPROVED_DATA_SOURCES.md` (source inventory and evidence) and
`ENRICHMENT_REMAINING_PLAN.md` (batch inventory).

## 1. Additional recovery opportunities discovered

1. **Team alias table** for ESPN soccer. Confirmed neighbours in the same ESPN fixture
   lists: `psv`↔`psveindenhoven`, `unionsg`↔`unionstgilloise`, `slbenfica`↔`benfica`,
   `qarabagagdam`↔`fkqarabag`, `dortmund`↔`borussiadortmund`. This is the cheapest way to
   convert real matches into positives; the 0.86 similarity threshold is what rejects them.
2. **ESPN qualifying-round competition slug.** UCL fixtures stored as qualification ties
   (`2024-07-09 Panevezys vs HJK`, `2022-06-21 La Fiorita vs Inter Escaldes`) return an empty
   `uefa.champions` scoreboard. A separate slug must exist and has not been identified yet.
3. **Stored-date integrity review.** Our data places `2019-08-07 Juventus vs Lyon` and
   `2019-08-07 Manchester City vs Real Madrid` on a date when only third-qualifying-round ties
   were played. Those records cannot match any provider and must be re-examined at source
   rather than forced onto whatever ESPN lists that day.
4. **Score-agreement gating.** Accept an ESPN match only when date, both teams and the final
   score agree, which prevents alias-driven false positives.
5. **StatsBomb for 2018/19 Champions League xG.** The only xG route found; per-season match
   counts remain unmeasured.
6. **basketball-reference** as an independent cross-check for ESPN basketball boxscores
   (team totals and per-player columns parse from HTML).
7. **Contract extension required**: ESPN soccer returns `foulsCommitted`, `offsides` and
   `saves`, but the football enrichment contract only stores shots, shots on target, corners,
   cards, possession and xG. Storing those three fields needs an additive schema change that
   has **not** been proposed as applied, let alone applied.

## 2. Competition and season recovery assessment

The full per-competition table lives in `APPROVED_DATA_SOURCES.md` §6. Summary:

- **Recoverable now**: World Cup 2022 (128 fixtures, 100% measured), Euro 2024 (51, 100%),
  Nations League 2024 (188, 93%), Nations League 2020 (162, 82%).
- **Partially recoverable**: Euro 2020 (51, 52%), Europa League 2022/2023/2024 (175/175/269 at
  43%/52%/21%), UCL 2019/2025/2026 (aliases and date review required).
- **Blocked**: UCL 2022 (214) and 2024 (279) — sampled failures are qualifying rounds absent
  from the main competition slug.
- **Domestic**: 2,203 fixtures across 38 league-seasons, recoverable only where a CSV row
  genuinely exists; 186 rows are already proven to have no counterpart fixture.

## 3. Remaining gaps, with evidence

| Gap | Count | Evidence |
|---|---|---|
| Football fixtures without statistics | 4,268 | SQL coverage query over `historical_fixtures` vs statistics rows |
| — of which competitions 1–5 | 2,065 | same query, `league_id IN (1,2,3,4,5)` |
| — of which domestic | 2,203 | remainder, 38 league-seasons |
| Basketball games without statistics | 8,737 of 9,584 | join on distinct `game_id` |
| xG | all football | no ESPN field; StatsBomb covers UCL only to 2018/19 and is unapproved |
| Player-level football statistics, competitions 1–5 | all | `boxscore.players` empty on every sampled ESPN soccer match |
| Possession for enriched domestic fixtures | all | football-data CSV has no possession column |
| Acquisition manifests incomplete | 77 INCOMPLETE, 1 FAILED | `historical_datasets.status` |

## 4. Complete work audit

### 4.1 Repositories and branches

- Repository: `johndeere77900-cyber/Football-agent-` only. No other project was accessed.
- `freebuff/a2ec780a649beb9940426c07` — work branch; pushed at `9d87a8d`.
- `acquisition/complete-football-and-basketball-enrichment` — current branch; pushed at
  `d166cde`, `5873788`, `9c01449`, plus the documentation commit that adds this file.
- `main` was read but never written; it remains `23cb9ca`.

### 4.2 Starting and current commit

- Start: `23cb9ca86bc7e6eaa1d2d5fe44e2fc483ddf604d`.
- Current before this documentation commit: `9c01449f1318b37ce2063a00b760c8b7e0ea1d73`.

### 4.3 Files created, edited, deleted

| File | Action | Purpose |
|---|---|---|
| `storage.py` | edited (+16 lines) | Idempotent `field_mask` `jsonb`→`text` repair in `init_db()`; committed as `9d87a8d` |
| `basketball_enrichment.py` | created | ESPN boxscore enrichment: team stats, period scores, player lines; schema creation, resumable batches |
| `football_csv_enrichment.py` | created | Approved CSV statistics fill for existing fixtures; creates no fixtures |
| `historical_provenance_cleanup.py` | created | Transaction-safe archive-then-delete of redundant provenance-only rows |
| `ENRICHMENT_REMAINING_PLAN.md` | created | Remaining batches, gaps, verification queries |
| `APPROVED_DATA_SOURCES.md` | created, then rewritten | Source inventory with four-way classification and evidence |
| `RECOVERY_ASSESSMENT_AND_AUDIT.md` | created | This audit |
| `.api_cache/*` (gitignored) | created/edited | `backfill_enrichment.py` (league/season filters, stable provenance labelling, enrichment-only status), `pgq.py`, `fix_reconciled_labels.py`, credential env files (0600), ESPN boxscore cache |
| — | deleted | none |

### 4.4 Sources investigated and endpoints tested

| Source | Endpoint tested | Result |
|---|---|---|
| ESPN soccer scoreboard | `/soccer/{comp}/scoreboard?dates=YYYYMMDD` | 200 |
| ESPN soccer summary | `/soccer/{comp}/summary?event=` | 200, statistics present |
| ESPN basketball summary | `/basketball/nba/summary?event=` | 200, boxscore present |
| football-data.co.uk | `mmz4281/{season}/{div}.csv`, `new/{CTY}.csv` | 200 (302 redirect followed) |
| openfootball | GitHub raw JSON/TXT | 200 |
| OpenLigaDB | `getmatchdata/bl1/{season}` | 200, results only |
| StatsBomb open data | `competitions.json`, `matches/16/{id}.json`, `events/{id}.json` | 200, 80 competition-seasons, xG verified |
| basketball-reference | `/boxscores/YYYYMMDD0XXX.html` | 200, tables parse |
| balldontlie | `api/v1/games` | 404 |
| cdn.nba.com | `boxscore_0022300001.json` | 403 |
| stats.nba.com | `boxscoretraditionalv2` | connection refused |
| worldfootball.net | match report | 403 |
| api.sofascore.com | `event/{id}/statistics` | 403 |
| football-data.org | `competitions/CL/matches` | 403 (no key) |
| api-football | `fixtures?id=1` | 403 (no key) |
| TheSportsDB | `eventsseason.php` | 403 |
| Understat | `/league/EPL/2023` | 200, no data objects |
| FotMob | `matchDetails`, `searchapi` | 404 |
| eu-football.info | `_match.php?id=17000` | 200, empty body |
| flashscore | homepage | 200, JS-rendered |

### 4.5 Competitions and seasons verified

- ESPN soccer: UCL 2019 and 2024; Europa League 2022 and 2024; Euro 2020 (2021) and 2024;
  World Cup 2022; Nations League 2020 and 2024.
- ESPN basketball: NBA seasons 2019–2026 present in the database; summaries sampled and
  parsed for 847 games.
- StatsBomb: Champions League 1970/71–2018/19 and eight World Cup editions listed; one match
  fully inspected for xG.
- football-data CSV: all seven mapped divisions, seasons 2019–2026 fetched during the dry-run.

### 4.6 Samples actually examined

- 355 football fixtures probed against ESPN scoreboards (identity matching).
- 60 additional UCL fixtures probed across three date offsets.
- 16 distinct UCL 2019 dates with summary statistics checks; 6 Europa League matches.
- 3,529 StatsBomb events from one match (34 shots, 34 with xG).
- 1 basketball-reference boxscore page (29 tables).
- 847 NBA games stored from ESPN summaries (1,694 team rows, 22,222 player rows, 6,878 period rows).

### 4.7 Reconciliation results

- Overall ESPN soccer identity match rate on exact dates: **167/355 = 47%**.
- Per competition-season rates and the three diagnosed causes (qualifying rounds, aliases,
  suspect stored dates) are in `APPROVED_DATA_SOURCES.md` §2.1.
- ±1 day offset added **0** matches in every UCL season tested, so date drift is not the cause.
- Football CSV matching: 482 rows planned and written with **0 ambiguous matches**.

### 4.8 Batches prepared

- Football domestic CSV: 38 league-season batches (command form in `ENRICHMENT_REMAINING_PLAN.md`).
- Football competitions 1–5: 13 competition-season batches via ESPN soccer, conditional on
  the reconciliation work in §1.
- Basketball: 8 season batches (2019 remainder 371, 2020–2026 totalling 8,366).

### 4.9 SQL prepared and executed

- Executed: read-only coverage/integrity queries (`pgq.py`), the `field_mask` type migration,
  the archive-insert + delete for 3,044 provenance-only rows, enrichment upserts, status
  updates, and the basketball schema creation.
- Prepared but **not executed**: all batch SQL in `APPROVED_DATA_SOURCES.md` §6 and
  `ENRICHMENT_REMAINING_PLAN.md` §7, and any schema change for fouls/offsides/saves.

### 4.10 Production database writes performed (all before the no-write instruction)

1. `field_mask` column converted `jsonb` → `text` (idempotent `DO` block).
2. Materialised 20,083 statistics rows; later reduced to 16,629 by the label-dedupe.
3. Removed 3,454 duplicate statistics rows created by an unstable label.
4. Archived and deleted 3,044 provenance-only rows (19,673 → 17,111 canonical rows).
5. Inserted 482 CSV-derived statistics rows (Premier League 2023, Serie A 2024).
6. Created four tables: `historical_basketball_team_statistics`,
   `historical_basketball_period_scores`, `historical_basketball_player_statistics`,
   `historical_enrichment_retired` (plus indexes).
7. Inserted 847 games' worth of basketball statistics.
8. Updated 2 football enrichment statuses and 8 basketball enrichment statuses.

**No production write occurred in the last two cycles** (source discovery and this audit);
every probe in them was read-only.

### 4.11 Schema, configuration and provider-order changes

- Schema: the four tables above exist in production; created at runtime by
  `basketball_enrichment.ensure_schema()` and the cleanup module, **not** by `storage.init_db()`.
- `storage.py` contains only the `field_mask` repair; the attempted DDL addition to `init_db()`
  failed and was never applied, so `init_db()` does **not** create basketball tables.
- Provider order, `config.py`, acquisition configuration and API Basketball credentials were
  **not** modified.

### 4.12 Commits pushed

- `9d87a8daf438ff00cf4c522856abbfa6677def27` → `freebuff/a2ec780a649beb9940426c07`
- `d166cde963fbe5421dbafc0845a1e17a60ba3098` → `acquisition/complete-football-and-basketball-enrichment`
- `58737884a7af6e3700b7bb4bb4922d23012eb47b` → same branch
- `9c01449f1318b37ce2063a00b760c8b7e0ea1d73` → same branch
- plus the documentation commit adding this file.

### 4.13 Pull request

No PR exists. `gh` reports "not logged into any GitHub hosts", so PR creation is impossible
from this environment. The branch is pushed and ready for review; the PR must be opened by you
or with working credentials.

### 4.14 What remains unfinished, and why

| Item | Status | Reason |
|---|---|---|
| Full acquisition sweep of 96 manifests | Not attempted | Production writes prohibited from the previous cycle onward |
| Enrichment of 4,268 football fixtures | Prepared only | Same restriction; UCL/Europa additionally need reconciliation |
| Basketball remaining 8,737 games | Prepared only | Same restriction |
| xG | Blocked | No approved source; StatsBomb limited and unverified |
| ESPN provider integration | Not attempted | Would change runtime behaviour; requires authorisation |
| Fouls/offsides/saves storage | Not attempted | Requires an additive schema change |
| PR creation | Blocked | No GitHub credentials |

### 4.15 Inferred versus verified

- **Verified against live data**: every HTTP status, field list, coverage probe, match rate and
  row count in this document.
- **Inferred**: that an ESPN qualifying-round slug exists (implied by empty scoreboards for real
  qualifying fixtures — not yet located); that StatsBomb's 2018/19 season holds a full season
  (only one match was inspected).
- **Not attempted**: any write, any provider integration, any acquisition run, any schema change
  for the new fields.
