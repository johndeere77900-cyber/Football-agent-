# Historical Data Source Inventory

Status legend:

- **VERIFIED AND APPROVED** — tested against the live source this cycle, evidence recorded below, safe to use for the batches named here.
- **DISCOVERED / UNVERIFIED** — reachable or partly reachable, but fails at least one verification criterion. Not usable until the stated gap is closed.
- **REJECTED** — tested and found unsuitable for this purpose.

No production rows were written while producing this inventory. Every probe was read-only.

## 1. VERIFIED AND APPROVED

### 1.1 `espn_soccer_summary` — NEW this cycle

| Attribute | Value |
|---|---|
| Provider | ESPN public site API (`site.api.espn.com`) |
| Access method | `GET /apis/site/v2/sports/soccer/{competition}/scoreboard?dates=YYYYMMDD`, then `GET .../summary?event={event_id}` |
| Competitions | `uefa.champions`, `uefa.europa`, `uefa.euro`, `fifa.world`, `uefa.nations` |
| Seasons verified | UCL 2019 and 2024; Europa League 2022 and 2024; Euro 2020 (played 2021) and 2024; World Cup 2022; Nations League 2020 and 2024 |
| Team statistics provided | `totalShots`, `shotsOnTarget`, `wonCorners`, `yellowCards`, `redCards`, `possessionPct`, `foulsCommitted`, `offsides`, `saves`, plus passing/crossing metrics and `penaltyKickGoals` |
| Period data | Yes — `header.competitions[].competitors[].linescores` |
| Player statistics | **No** — `boxscore.players` is empty for every soccer match sampled |
| xG | **No** — no expected-goals field in the payload |
| Verification evidence | Scoreboard and summary returned HTTP 200 for all five competitions. UCL 2019: 28 events across 16 match dates, statistics present on every date sampled. Europa League 2022/2024: team statistics present on 6 of 6 sampled matches. Euro 2021/2024, World Cup 2022, Nations League 2020/2024: all nine required statistic fields present. |
| Identity matching | Events carry `competitions[].competitors[].homeAway` and `team.displayName`; events are addressed by ESPN numeric `event.id` and ISO `date`. |
| Known limitations | (a) Date-range queries return **HTTP 400**; coverage must be collected one date at a time. (b) No player-level statistics. (c) No xG. (d) Display names differ from stored names in places, so a reconciliation step is mandatory. |
| Batches it can supply | Football leagues 1–5 (2,065 fixtures currently without statistics), subject to reconciliation below. |

**Measured reconciliation rate** (sampled up to 15 distinct match dates per league-season, exact-date matching only):

| League | Season | Fixtures probed | Matched | Rate |
|---|---|---|---|---|
| 1 World Cup | 2022 | 30 | 30 | 100% |
| 2 Champions League | 2019 | 23 | 4 | 17% |
| 2 Champions League | 2022 | 29 | 0 | 0% |
| 2 Champions League | 2024 | 30 | 0 | 0% |
| 2 Champions League | 2025 | 30 | 4 | 13% |
| 2 Champions League | 2026 | 14 | 2 | 14% |
| 3 Europa League | 2022 | 28 | 12 | 43% |
| 3 Europa League | 2023 | 27 | 14 | 52% |
| 3 Europa League | 2024 | 28 | 6 | 21% |
| 4 Euro | 2020 | 29 | 15 | 52% |
| 4 Euro | 2024 | 29 | 29 | 100% |
| 5 Nations League | 2020 | 28 | 23 | 82% |
| 5 Nations League | 2024 | 30 | 28 | 93% |
| **Total** | | **355** | **167** | **47%** |

The 0% Champions League rows are a date-alignment/name-alignment failure, not missing coverage: the same competition returned statistics for 2019 events. The stored fixtures come from sources whose kickoff is date-only or whose team naming differs, so the batch **must** apply a ±1 day window plus name reconciliation and a dry-run review before any write. Approving the source does not approve an unreviewed 47% match rate.

### 1.2 Existing approved sources (unchanged)

- `openfootball_json`, `openfootball_txt`, `openfootball_internationals`, `openligadb` — fixtures/results only; no match statistics.
- `football_data_csv` — approved; supplies shots, shots on target, corners, cards and fouls for the seven mapped domestic divisions. Already used for the 482 enrichments in production.
- `espn_boxscore` (basketball) — approved; supplies team boxscores, period scores and player lines for NBA.

## 2. DISCOVERED / UNVERIFIED

### 2.1 StatsBomb Open Data

- Reachable: `https://raw.githubusercontent.com/statsbomb/open-data/master/data/competitions.json` returned HTTP 200 with **80 competition-seasons**, including Champions League editions from 1970/71 and World Cup/Euro seasons.
- Supplies event-level data from which shots, xG and lineups can be derived — the only candidate found that offers **xG** for these competitions.
- **Not approvable yet**: records are keyed by StatsBomb match ids that our fixtures do not store, so identity resolution needs a matching pass first; and the dataset is licensed for non-commercial use, which must be confirmed as acceptable before production use.
- Required before approval: identity mapping evidence, licence confirmation, field-level extraction proof on a sample match.

### 2.2 FotMob

- Probed `https://www.fotmob.com/api/matchDetails?matchId=4193843` → **HTTP 404** with an arbitrary id, so availability could not be established.
- Not rejected, but no evidence of usable access. Needs a valid match id and a stable endpoint contract before any claim.

## 3. REJECTED

| Source | Probe | Reason |
|---|---|---|
| `soccerdata_fbref` / `soccerdata_match_history` | `import soccerdata` → ModuleNotFoundError; `fbref.com` → **HTTP 403** | Already in the approved order but non-functional in this environment. Should be reported as `UNSUPPORTED_COVERAGE`, not silently skipped. |
| Understat | `https://understat.com/league/EPL/2023` → HTTP 200 | Page loads but carries none of the expected data objects (`playersData`, `datesData`, `teamsData` absent), so no xG is extractable. |
| `stats.nba.com` | Connection failed (HTTP 000) | Blocked from this environment; ESPN remains the working basketball source. |
| TheSportsDB | `eventsseason.php` → **HTTP 403** | Access denied. |
| football-data.co.uk "new leagues" | `new/BRA.csv` → HTTP 200 | Columns are Country, League, Season, Date, Home, Away, HG, AG only — results without statistics, and none of the five affected competitions. |

## 4. What each approved source can recover

| Source | Recoverable fixtures | Recoverable fields |
|---|---|---|
| `espn_soccer_summary` | Up to 2,065 across leagues 1–5, subject to the measured reconciliation rate | shots, shots on target, corners, yellow/red cards, possession, fouls, offsides, saves |
| `football_data_csv` | The 2,203 remaining domestic fixtures where a CSV row exists | shots, shots on target, corners, cards, fouls |
| `espn_boxscore` | 8,737 remaining NBA games | team boxscore, period scores, player lines |

**Not recoverable from any approved source:** xG for football competitions 1–5 (StatsBomb is the only candidate and is unverified), player-level football statistics for those competitions (ESPN soccer returns none), and odds/market history (out of scope).

## 5. Prepared batches (NOT APPLIED)

Football, competitions 1–5, per competition-season using `espn_soccer_summary`:

1. Collect scoreboards for each stored fixture's match date (`dates=YYYYMMDD`), retaining the raw payload.
2. Resolve each stored fixture to at most one ESPN event using date (±1 day), normalised home/away names, and score agreement where available.
3. Reject rather than guess: no unique match, no statistics payload, or a score disagreement means no write and a recorded reason.
4. Dry-run the whole competition-season and report the match rate; only then attach statistics.

Basketball: the eight per-season batches already listed in `ENRICHMENT_REMAINING_PLAN.md`.

## 6. Proposed SQL (NOT APPLIED)

Read-only checks to run before authorising any batch:

```sql
-- 1. Fixtures still without statistics, per competition-season
with e as (select fixture_id from historical_fixture_enrichment where home_shots is not null group by 1)
select f.league_id, f.season, count(*) total, count(e.fixture_id) enriched
  from historical_fixtures f left join e on e.fixture_id=f.fixture_id
 where f.league_id in (1,2,3,4,5) group by 1,2 order by 1,2;

-- 2. Confirm the target row really has no statistics before writing
select fixture_id, source, field_mask
  from historical_fixture_enrichment
 where fixture_id = :fixture_id;
```

Write shape, once authorised (one row per fixture, additive merge, provenance preserved):

```sql
-- Proposal only. The application path must remain storage.save_historical_enrichment,
-- which extracts typed columns, builds the field mask and refuses to overwrite a
-- verified value with NULL.
-- source: 'espn_soccer', provider_version: 'espn_soccer_summary_v2'
```

A new source value requires it to be added to the writer's recognised sources; that change is a code change in `storage.save_historical_enrichment`, not a database mutation, and is not applied here.

## 7. Gaps that still cannot be recovered

- **xG** for any football competition. No approved source supplies it; StatsBomb is the only candidate and remains unverified (identity + licence).
- **Player-level football statistics** for competitions 1–5. ESPN soccer returns none, and no other approved source covers those competitions.
- **Possession and xG for domestic fixtures already enriched**: possession for leagues 1–5 becomes available through ESPN soccer, but the domestic CSV rows remain without possession because that provider does not publish it.
- **UCL 2022–2026**: coverage exists at the provider, but identity reconciliation currently succeeds on 0–14% of sampled fixtures, so those seasons are only recoverable after the matching problem is solved and reviewed.
