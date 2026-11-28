# Data Source Inventory and Capability Evidence

Last audited: 2026-11-27 (UTC). Branch: `feat/autonomous-live-prediction-learning`.

This inventory records what each configured provider can actually do **in this
repository**, not what its public documentation advertises. A capability is
listed as `VERIFIED` only when a real response was fetched, parsed and followed
to its persistence/downstream consumer during this audit (or, where marked,
during the preceding Phase 2 verification run with the named evidence).

Legend (exact statuses used below):

- `VERIFIED` — implemented, called, parsed and observed working end to end.
- `PARTIALLY_VERIFIED` — works, but only some competitions/fields/seasons or
  only part of the path was demonstrated.
- `UNVERIFIED` — adapter exists and is wired, but no successful live
  demonstration exists for this capability.
- `UNAVAILABLE` — adapter missing, disabled, or the source currently refuses
  access.
- `HISTORICAL_ONLY` — legitimate for historical acquisition, never a current
  observation.
- `ACCESS_DENIED` — the source actively refuses automated access.
- `CREDENTIAL_BLOCKED` — required secret is absent in this environment.
- `LICENSE_BLOCKED` — licensing/terms not cleared for automated acquisition.

Never infer a capability from another capability. Discovery, historical
statistics, pre-match features, live monitoring and final-result verification
are separate rows.

---

## Capability matrix

| Provider | Sport | Discovery | Historical fixtures | Historical statistics | Pre-match features | Live in-play | Final result | Credential |
|---|---|---|---|---|---|---|---|---|
| `api_football` | football | CREDENTIAL_BLOCKED | CREDENTIAL_BLOCKED | CREDENTIAL_BLOCKED | CREDENTIAL_BLOCKED | CREDENTIAL_BLOCKED | CREDENTIAL_BLOCKED | `API_FOOTBALL_KEY` |
| `espn_soccer` | football | VERIFIED | UNVERIFIED | PARTIALLY_VERIFIED | VERIFIED | UNVERIFIED | VERIFIED | none |
| `fotmob` | football | VERIFIED | UNVERIFIED | VERIFIED | VERIFIED | UNVERIFIED | VERIFIED | none |
| `football_data_csv` (football-data.co.uk) | football | HISTORICAL_ONLY | VERIFIED | PARTIALLY_VERIFIED | HISTORICAL_ONLY | HISTORICAL_ONLY | HISTORICAL_ONLY | none |
| `openfootball` (JSON + TXT) | football | HISTORICAL_ONLY | VERIFIED | UNAVAILABLE | HISTORICAL_ONLY | HISTORICAL_ONLY | HISTORICAL_ONLY | none |
| `openligadb` | football (Bundesliga) | HISTORICAL_ONLY | VERIFIED | UNAVAILABLE | HISTORICAL_ONLY | HISTORICAL_ONLY | HISTORICAL_ONLY | none |
| `football_data_org` (api.football-data.org v4) | football | CREDENTIAL_BLOCKED | CREDENTIAL_BLOCKED | CREDENTIAL_BLOCKED | CREDENTIAL_BLOCKED | CREDENTIAL_BLOCKED | CREDENTIAL_BLOCKED | `FOOTBALL_DATA_API_KEY` |
| `uefa_match_api` | football | HISTORICAL_ONLY | PARTIALLY_VERIFIED | UNAVAILABLE | HISTORICAL_ONLY | HISTORICAL_ONLY | HISTORICAL_ONLY | none |
| `fifa_calendar` | football | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | none |
| `fbref_soccerdata` | football | ACCESS_DENIED | ACCESS_DENIED | ACCESS_DENIED | ACCESS_DENIED | ACCESS_DENIED | ACCESS_DENIED | none (terms) |
| `sofascore` (via SoccerData) | football | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | none (no adapter) |
| `statsbomb_open` | football | LICENSE_BLOCKED | LICENSE_BLOCKED | LICENSE_BLOCKED | LICENSE_BLOCKED | LICENSE_BLOCKED | LICENSE_BLOCKED | licence |
| `understat` | football | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED | none |
| `espn_nba` | basketball | VERIFIED | PARTIALLY_VERIFIED | VERIFIED | VERIFIED | UNVERIFIED | VERIFIED | none |
| `api_basketball` | basketball | CREDENTIAL_BLOCKED | CREDENTIAL_BLOCKED | CREDENTIAL_BLOCKED | CREDENTIAL_BLOCKED | CREDENTIAL_BLOCKED | CREDENTIAL_BLOCKED | `API_BASKETBALL_KEY` |
| `basketball_reference` | basketball | LICENSE_BLOCKED | LICENSE_BLOCKED | LICENSE_BLOCKED | LICENSE_BLOCKED | LICENSE_BLOCKED | LICENSE_BLOCKED | permission |
| `thesportsdb` | both | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | no adapter |

Configured football competitions: `[39, 140, 135, 78, 61, 2, 3, 5, 1, 4, 88, 94]`.
Configured basketball competition: `[12]`. Coverage beyond these lists is not
claimed.

---

## ESPN Soccer (`espn_soccer`)

- **Sport:** football. **Base URL:** `https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}`
- **Endpoints used:** `/scoreboard?dates=YYYYMMDD`, `/summary?event={event_id}`,
  `/teams/{team_id}/schedule?season={season}`.
- **Auth:** none. **Competition coverage:** every slug configured in
  `espn_football_stats.py`/`live_providers.configured_league_name` (eng.1,
  esp.1, ita.1, ger.1, fra.1, uefa.champions, etc.), gated to the configured
  competition ids.
- **Native ids:** ESPN event ids (`4018xxxxx`); ESPN team ids (e.g. Man Utd
  `360`, Leeds `357`). Never reused as API-Football ids.
- **Pagination:** none (day scoreboard / one summary).
- **Cache:** in-process TTL cache in `live_providers`; no on-disk cache for
  current discovery.
- **Adapter:** `live_providers.espn_soccer_fixtures`,
  `espn_soccer_fixture`, `espn_soccer_team_matches`;
  `espn_football_stats.py` for historical match summaries.
- **Roles:** discovery, team schedule history (recent form), pre-match derived
  statistics, match summary statistics, final-result verification.
- **Reaches the model:** yes. `main._provider_prematch_features` derives season
  statistics/recent form/H2H from the ESPN team schedule strictly before
  kickoff; the pre-match snapshot stores the resulting `feature_snapshot` plus
  `feature_provenance`.
- **Evidence (2026-11-27T20:07Z):** scoreboard
  `/soccer/eng.1/scoreboard?dates=20261128` → HTTP 200, `application/json`,
  40,418 bytes, 3 events with status `STATUS_SCHEDULED`, competitor scores and
  `displayClock`. `/summary?event=704386` → HTTP 200; `boxscore.teams[]`
  parsed into home/away statistics. That specific 2024-11-10 match returned
  all-zero ESPN statistics; event `704369` (2024-11-03) returned real values
  (Tottenham 51.1% possession, 16 shots, 6 on target; Aston Villa 48.9%,
  12 shots). All-zero ESPN statistics are recorded as a provider coverage
  limitation, not as a parser failure.
- **Gaps:** live clock/event polling is not implemented, so `live` stays
  `UNVERIFIED`; xG is not supplied by this parser; undocumented endpoint
  stability and permitted use remain a review item.

## FotMob (`fotmob`)

- **Sport:** football. **Base URL:** `https://www.fotmob.com/api/data`
- **Endpoints:** `/matches?date=YYYYMMDD`, `/matchDetails?matchId={id}`.
- **Auth:** none. **Coverage:** broad; the day listing contained 149
  competitions. Competition identity is gated by both name **and** listing
  country code (`ccode`) so a same-name league from another country cannot be
  accepted.
- **Native ids:** FotMob match ids (`5795531`) and team ids (`8463`).
- **Pagination:** none for a day; one request per match detail.
- **Adapter:** `live_providers.fotmob_fixtures`, `fotmob_fixture`,
  `fotmob_team_matches`; `fotmob_enrichment.day_matches`, `parse_detail`.
- **Roles:** discovery, team match history, possession/xG/shots/corners/cards
  enrichment, final-result verification from the day listing.
- **Reaches the model:** team history feeds pre-match form; match-detail
  statistics are persisted as enrichment, not injected into the prediction
  model (no silent model change).
- **Evidence (2026-11-27T20:07Z):** `/matches?date=20261128` → HTTP 200,
  266,069 bytes, 149 leagues; Premier League (`id=47`, `ccode=ENG`) had 3
  matches with UTC kickoff and start/finish flags. `/matchDetails?matchId=4506478`
  parsed to `home_xg=1.02`, `away_xg=0.63`, `home_possession=51.0`,
  `away_possession=49.0`, shots 13/6, shots on target 3/5, corners 1/5,
  cards 0/1.
- **Gaps:** live polling not implemented (`live` `UNVERIFIED`); xG absent for
  some competitions/seasons is recorded per field, never inferred; undocumented
  endpoint terms/stability remain a review item.

## API-Football (`api_football`)

- **Base:** `https://v3.football.api-sports.io`. **Secret:** `API_FOOTBALL_KEY`.
- **Endpoints wired:** `/fixtures`, `/fixtures?live=all`, `/fixtures/statistics`,
  `/fixtures/events`, `/fixtures/lineups`, `/fixtures/headtohead`,
  `/teams/statistics`, `/standings` (`api_football.py`).
- **Role:** primary credentialed football source: discovery, pre-match,
  live, final result, lineups/events/statistics where the plan covers them.
- **Status in this environment:** `CREDENTIAL_BLOCKED`. The adapter is
  preserved; `DataResolver` reported `API_FOOTBALL_KEY is not configured.`
  No API-Football capability is claimed as verified here.
- **Quota protection:** existing `storage.reserve_api_request` budget and
  `config.API_FOOTBALL_DAILY_CREDIT_LIMIT` remain in force.
- **Gap:** actual competition/field coverage per season cannot be established
  without a key. A successful fixture response would not by itself prove
  lineups/statistics availability.

## ESPN NBA (`espn_nba`)

- **Base:** `https://site.api.espn.com/apis/site/v2/sports/basketball/nba`.
- **Endpoints:** `/scoreboard?dates=YYYYMMDD`, `/summary?event={id}`,
  `/teams/{team_id}/schedule?season={season}`.
- **Auth:** none. **Coverage:** configured league id `12` only.
- **Native ids:** ESPN game ids (`401909317`) and team ids; never translated
  into API-Basketball ids.
- **Adapter:** `live_providers.espn_nba_games`, `espn_nba_game`,
  `espn_nba_team_scoring`; `basketball_enrichment.fetch_summary/parse_summary`.
- **Roles:** discovery, identity, pre-match season scoring averages, model
  inputs, prediction snapshots, final-result verification, grading, historical
  persistence, box-score enrichment.
- **Evidence (2026-11-27):** scoreboard `20261127` → HTTP 200, 218,908 bytes,
  11 events; each carries `status.clock`, `displayClock`, `period`. Summary
  for `401909317` → parsed teams (Miami 104, field goals 39/86, rebounds 44,
  assists 24), four period scores, 27 player rows. Lifecycle end-to-end:
  game `401909317` (111–104 FT) discovered, prediction cycle opened, final
  result verified from the ESPN payload, graded, and advanced to
  `HISTORICAL_RECORD_UPDATED`; `lifecycle_events` recorded
  `result_retrieved`, `result_verified`, `prediction_graded`.
- **Gaps:** in-play polling adapter is not implemented (`live` `UNVERIFIED`),
  even though the scoreboard exposes clock/period fields. Only competition 12
  is claimed.

## API-Basketball (`api_basketball`)

- **Base:** `https://v1.basketball.api-sports.io`. **Secret:** `API_BASKETBALL_KEY`.
- **Endpoints wired:** `/games`, `/games?live=all`, `/games?id=`,
  `/statistics`, `/games/statistics/teams`, `/games/statistics/players`
  (`basketball_api.py`).
- **Role:** credentialed fallback and live source.
- **Status:** `CREDENTIAL_BLOCKED` in this environment
  (`API_BASKETBALL_KEY (or API_FOOTBALL_KEY) is not configured.`). Adapter
  preserved; no capability claimed.
- **Identity rule:** provider-native ids are never mixed with ESPN ids; the
  lifecycle row is keyed by `<provider>:<native_id>`.

## Football-Data.co.uk CSV (`football_data_csv`)

- **Base:** `https://www.football-data.co.uk/mmz4281/{season_code}/{division_code}.csv`
  (`data_resolver.FOOTBALL_DATA_CSV_URL_PATTERN`).
- **Auth:** none. This is a static CSV route, not the football-data.org API and
  **not** the source that requires `FOOTBALL_DATA_API_KEY`.
- **Coverage:** configured European divisions with a CSV mapping; results and
  shots/corners/cards statistics.
- **Roles:** historical fixtures and enrichment only.
- **Evidence (2026-11-27T20:44Z):** `_fetch_direct_csv_league_fixtures(39, 2024)`
  downloaded `mmz4281/2425/E0.csv` and normalised **380** fixtures with real
  canonical identities (e.g. Man United 1–0 Fulham, 2024-08-16) and no API key.
  Header/total-goal statistics are available; full per-shot statistics are not
  present in the CSV, which is why statistics remains `PARTIALLY_VERIFIED`.

## OpenFootball JSON/TXT (`openfootball`)

- **Base:** `https://raw.githubusercontent.com/openfootball/football.json/master/{season}/{competition}.json`
  plus the football.TXT and internationals repositories.
- **Auth:** none. **Role:** historical fixtures/results only.
- **Evidence (2026-11-27T20:2xZ):** restored
  `historical_sync.sync_historical_fixtures(39, 2024)` selected
  `openfootball_json`, reported `SUCCESS`, stored **380** fixtures, status
  `COMPLETE`, `acquisition_state=VALIDATED`, 0 API requests consumed,
  `pages_completed=1/1`. An immediate rerun performed a read-first check and
  stored **0** new fixtures (`final_stored_count=380`) — idempotent.
- **Gaps:** no statistics (fixtures/results only).

## OpenLigaDB (`openligadb`)

- **Base:** `https://api.openligadb.de/getmatchdata/{leagueShortcut}/{season}`.
- **Coverage:** Bundesliga (id 78) only in this configuration. Historical
  fixtures only.
- **Evidence (2026-11-27T20:45Z):** `openligadb_provider.get_games(78, 2024)`
  returned `SOURCE_AVAILABLE` with **306** matches, real goals, and UTC kickoff
  conversion (`matchDateTimeUTC` `2024-08-23T18:30:00Z` from local
  `20:30` `W. Europe Standard Time`). Each record carries
  `kickoff_time_known`, `kickoff_time_precision=second_utc` and
  `timezone_known=true`. The raw payload also exposes `timeZoneID` and the
  local `matchDateTime`, both preserved in provenance.

## football-data.org v4 (`football_data_org`)

- **Base:** `https://api.football-data.org/v4`. **Secret:**
  `FOOTBALL_DATA_API_KEY` (`football_data_api.py`).
- **Role:** secondary football discovery/standings/team statistics when a key
  is present.
- **Status:** `CREDENTIAL_BLOCKED`. This is a **separate product** from the
  Football-Data.co.uk CSV feed; do not conflate the two.

## UEFA match API (`uefa_match_api`)

- **Base:** `https://match.uefa.com/v5/matches?competitionId={id}&seasonYear={year}&limit=500&offset=0`.
- **Adapter:** `uefa_qualifying_backfill.py` (deterministic ids derived from the
  UEFA match id) and `uefa_kickoff_repair.py`.
- **Coverage evidence:** Champions League, Europa League and European
  Championship only. Nations League / Conference League are not assumed.
- **Evidence (2026-11-27T20:07Z):** probe of
  `/v5/matches?competitionId=1&seasonYear=2025&limit=5&offset=0` → HTTP 200,
  `application/json`, 198,133 bytes. Response fields are type-guarded because
  values may be strings.
- **Gaps:** licensing/data-use review still pending; not a live source.

## FIFA calendar API (`fifa_calendar`)

- **Base:** `https://api.fifa.com/api/v3/calendar/matches?idCompetition=17&idSeason=255711&count=500&language=en`.
- **Status:** `UNAVAILABLE` **as a repository integration** — the endpoint is
  reachable (probe 2026-11-27T20:45Z: HTTP 200, `application/json`, 24,199
  bytes, `Results` array) but there is no FIFA calendar adapter/normaliser in
  this codebase. FIFA appears only in competition naming. Licensing review is
  also pending. No fixture ingestion from this source is claimed.

## FBref via SoccerData (`fbref_soccerdata`)

- **Status:** `ACCESS_DENIED` for automated access (previous HTTP 403). The
  repository's `.venv` does contain `soccerdata` 2026.43.0, but the documented
  FBref/Sofascore refusals stand and access controls are never bypassed.
  Historical only. The ad-hoc `/tmp` audit virtualenv used for the keyless
  checks does not include it.

## Sofascore

- **Status:** `UNAVAILABLE` — no adapter exists in this repository and the
  previous automated access returned HTTP 403. Not routed.

## StatsBomb Open Data

- **Status:** `LICENSE_BLOCKED` — non-commercial licensing, attribution,
  identity and coverage constraints are unresolved. No adapter/normalisation
  is claimed.

## Understat

- **Status:** `UNVERIFIED` — SoccerData may theoretically expose it, but this
  repository has no Understat route or demonstrated permitted payload. Not
  routed.

## Basketball-Reference

- **Status:** `LICENSE_BLOCKED` — automated bulk acquisition permission has not
  been cleared. Not used.

## TheSportsDB

- **Status:** `UNAVAILABLE` — no adapter or production key in this repository;
  free-tier limits and permitted use are unresolved.

---

## Fixture-level multi-provider discovery (implemented this pass)

`fixture_merger.py` replaces the previous all-or-nothing fallback:

1. Every eligible provider for the day/competition is queried independently;
   one provider returning fixtures never suppresses another.
2. Attempts are recorded with outcome, count, latency, failure category and an
   explicit `empty_is_not_proof` flag.
3. Fixtures are matched by competition + season + canonical (or normalized)
   home/away identity + kickoff tolerance (240 min). A home/away swap is never
   merged; it becomes `identity_unresolved` with reason
   `home_away_orientation_conflict`.
4. Merged fields keep field-level provenance (`provider`, native fixture id,
   retrieval time, parser version). Field preference is per field, not per
   provider (e.g. statistics/xG prefer FotMob; status/score prefer
   API-Football, then ESPN).
5. Disagreements are preserved as conflicts and the canonical field is marked
   unresolved; scores are never averaged or silently overwritten.
6. All provider-native ids survive reconciliation in `provider_fixture_ids`.

**Real merged-discovery evidence (2026-11-27T20:1xZ, isolated DB):**
`fixture_merger.discover("football", "2026-11-28", [39])` queried
`api_football` (empty; credential unavailable), `espn_soccer` (27 fixtures) and
`fotmob` (27 fixtures); produced **17 merged fixtures** with both provider ids
retained and **0** false merges; 8 records stayed unresolved because the local
database had no canonical team mapping for those clubs. The lifecycle run then
tracked 18 football fixtures, saved 18 football pre-match snapshots, and the
basketball run tracked 11 ESPN NBA games. Every attempt/merge/unresolved outcome
was written to `lifecycle_events`.

## Provider health and reliability

A provider whose `auth_env` is absent is excluded from routing by
`provider_registry.supports()`; multi-provider discovery records it as
`credential_missing` rather than treating the resulting empty result as an
honest "no fixtures" answer. Permanent auth/access failures are not retried on
every cycle, but a bounded recovery probe is allowed after 10 minutes.

`provider_health.py` persists per-provider health and isolates failing
providers:

- explicit classification of 401/403/404/429/5xx, timeout, connection failure,
  invalid JSON, unexpected schema, missing field, empty response, parser
  failure, identity mismatch and configuration error;
- 401/403/expired credentials are never retried as transient errors, and empty
  or denied responses are never treated as proof of an empty dataset;
- bounded exponential backoff with jitter and bounded `Retry-After`;
- three consecutive failures open a circuit for 300 s; a success resets it;
- health is independent from registry capability verification.

## Observability

`lifecycle_storage.record_event/get_events/event_metrics` persist structured
events (discovery, provider attempts/failures, merges, conflicts, identity
resolution, prediction/skip, snapshot, live observation, result retrieval/
verification/conflict, grading, learning evaluation/accept/reject) keyed by a
stable `event_key` and correlated by `lifecycle_id`. Observed in the audited run:
`fixture_identity_resolved=29`, `provider_observation_merged=17`,
`provider_queried=4`, `provider_failed=1`, `identity_unresolved=19`,
`prediction_generated=29`, `snapshot_persisted=29`, `result_retrieved`,
`result_verified`, `prediction_graded` for the ESPN NBA end-to-end case.

## Missing-data classification

`enrichment_status.py` distinguishes `provider_does_not_supply_field`,
`provider_predates_field_coverage`, `no_matching_fixture`,
`ambiguous_identity`, `missing_request_evidence`, `provider_request_failed`,
`statistics_block_unavailable`, `parser_failed`, `not_acquired`,
`intentionally_excluded_by_policy` and `conflicting_observations`. The parsed
ESPN all-zero statistics case is classified as a provider coverage limitation,
not a parser failure.

## Corrections and controlled replay

`corrections.py` appends corrected provider evidence and can replay grading
without touching issued predictions:

- `record_correction(lifecycle_id, field=..., corrected_value=..., reason=...)`
  stores a new `correction` observation that links to the observation it
  corrects, records a `result_conflict_detected` event naming both the original
  and the corrected evidence, and returns an impact report
  (snapshots, grading requirement, historical records untouched).
- `replay_grading(lifecycle_id, reason=...)` re-verifies the corrected payload
  through the same football/basketball verification gates and writes a separate
  grading row keyed `...:replay:<correction observation key>`. The original
  grading row and every PRE_MATCH/LIVE snapshot remain byte-identical.

Verified on the isolated end-to-end basketball lifecycle: after a correction and
replay the snapshot row was unchanged, the original grading row was preserved,
and exactly one replay grading row was added.

## Schema and migration notes

- New additive tables: `lifecycle_events` and `provider_health`, added to both
  `migrations/phase2_lifecycle_sqlite.sql` and
  `migrations/phase2_lifecycle_postgres.sql`.
- Local SQLite creates them lazily in `ensure_schema()`.
- PostgreSQL DDL is never executed by the application; the reviewed migration
  must be applied by the operator before a deployment uses these tables.
- No existing table/column was dropped, renamed or recreated.

## Blockers (kept separate)

1. `API_FOOTBALL_KEY` absent → all API-Football capabilities blocked.
2. `API_BASKETBALL_KEY` absent → all API-Basketball capabilities blocked.
3. `FOOTBALL_DATA_API_KEY` absent → football-data.org v4 blocked (the
   Football-Data.co.uk CSV route does **not** need it).
4. FBref/SoccerData automated access refused and package absent.
5. Sofascore: no adapter, previous 403.
6. StatsBomb: licensing/attribution/identity unresolved.
7. Understat: no demonstrated permitted payload.
8. Basketball-Reference: automated acquisition permission not cleared.
9. TheSportsDB: no adapter/production key.
10. ESPN undocumented endpoint stability/terms review pending.
11. FotMob undocumented endpoint stability/terms review pending.
12. UEFA data-use/licensing review pending.
13. FIFA data-use/licensing review pending.
14. Live in-play verification for ESPN Soccer, FotMob and ESPN NBA was not
    demonstrated (no repeated polling adapter for live in-play payloads), so
    those capabilities remain `UNVERIFIED` despite the scoreboard exposing
    clock/period fields.
