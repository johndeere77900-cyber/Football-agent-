# Phase 1 audit — historical acquisition repair and sports knowledge foundation

Branch: `fix/phase1-reliability-and-sports-knowledge`
Base: `origin/main` at `1644762e47c83a719b375d701adba65488b78321`
("Scope completion evidence to the current acquisition and stamp historical provenance")

The branch was created from a freshly fetched `origin/main` (the working tree was clean
and already at that commit). No force-push, reset or history rewrite is used, and `main`
itself is never written to.

---

## 1. Call path traced (latest `main`, read-only)

Football historical queue:

```
main.py --historical-queue
  -> historical_sync.run_historical_queue()                      # season/league selection
     -> run_safe_preflight()                                     # DB + manifest status
     -> check_acquisition_coverage()                             # manifest + cache estimate
     -> historical_sync.sync_historical_fixtures()
        -> data_resolver.DataResolver.get_league_fixtures_page()
           -> _fetch_direct_csv_league_fixtures()                # 1. Football-Data.co.uk CSV
           -> soccerdata_provider.get_fbref_games()              # 2. SoccerData FBref
           -> soccerdata_provider.get_match_history_games()      # 3. SoccerData MatchHistory
           -> soccerdata_provider.get_sofascore_historical_games()  # 4. SoccerData Sofascore
           -> reconcile_fixture_records()                        # field-level reconciliation
           -> _validate_historical_structural_coverage()         # independent coverage proof
        -> storage.save_historical_fixtures()                    # (source, fixture_id) upsert
        -> storage.mark_historical_dataset_complete/incomplete() # manifest
        -> get_competition_lifecycle()                           # lifecycle vs acquisition state
  -> historical_sync.verify_persisted_historical_datasets()      # independent readback audit
```

Basketball historical queue:

```
historical_sync.run_historical_queue()
  -> historical_sync.sync_historical_basketball_games()
     -> espn_basketball_provider.fetch_espn_nba_season_games()   # ESPN monthly scoreboards
        -> _classify_espn_event()                                # competition + season type
     -> storage.save_historical_basketball_games(source="espn_url")
     -> basketball_api.get_league_games_page()                   # API-Basketball fallback
     -> storage.save_historical_basketball_games(source="api_basketball")
     -> _validate_historical_basketball_dataset()                 # fail-closed coverage
     -> storage.mark_historical_dataset_complete/incomplete()
```

Identity: `team_identity.resolve_canonical_team_id()` (provider mapping -> exact normalized
name + competition context -> configured aliases -> national-team suffix -> safe stripped
name within the same competition -> `None`). A bare provider ID in an authoritative manifest
is resolved through `data_resolver._resolve_expected_provider_team_id()`, which fails closed
when more than one canonical identity matches.

Validation and lifecycle are deliberately independent: `get_competition_lifecycle()` answers
"does this competition season exist and is it finished" from declared competition metadata,
while acquisition state (`PENDING/RUNNING/INCOMPLETE/VALIDATED/FAILED`) answers "how far did
persistence get". A dataset is only `COMPLETE` when both agree.

---

## 2. Confirmed defects on the latest `main`

Each line was read in the current source before the change. "Fixed" means fixed on this
branch; everything else is reported for a later, explicitly scoped change.

| # | File / function | Confirmed faulty behaviour | Impact | Scope | Correction |
|---|---|---|---|---|---|
| A | `storage.py::save_historical_basketball_games` | PostgreSQL `ON CONFLICT (source, game_id) DO NOTHING`, SQLite `INSERT OR IGNORE`: an existing game is discarded even when the re-acquisition carries verified phase metadata | Basketball metadata could never be repaired; unclassified persisted games stayed unclassified forever | Basketball | **Fixed.** Idempotent enrichment upsert: existing rows are merged (missing/unclassified fields filled, valid date/scores/identity preserved, conflicts recorded, `enrichment_history` appended); identical PostgreSQL and SQLite behaviour; new `enriched`/`already_existing`/`metadata_conflicts` counters |
| B | `historical_sync.py::_validate_historical_basketball_dataset` | Regular-season coverage excludes games without a verified phase and requires a positive independently established total | Fail-closed behaviour is correct and intentional | Basketball | **Kept unchanged.** Only the persistence path (A) was repaired; no validator weakened. Covered by a regression test asserting an unclassified dataset still fails |
| C | `historical_sync.py::run_historical_queue` | Blanket `TARGET_SEASONS x ALLOWED_LEAGUE_IDS` product: World Cup, Euro and Nations League are requested for seasons that never existed | Fake coverage gaps, meaningless failures and wasted provider attempts | Football (and basketball if another competition is added) | **Fixed.** Per-competition edition selection from the knowledge catalogue; editions a competition never staged are reported in `summary["edition_selection"]["skipped"]` as `unsupported_season_edition` and are never silently dropped |
| D | `config.py::COMPETITION_SEASON_WINDOWS` | Generic windows applied to editions that do not follow them (2022 World Cup was in November; Euro 2020 was played in 2021; the Nations League is biennial, not annual) | Lifecycle and schedule-window validation judged real seasons against the wrong dates | Football | **Fixed.** Documented per-edition overrides added (WC 2018/2022/2026, Euro 2020/2024, Nations League 2026-27) and the generic Nations League window removed; editions without documented dates stay `UNKNOWN` |
| E | `data_resolver.py::DataResolver.get_league_fixtures_page` | Contributor lists were assembled per return branch by hand; the MatchHistory, Sofascore and final branches omitted `soccerdata_fbref` even when FBref data had been merged | Wrong provenance: FBref contributed persisted fields but was not attributed | Football | **Fixed.** `_providers_contributed_from_records()` derives contributors from `reconciliation_metadata.providers_used`, `field_provenance` and `provider_provenance`, with the attempted-provider list only as a legacy fallback |
| F | `config.py::NBA_EXPECTED_MIN_REGULAR_SEASON_GAMES` | No 2026 entry | Correct fail-closed behaviour for an ongoing season | Basketball | **Kept absent, documented.** The 2026-27 season is in progress; asserting 1230 would be an unverified copy of a previous season and would let an ongoing season be judged complete |
| G | `data_resolver.py` coverage rules for cups/internationals | `DOUBLE_ROUND_ROBIN_LEAGUE_IDS = {39, 140, 135, 78, 61, 88, 94}`; cup/international competitions (2, 3, 5, 1, 4) have no pair-schedule expectation. Only Sofascore is configured for UCL/UEL/Nations League; Football-Data CSV has no division for them and the FBref/MatchHistory maps omit them | Those competitions cannot be proven complete from configuration alone today | Football | **Audited, not weakened.** The knowledge catalogue now records the exact per-competition provider coverage and the gap; lifecycle for them fails closed. Closing the gap needs a verified additional source, which is a separate decision |
| H | `data_resolver.py` expected-team manifests | Manifests without usable `expected_team_ids` cause `authoritative_team_universe_unavailable` (fail closed); provider-scoped IDs are resolved across namespaces and ambiguity fails closed | Correct, but production manifest coverage is unknown from the repository | Football/basketball | **No code change.** Production manifest state requires a Neon readback (section 8) |
| I | `data_resolver.py::get_league_fixtures_page` outcome reporting | Provider outcomes distinguish `SUCCESS / FAILED / MALFORMED / VALID_EMPTY / QUOTA_EXHAUSTED / INSUFFICIENT` and a failed provider never blocks the next fallback, but `secondary_access_denied` is hard-coded `False` and no `ACCESS_DENIED` outcome exists | A 403 is reported as `FAILED` (never as an empty page), but callers cannot tell access denial from a timeout | Football | **Reported, not changed.** Adding an access-denial classification needs provider-level evidence (for example soccerdata surfacing an HTTP status) and is out of this narrow scope |
| J | `PHASE1_INVENTORY.json` | 237 KB hand-maintained file with per-target `BLOCKED_NO_DATABASE_ACCESS` claims and no generator | Stale, unverifiable, not reproducible | Both | **Fixed.** `build_phase1_inventory.py` regenerates the inventory from configuration + knowledge; without a database every persisted count is `UNKNOWN`, and `--local-database` is needed for real evidence |
| K | `storage.py::save_historical_fixtures` season allow-list | Storage accepted only `config.TARGET_SEASONS` (2019-2026) | The catalogued World Cup 2018 edition could not be persisted even if acquired | Football | **Fixed in Part 4 (finding X).** Storage and acquisition now share `config.documented_football_tournament_editions()`: documented editions of the same competition are admitted, every other season is still rejected |
| L | `espn_basketball_provider` ongoing-season coverage | A season's monthly coverage units span October .. June of the following year; an ONGOING season's future months cannot succeed | An ongoing season can never be marked `COMPLETE` | Basketball | **Correct fail-closed behaviour, now made explicit** in the inventory (`ONGOING`) and the knowledge catalogue (2026 expectation `null`) |

Additional confirmed-but-benign behaviour that was deliberately not changed: provider order
(Football-Data CSV -> FBref -> MatchHistory -> Sofascore), API-Football/football-data.org
exclusion from historical fixtures, basketball ESPN-first then API-Basketball, and every
fail-closed check in both validators.

---

## 3. Competition and season inventory

`PHASE1_INVENTORY.json` is generated by `python build_phase1_inventory.py` from the current
configuration and `sports_knowledge/`. Current generated totals (Part 4): **106
competition-season targets** (98 football, 8 basketball), 16 `NOT_APPLICABLE` editions,
75 `COMPLETED`, 11 `ONGOING`, 4 `UNKNOWN`, and **zero knowledge-validation problems**.
Every target also carries `provider_sources` (endpoint/URL and competition code per approved
provider) and `evidence_gaps` (`edition_window`, `expected_fixture_total`,
`authoritative_participant_universe`), so an unverifiable dataset states exactly which
evidence is missing instead of merely showing an `UNKNOWN` count.

Configured competitions:

| Sport | ID | Competition | Format | Editions targeted | Lifecycle evidence |
|---|---|---|---|---|---|
| Football | 39, 140, 135, 78, 61, 88, 94 | Premier League, La Liga, Serie A, Bundesliga, Ligue 1, Eredivisie, Primeira Liga | double round robin | 2019-2026 annual | declared domestic windows in `config.COMPETITION_SEASON_WINDOWS` |
| Football | 2, 3 | UEFA Champions League, UEFA Europa League | cup | 2019-2026 annual | July -> June declared window |
| Football | 5 | UEFA Nations League | international | 2018, 2020, 2022, 2024, 2026 (biennial) | 2026-27 documented (UEFA); other editions `UNKNOWN` |
| Football | 1 | FIFA World Cup | tournament | 2018, 2022, 2026 (quadrennial) | FIFA finals windows (2022: 20 Nov - 18 Dec) |
| Football | 4 | UEFA European Championship | tournament | 2020 (played 2021), 2024 | UEFA: EURO 2020 played 11 Jun - 11 Jul 2021 |
| Basketball | 12 | NBA | league + playoffs | 2019-2026 annual | declared Oct -> Jun window, 2019/2020 COVID overrides |

Every target records sport, competition identity and aliases, governing body, season/edition
key, window or `UNKNOWN`, lifecycle state and evidence, format, expected coverage basis,
approved provider order, per-provider support, and `UNKNOWN` for every persisted count that
was not actually read from a database. No production count is written from configuration.

---

## 4. Provider and URL research

Reachability was probed from this environment on 2026-11-01 with `HEAD` requests only (no
scraping, no access-control circumvention):

| Source | URL | Probe | Role |
|---|---|---|---|
| Football-Data.co.uk | https://www.football-data.co.uk/ | `302` (redirect, reachable) | Approved provider 1 (domestic CSV) |
| Football-Data notes | https://www.football-data.co.uk/notes.txt | `302` | URL/season-code documentation |
| FBref | https://fbref.com/ | `403` on HEAD | Approved provider 2 via SoccerData; the blocked HEAD is recorded as an access outcome, never as an empty dataset |
| Sofascore | https://www.sofascore.com/ | `403` on HEAD | Approved provider 4; same rule - access denial is `FAILED`, not `VALID_EMPTY` |
| SoccerData docs | https://soccerdata.readthedocs.io/en/stable/datasources/index.html | `200` | Source/provider documentation |
| ESPN scoreboard API | https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard | `200` | Basketball primary route (monthly units) |
| NBA schedule | https://www.nba.com/schedule | `200` | Season lifecycle evidence |
| FIFA 2022 World Cup | https://www.fifa.com/en/tournaments/mens/worldcup/qatar2022 | not re-fetched (search-verified: 20 Nov - 18 Dec 2022) | Edition window evidence |
| UEFA Nations League 2026/27 | https://www.uefa.com/uefanationsleague/news/0298-1d6ef1acfaef-b54fcf1da859-1000--2026-27-uefa-nations-league-all-you-need-to-know/ | search-verified | Edition window evidence |
| nba_api | https://github.com/swar/nba_api | reference | MIT; NBA.com terms apply to its data |
| SportsDataverse Python | https://github.com/sportsdataverse/sportsdataverse-py | reference | MIT (verify before use) |

No new endpoint was added to production. Nothing was routed through API-Football or
football-data.org for historical fixtures, and provider order was not changed.

---

## 5. Open-source candidate evaluation

| Project | License / activity | Coverage | Decision |
|---|---|---|---|
| SoccerData (already a dependency, `soccerdata>=1.9.1`) | MIT; actively used by the existing FBref/MatchHistory/Sofascore adapters | FBref, MatchHistory, Sofascore, Football-Data | **Keep as-is.** Do not duplicate its parsing; the repository keeps its own normalization, reconciliation and validation above it |
| SportsDataverse Python | MIT; 0.2 line is beta | broad multi-sport, NBA included | **Reference only.** Not added as a runtime dependency: beta API, heavy dependency surface, and no verified Workers/CI compatibility |
| nba_api | MIT; actively maintained | NBA.com stats endpoints | **Reference only.** ESPN-first acquisition and the API-Basketball fallback are preserved; NBA.com terms and endpoint stability are unresolved |
| Other repositories | - | - | **Not used.** No actively maintained, licensed, credibly sourced project was found that closes the UCL/UEL/Nations League gap without an access-control or stability risk |

---

## 6. Sports knowledge foundation (implemented)

`sports_knowledge/` (versioned JSON) + `sports_knowledge.py` (loader, validator, edition
materialisation):

- competition identity, aliases, governing body, country/region, format;
- season/edition calendars: annual ranges with declared windows, and explicit editions for
  quadrennial/biennial competitions; undocumented editions stay `UNKNOWN`;
- expected coverage shape (`double_round_robin`, `cup`, `international`, `tournament`,
  `regular_season_games`) and, only where documented, an expected total;
- approved provider order and per-competition provider coverage;
- phase classification vocabulary for basketball (regular season, preseason, postseason,
  play-in, All-Star, unclassified);
- evidence per fact with source URL, retrieval timestamp and confidence, and separately
  recorded `hypotheses` that never drive acquisition or completion.

`sports_knowledge.validate()` cross-checks the catalogue against `config` (allowed league IDs,
alias map, declared windows, NBA expectations) and returns zero problems on this branch.
Other sports are added by a new catalogue file registered in `SPORT_FILES`; no LLM memory or
vector database is involved.

---

## 7. Implementation sequence used

1. Fetch `origin/main`; branch from the fetched commit.
2. Repair basketball persistence (A) with a pure merge function plus one upsert path shared by
   PostgreSQL and SQLite; keep both validators untouched.
3. Report enrichment progress through `sync_historical_basketball_games` (`enriched`).
4. Derive contributor attribution from records (E).
5. Add documented per-edition lifecycle metadata (D) and the competition edition catalogue
   (C, section 6).
6. Restrict the queue to real editions (C) while keeping preflight, quota independence and
   report structure backward compatible.
7. Add the reproducible inventory generator and regenerate `PHASE1_INVENTORY.json` (J).
8. Add regression tests for every change and re-run affected existing suites.

## 8. Files changed

- `storage.py` - basketball enrichment upsert + merge helper (`merge_historical_basketball_game_metadata`)
- `historical_sync.py` - competition edition selection, `enriched` reporting
- `data_resolver.py` - record-derived provider attribution
- `config.py` - documented edition window overrides, Nations League correction, NBA 2026 note
- `sports_knowledge.py`, `sports_knowledge/football_competitions.json`,
  `sports_knowledge/basketball_competitions.json` - new knowledge layer
- `build_phase1_inventory.py`, `PHASE1_INVENTORY.json` - reproducible inventory
- `tests/test_phase1_reliability_knowledge.py` - regression coverage
- `PHASE1_AUDIT.md` - this document

## 9. Working areas explicitly preserved

Football live predictions and current fixtures; API-Football live fixtures, statistics,
standings, form and enrichment; basketball live/current acquisition and prediction models;
ESPN-first NBA historical acquisition and the API-Basketball fallback; the approved football
historical provider order; backtesting, feature generation, grading, training, Telegram/bot
functions, storage APIs and existing database rows/provenance; public function signatures used
by other modules; Python as the implementation language.

## 10. Issues that require Neon/ChatGPT investigation

These cannot be answered from the repository and were not guessed:

1. For NBA seasons, how many persisted `historical_basketball_games` rows per season currently
   have `raw_json->'provider_provenance'->>'season_type'` null or `unclassified`, and how many
   are missing `status_short`, team names or points that a re-acquisition could now fill?
   (This determines whether a targeted re-acquisition after this merge is worthwhile.)
2. Do the `historical_datasets` manifests for league 12 still record `provider_coverage` with a
   `VALIDATED` `espn_url` entry and `competition_verified = true`, or were those rows written
   before that evidence existed?
3. How many football manifests have an empty `expected_team_ids_json`, by competition and
   season? Those datasets cannot pass structural coverage regardless of provider health.
4. For competitions 2, 3 and 5, what is the current manifest state and the last recorded
   `error_reason` (to confirm the Sofascore access gap is still the only blocker)?
5. Are there persisted basketball rows whose `season` differs from the season encoded in their
   `raw_json` league object, which the enrichment path would leave untouched by design?
6. Are there rows in `historical_basketball_games` whose `source` value is not one of
   `espn_url` / `api_basketball` (they would never be attributed to a validated provider)?

---

## 11. Part 2 — additional confirmed blockers fixed

These were confirmed in the live source during Part 2 and are now fixed on the same branch.

| # | File / function | Confirmed faulty behaviour | Correction |
|---|---|---|---|
| M | `storage.py::save_historical_fixtures` | PostgreSQL `ON CONFLICT (source, fixture_id) DO NOTHING` and SQLite `INSERT OR IGNORE`: an already persisted fixture was discarded, so a later richer acquisition (merged CSV+FBref statistics, `field_provenance`, `reconciliation_metadata`, resolved canonical team ids) could never be applied to it | New `merge_historical_fixture_metadata()` plus an enrichment pass: existing rows are enriched additively (missing statistics/field provenance/reconciliation evidence filled, missing canonical ids filled), while kickoff, completed status, teams, goals and identity columns are never rewritten. Conflicts are recorded, applied enrichments append to `enrichment_history`, and `enriched`/`already_existing`/`metadata_conflicts` are returned. PostgreSQL and SQLite share the same merge function and UPDATE statement |
| N | `storage.py::_fetch_existing_basketball_raw_json` | The Part 1 enrichment upsert read one row per game (an N+1 pattern across a season) | One bounded pre-read per page (chunked, 400 ids) feeds the same merge; round trips are now a small fixed number per page regardless of page size. Idempotency and composite identity are unchanged |
| O | `data_resolver.py::_classify_provider_attempt_outcome` | Only `FAILED / QUOTA_EXHAUSTED / MALFORMED / INSUFFICIENT / SUCCESS / VALID_EMPTY / NO_DATA_FROM_PROVIDER` existed, so an HTTP 403, a 429, a timeout, an unsupported endpoint or a parser failure all collapsed into one `FAILED` bucket; `secondary_access_denied` was hard-coded `False` | Outcomes now honour provider-native evidence (`error_category`, `http_status`) and add `ACCESS_DENIED`, `UNSUPPORTED`, `RATE_LIMITED`, `TIMEOUT`, `NETWORK_ERROR`, `PARSER_ERROR`. A failed provider still never blocks the next approved fallback, none of these is ever `VALID_EMPTY`, and `secondary_access_denied` is derived from the observed outcomes. The football report lists `access_denied_providers` |
| P | `historical_sync.py::sync_historical_fixtures` | Manifest `providers_contributed` and the report came only from the current attempt's provider payload, so persisted rows acquired by an earlier provider were attributed incorrectly | Contributors and `persisted_records_by_provider` are now derived from the persisted fixture provenance (record-level evidence), with the attempt's own list used only when stored records carry no provenance at all |
| Q | `data_resolver.py::_validate_historical_structural_coverage` + `config.FOOTBALL_TOURNAMENT_COVERAGE_EXPECTATIONS` | Cup / international / tournament formats were correctly never judged with the double-round-robin expectation, but no documented alternative existed, so even fully acquired finals tournaments stayed unverifiable | Format-aware validation now uses a documented match total when one exists (FIFA World Cup 2018 and 2022: 64 matches; UEFA EURO 2020 and 2024: 51 matches), requiring an exact match with resolved canonical identities. Editions without a documented total (UCL, UEL, Nations League, World Cup 2026) remain unverifiable and therefore INCOMPLETE. `sports_knowledge.validate()` cross-checks these totals against the catalogue |

### Deliberately deferred (reported, not guessed)

- **UCL / UEL / Nations League coverage.** No documented match total exists for a league-phase + knockout edition, so they stay unverifiable rather than receiving an invented count. Closing the gap needs a verified, licensed additional source and an edition-specific evidence set.
- **World Cup 2018 persistence boundary — resolved in Part 4.** Storage and acquisition now both admit the documented tournament editions of the requested competition (2018 FIFA World Cup), while every undocumented season stays rejected. See finding X in section 13.
- **NBA 2026 expectation.** Still absent by design: the 2026-27 season is in progress and its final regular-season total is not established.
- **Team identity architecture.** `team_identity.resolve_canonical_team_id()` already fails closed (no name-derived canonical ids unless an explicit bootstrap path registers them, ambiguity and provider/name conflicts return `None`, provider ids are resolved across namespaces and ambiguity fails closed). No verified defect was found, so it was not rewritten.

### Part 2 verification actually performed

- `python -m py_compile` on `storage.py`, `data_resolver.py`, `historical_sync.py`, `config.py`, `sports_knowledge.py`, `build_phase1_inventory.py`.
- `sports_knowledge.validate()`: no problems.
- Local SQLite smoke run: first save inserts; a richer re-save reports `enriched: 1` and merges statistics while preserving goals and kickoff; the third identical save reports `already_existing: 1` (idempotent); a regressive re-save with a different score and nulled statistics keeps the stored goals and statistics and reports `goals.home`/`goals.away` conflicts.
- `_football_tournament_coverage_expectation(1, 2022) == 64`; `_football_tournament_coverage_expectation(2, 2022) is None` (UCL stays unverifiable).
- Existing checks run narrowly for diagnosis: `tests/test_storage.py`, `tests/test_historical_queue.py`, `tests/test_direct_csv_and_resiliency.py`, `tests/test_zero_network_and_provider_order.py`, `tests/test_multi_source_football.py`. The full suite was not run and is left to the user's review.

---

## 12. Full sweep (Part 3) — cross-provider identity, conflict auditing, tournament participants, metrics

### Blockers fixed in this sweep

| # | Blocker | File / function | Evidence of the defect | Correction |
|---|---|---|---|---|
| R | **Football enrichment was keyed by `(source, fixture_id)`** | `storage.py::save_historical_fixtures` | The pre-read and the enrichment `UPDATE` both filtered on `source = <current writer>`, while provider-native fixture ids differ between the approved sources. A fixture first acquired as CSV could never be repaired or enriched by an FBref/MatchHistory/Sofascore acquisition; the same logical match inserted a second row because `ON CONFLICT (source, fixture_id)` never collides across providers | Canonical logical identity `(canonical_home_id, canonical_away_id, UTC kickoff date)` now matches persisted rows across providers. One bounded read of the competition season builds both the native-id and the identity index; enrichment targets the stored row's own `(source, fixture_id)`. Provider-native ids and attribution are preserved separately in `provider_fixture_ids` and `provider_provenance.providers`, the record owner keeps `provider_provenance.provider`, and an ambiguous identity (several persisted rows for one logical fixture) fails closed as `ambiguous_logical_fixture_identity` instead of deepening the duplication. Matching never uses team names or near dates: ids are canonical and the date must be identical. **Part 4 narrowed this**: canonical identity is only reconciled between approved historical providers (CSV, FBref, MatchHistory, Sofascore), which also makes the two provider-isolation tests pass unchanged (section 13) |
| S | **Conflict-only enrichment left no durable evidence** | `storage.py::merge_historical_fixture_metadata` / `save_historical_fixtures` | `enrichment_history` was appended only `if added`, and the caller skipped the `UPDATE` when no field was added, so a conflicting re-acquisition (score, kickoff, provider id) was silently discarded after being counted | A conflict-only attempt now appends a history entry carrying `{field, kept, discarded}` plus its resolved conflict counts, and the row is persisted once so the evidence is inspectable. An identical repeat is recognised by the de-duplicated last entry and reported as `already_existing` with no further growth, so retries cannot flood the audit trail. Stored values are still never replaced |
| T | **Tournament completion could bypass participant-universe evidence** | `data_resolver.py::_validate_historical_structural_coverage` (non-double-round-robin branch) | `is_verified = ... and (not expected_team_set or expected_team_set.issubset(...))` — an empty participant set made the check a wildcard, so a documented match total plus any teams verified a tournament | A tournament now requires an independently established participant universe: no expected participants returns `tournament_participant_universe_unavailable` with `evidence_gaps: ["authoritative_participant_universe"]`, a total mismatch returns `tournament_coverage_incomplete`, and missing/unexpected participants return `tournament_participant_mismatch`. The acquired fixtures are never used to invent the universe. World Cup and EURO datasets therefore remain INCOMPLETE until an authoritative participant list reaches the manifest |
| U | **Enrichment/conflict metrics were not surfaced** | `historical_sync.py::sync_historical_fixtures` (and the basketball sync) | Only `inserted`, `duplicates_skipped` and `rejected` were aggregated; `already_existing_skipped` was computed as `valid - inserted`, silently folding enrichment into "already existing", and conflicts were dropped | `inserted`, `enriched`, `already_existing`, `conflict_only`, `ambiguous_identity`, `metadata_conflicts`, `metadata_conflict_events` are aggregated per page/provider/season, exposed in the report, in `persistence_breakdown`, and inside the manifest `provider_coverage.persistence`; `already_existing_skipped` is now the exact unchanged-row count. Basketball aggregates the same conflict counters. Completion still requires validated persisted data (`db_coverage.verified` plus an independently completed lifecycle), never attempt counts |

### Additional sweep findings and dispositions

- **World Cup 2018 persistence (gap 1) — fixed.** `save_historical_fixtures` still rejected 2018. The season scope now also admits the editions declared in `config.FOOTBALL_TOURNAMENT_COVERAGE_EXPECTATIONS` for that same competition, so `league 1 / 2018` persists while `league 39 / 2018` and `league 1 / 2006` are still rejected. The allow-list is widened only by documented editions, never generically.
- **UCL / UEL / Nations League (gap 2) — unresolved, needs a verified source.** `config` still configures only the Sofascore route for these competitions and only the World Cup/EURO editions have documented match totals, so their coverage remains unverifiable and INCOMPLETE. No provider, endpoint or URL was invented to close this.
- **NBA 2026-27 (gap 3) — preserved.** No full-season expectation was added; the ongoing season continues to fail closed.
- **Tournament participant evidence (gap 4) — fail-closed, not invented.** No authoritative participant lists are stored in this repository, so the new validation reports the evidence gap rather than synthesising one. Closing it requires an authoritative roster source and a manifest field, which is a separate authorized change.
- **Pre-existing duplicate rows (new dependency).** Duplicates written before identity matching now surface as `ambiguous_logical_fixture_identity` instead of being merged or multiplied. Removing them is a database operation and belongs to ChatGPT/Neon; no code path deletes or rewrites persisted rows.
- **Team identity architecture — unchanged.** `resolve_canonical_team_id()` still fails closed and no verified defect was found; the new matching consumes its canonical ids rather than re-deriving them.
- **Provider order, outcomes and fallbacks — unchanged** in this sweep (verified by the provider-order, zero-network, direct-CSV and coverage suites). No live/current, prediction, backtest, grading or Telegram path was touched.

### Verification actually performed in this sweep

- `python -m py_compile` on `storage.py`, `data_resolver.py`, `historical_sync.py`, `config.py`, `sports_knowledge.py`.
- `sports_knowledge.validate()` -> no problems; `build_phase1_inventory.py` -> 104 targets, 16 NOT_APPLICABLE, 0 knowledge problems, `database_inspected=False`.
- SQLite smoke runs: the same logical fixture acquired as `football_data_csv` (id 99001) and then as `soccerdata_fbref` (id 550001) yields **one** persisted row, statistics merged, `provider_provenance.providers = [football_data_csv, soccerdata_fbref]`, `provider_fixture_ids` holding both native ids, and `inserted: 0, enriched: 1`; an identical repeat reports `enriched: 0`. A conflicting re-save keeps the stored goals and records `fixture.id`, `teams.home.id`, `teams.away.id`, `goals.home`, `goals.away` conflicts in `enrichment_history`; repeating it adds no further history entry.
- Tournament validation with the documented 64-match total but no participant universe -> `verified: False`, `reason: tournament_participant_universe_unavailable`, `evidence_gaps: ["authoritative_participant_universe"]`, `expected_fixture_count: 64`.
- Season scope checks: `league 1 / 2018 -> accepted`, `league 1 / 2022 -> accepted`, `league 39 / 2018 -> rejected`, `league 1 / 2006 -> rejected`.
- Existing tests run narrowly: storage, historical sync, historical queue, direct-CSV resiliency, zero-network provider order, multi-source football and the Phase 1 knowledge suite -> **80 passed**.
- **Full suite: 692 passed, 27 failed** (`pytest -q`). 25 of the 27 are pre-existing: 22 were reproduced on the unmodified `f996675` baseline, and `test_coverage_fallback_scenarios.py` (2) and `test_historical_workflow_verification.py` (1) also fail on that baseline.
- **2 failures were the intended contract change at the time of Part 3**: `tests/test_provider_identity_regression.py::test_2_same_numeric_fixture_id_across_providers_remains_isolated` and `tests/test_provider_neutral_identity.py::test_01_02_03_provider_fixture_id_isolation_and_coexistence`. Both assert that the same fixture acquired from `api_football` and `football_data_org` must be stored as two rows. **Part 4 resolved the conflict without touching either test**: those providers are excluded from cross-provider identity matching, so both tests now pass unchanged and only approved historical providers are reconciled (section 13).

---

## 13. Full sweep (Part 4) — provider-scoped identity, edition scope, inventory evidence

Everything below was confirmed in the live source and verified again after the change.

| # | Finding | File / function | Evidence | Correction |
|---|---|---|---|---|
| V | **Cross-provider identity matching was too broad.** The canonical-identity match introduced for Blocker R applied to every persisted provider, so a retired `api_football` / `football_data_org` row could be merged into an approved historical record (or serve as its match target), rewriting provenance and discarding the provider-native ids those namespaces depend on | `storage.py::save_historical_fixtures` | Both provider-isolation tests still failed on the branch head: they persist the same logical fixture as `api_football` and `football_data_org` and require two rows | `config.FOOTBALL_APPROVED_HISTORICAL_PROVIDERS` (CSV, FBref, MatchHistory, Sofascore) is the only set reconciled across providers. Matching now requires **both** the incoming record's own provider and the stored row's own provider (read from `provider_provenance.provider`, falling back to the stored source column) to be approved. Every other writer falls back to exact `(source, fixture_id)` idempotency. A record's source, owner attribution, native ids and provenance are never changed by a match, and rows with missing kickoff or canonical ids are still rejected before any comparison. Both provider-isolation tests now pass **unchanged** |
| W | **`source` stopped describing the acquisition route.** After record-derived attribution, `res["source"]` was taken from record provenance; two existing coverage-fallback tests assert the route label and failed on this branch (they pass on `main`) | `data_resolver.py::get_league_fixtures_page` | Synthetic records naming a different provider than the mock exposed the conflation | `_attempted_provider_label()` reports the approved route that produced the records (`football_data_csv`, `soccerdata`, `mixed`, or `none` when nothing was produced), while `providers_contributed` remains record-derived. The coarse writer label and the precise contributor list are now both reported and neither overrides the other; the two tests pass unchanged |
| X | **World Cup 2018 could be stored but never acquired.** The storage season scope admitted documented tournament editions, but the acquisition guard still raised `ValueError` outside `TARGET_SEASONS`, so the edition the inventory lists could not be fetched | `historical_sync.py::sync_historical_fixtures` | Storage accepted `league 1 / 2018`; acquisition rejected it | Both paths now share `config.documented_football_tournament_editions(league_id)`. Documented editions of the same competition are admitted; `league 39 / 2018`, `league 1 / 2006` and `league 39 / 2027` are still rejected, and the queue's default season list is unchanged |
| Y | **The inventory hid documented editions and evidence.** Seasons came only from `TARGET_SEASONS`, so the 2018 World Cup and the 2018-19 Nations League were absent; provider endpoints and evidence gaps were not recorded | `build_phase1_inventory.py` | 104 targets, no 2018 World Cup row, no source URLs, no explicit gaps | Documented edition years are unioned into the target list; each target now carries `provider_sources` (name, kind, endpoint/URL, competition code), `evidence_gaps`, and the top-level inventory records the providers excluded from historical fixture acquisition. Regenerated: **106 targets** (98 football, 8 basketball), 16 `NOT_APPLICABLE`, 0 knowledge problems, `database_inspected=false` |
| Z | **Legacy/approved duplicate overlap was invisible.** After scoping identity, a `football_data_csv` record and an `api_football` record describing the same logical fixture are never merged — and nothing reported the overlap, which is exactly the pattern behind the 342 duplicate groups ChatGPT observed | `storage.py`, `historical_sync.py` | No counter existed for cross-contract duplicates | `legacy_identity_overlap` is counted per save (legacy row identity vs. the incoming logical fixture), aggregated per sync, and reported in the report, `persistence_breakdown` and `provider_coverage.persistence`. Nothing is merged or deleted; the count is evidence for database reconciliation |

### Audited and confirmed unchanged (no code change)

- **Team identity / aliases / promotion-relegation.** A sample of 29 historical spellings across
  all seven domestic leagues (`Ipswich Town`, `Luton Town`, `Nott'm Forest`, `Leeds United`,
  `Sunderland`, `FC Volendam`, `Go Ahead Eagles`, `Paris SG`, `Ein Frankfurt`, `Dortmund`,
  `Verona`, `Monza`, `Como`, `Sp Lisbon`, `AVS`, `Real Oviedo`, ...) resolves; competition
  context prevents cross-league reuse (`Nacional` resolves in 94, fails closed in 140); unknown
  clubs fail closed in `resolve_canonical_team_id`. Team participation is never taken from a
  current roster: structural validation reads the season's authoritative manifest, and the
  historical bootstrap mints deterministic, league-qualified ids only inside trusted ingestion
  and refuses collisions with a different name.
- **Tournament validation.** Documented totals (WC 2018/2022 = 64, EURO 2020/2024 = 51) are
  applied only with a non-empty authoritative participant universe; without one the result is
  `tournament_participant_universe_unavailable` with `evidence_gaps`. UCL/UEL/Nations League have
  no documented total and stay `structural_coverage_unverifiable`.
- **Basketball phases.** Preseason, regular season, play-in, postseason and All-Star stay
  distinct; an unclassified record never counts toward the regular-season minimum; every
  persisted source must itself be `VALIDATED`; a failed ESPN attempt does not block a validated
  API-Basketball fallback; 2026-27 stays `ONGOING` with no invented total.
- **Completion vs acquisition state.** `acquisition_complete` alone never marks a dataset
  `COMPLETE`; lifecycle and persisted-data validation are both required.
- **Verification audit scope.** `verify_persisted_historical_datasets()` deliberately covers the
  full configured competition x season matrix (asserted by an existing test) so a missing dataset
  is never hidden by an edition filter; acquisition itself stays edition-scoped.

### Verification actually performed in this sweep

- `python -m py_compile` on `storage.py`, `data_resolver.py`, `historical_sync.py`, `config.py`,
  `build_phase1_inventory.py`.
- `sports_knowledge.validate()` -> no problems; `build_phase1_inventory.py` -> 106 targets,
  16 `NOT_APPLICABLE`, 0 problems, `database_inspected=False`.
- Provider-isolation regression suites (`test_provider_identity_regression.py`,
  `test_provider_neutral_identity.py`): **13 passed**, both named tests unchanged.
- SQLite smoke runs: approved CSV -> FBref merge yields **one** row (`inserted: 0, enriched: 1`,
  `providers = [football_data_csv, soccerdata_fbref]`, both native ids kept); identical repeat
  `already_existing: 1`; the same numeric fixture id for a different match stays separate; a
  conflicting score keeps the stored result, records the field-level conflicts once, and a repeat
  adds no history entry; a legacy `api_football` row stays isolated and is reported as
  `legacy_identity_overlap: 1`; two pre-existing approved rows for one logical fixture cause the
  next record to fail closed (`ambiguous_logical_fixture_identity`, row count unchanged).
- Season scope: `league 1 / 2018` and `league 1 / 2022` accepted; `league 39 / 2018`,
  `league 1 / 2006` and `league 39 / 2027` rejected.
- Focused suites run: storage/historical-sync/knowledge/provider-isolation **54 passed**;
  queue, direct-CSV, zero-network, multi-source, soccerdata, completion, ingestion and resume
  **100 passed**; coverage-fallback, data-resolver and soccerdata attribution **51 passed**.
- **Full suite: 696 passed, 23 failed** (`python -m pytest -q`). All 23 reproduce on unmodified
  `origin/main` (`1644762`) in a clean worktree (23 failed / 57 passed on the failing files
  there), and the two coverage-fallback failures that this PR branch had introduced now pass.
  The 23 are: `test_migrate_sqlite_to_neon_integrity` (2), `test_neon_persistence_b2` (12),
  `test_p0_p1_integrity_repair` (6), `test_phase3_production_integration` (2),
  `test_historical_workflow_verification` (1).

### Remaining blockers after this sweep

- **UCL / UEL / Nations League totals.** Still no documented edition total and only the
  Sofascore route; coverage stays unverifiable and INCOMPLETE. Needs a verified source.
- **Tournament participant universes.** No authoritative roster is stored, so World Cup and EURO
  datasets stay INCOMPLETE with an explicit evidence gap.
- **Pre-existing duplicate rows.** Detected (`ambiguous_logical_fixture_identity`) and reported
  (`legacy_identity_overlap`); removing them is a Neon/ChatGPT database operation.
- **NBA 2026-27.** No expectation by design.
- **Neon readback.** Still required for persisted phase coverage, league-12 manifests, empty
  `expected_team_ids_json` and the manifest/`error_reason` state for competitions 2/3/5
  (section 10). No database was accessed, queried or modified by this sweep.

## 14. Full sweep (Part 5) — CI #214 diagnosis, ESPN competition evidence, FBref surrogate identity, mapping verification

### 14.1 CI #214 was an interrupted run, not a result

Run [#214](https://github.com/johndeere77900-cyber/Football-agent-/actions/runs/41347216655)
(`3eb451a`) hit the runner's shutdown signal at roughly 89% and exited with code 143. **No pytest
summary was produced**, so neither a pass count nor a failure count can be quoted from it. Nine
failing node IDs were observed before the interruption; the tests after them were never run.

### 14.2 Baseline comparison, reproduced independently

`.venv/bin/python -m pytest -q` on the three files that contain every observed node (branch
`3eb451a`), then the identical command in a clean worktree of unmodified `main` (`1644762`, no PR
commits):

| Tree | Result |
|---|---|
| Branch `3eb451a` | **9 failed, 15 passed** |
| `main` `1644762` (clean worktree) | **9 failed, 15 passed**, same node IDs |

Identical node IDs, identical assertion messages, identical raising lines (only the storage
line number differs, as expected from the branch's added code). **None of the nine is a
regression introduced by PR #54.** This conclusion rests on these two runs, not on any earlier
report.

Observed nodes and messages:

- `test_historical_workflow_verification.py::test_verify_persisted_historical_datasets_football_and_basketball_pass`
  — `ValueError: Cannot mark dataset COMPLETE: the persisted dataset failed validation
  (persisted_basketball_dataset_invalid)`.
- `test_p0_p1_integrity_repair.py::test_A_to_G_pagination_failure_persistence_and_resumption`,
  `::test_later_page_quota_interruption_and_resume_basketball_fallback`,
  `::test_api_exception_checkpoint_and_resume_basketball_fallback`
  — `AssertionError: assert 'INCOMPLETE' == 'COMPLETE'`.
- `test_p0_p1_integrity_repair.py::test_espn_full_month_coverage_completes_basketball_dataset`
  — `Expected 'get_league_games_page' to not have been called. Called 1 times.`
- `test_p0_p1_integrity_repair.py::test_storage_level_complete_invariant_enforcement`,
  `::test_U_to_Y_backtest_persistence_status_and_errors`,
  `test_phase3_production_integration.py::test_basketball_backtest_database_first_e2e`,
  `::test_basketball_backtest_actual_calibration_order`
  — the same `persisted_basketball_dataset_invalid` rejection.

### 14.3 Root causes

Two families, both in the test fixtures rather than the production logic:

1. **Eight nodes — synthetic basketball records carry no provider-native phase.**
   `sample_basketball_game()` in `test_p0_p1_integrity_repair.py` (repeated inline in
   `test_historical_workflow_verification.py` and `test_phase3_production_integration.py`) has no
   `stage`, `season_type` or `provider_provenance`, and the mocked pages bypass
   `basketball_api.get_league_games_page()`, so the rows persist as `unclassified`.
   `_validate_historical_basketball_dataset()` counts only records with recorded
   `regular_season` phase against the independently established season total, so it refuses to
   promote them — which is the required behaviour (missing phase is never regular season). The
   sync then reports `INCOMPLETE` with
   `reason=persisted_dataset_invalid` and `season_types={'unclassified': N}`.
2. **One node — ESPN mock coverage omits positive competition evidence.**
   `espn_full_coverage_patch()` returns coverage metadata without `competition_verified`, and the
   acquisition gate requires an explicit `True`, so ESPN coverage is not validated and the
   documented API-Basketball fallback runs. Also the required behaviour (missing
   `competition_verified` is not proof of NBA membership).

**No test was changed.** Resolving these nodes requires the fixtures to carry the evidence the
production contract demands — a provider-native phase on every synthetic game (`stage:
"Regular Season"` or `provider_provenance.season_type`) and `competition_verified: True` plus
`monthly_details` in the mocked ESPN metadata. Manufacturing that evidence inside production
code, or relaxing the validator, is explicitly out of scope, so the incompatibility is reported
rather than engineered around.

### 14.4 Corrections implemented in this sweep

1. **ESPN aggregation claimed NBA membership without payload evidence**
   (`espn_basketball_provider.py`). `coverage_meta["competition_verified"]` was set to
   `months_failed == 0`, so a season whose months succeeded while returning no verifiable
   league metadata could assert NBA membership — an inference from request success, not from
   the payload. It is now derived from the monthly payloads: no failed month, every month
   succeeded, and at least one month whose own payload was positively verified. A new
   `months_competition_verified` counter records how many months proved membership. Months that
   legitimately hold no events contribute no evidence and neither establish nor break the claim.
   The change is strictly stricter and affects only the ESPN acquisition metadata.
2. **FBref surrogate identity was indistinguishable from a native id**
   (`soccerdata_provider.py`). `_normalize_fbref_row()` always writes a 32-bit SHA-256 digest
   into `fixture.id`, which is the storage identity, even when FBref supplied a native game id;
   nothing marked it internal. Provenance now records `fixture_id_is_surrogate: true`,
   `fixture_id_scheme`, and `provider_fixture_id_is_native`, while the native id stays in
   `provider_fixture_id`. The digest derivation is unchanged, so already-persisted FBref
   identities keep matching idempotently and valid native ids are preserved.

### 14.5 Competition mapping verification (leagues 2, 3, 5)

Verified against the wheel CI actually installs, `soccerdata 2026.43.0` (downloaded with
`pip download --no-deps`, not installed). `soccerdata/_config.py::LEAGUE_DICT` defines FBref
entries for `INT-World Cup` (`FIFA World Cup`), `INT-European Championship` and
`INT-Women's World Cup` only — there is **no** `INT-Champions League`, `INT-Europa League` or
`INT-Nations League` entry — and `MatchHistory` maps only football-data.co.uk domestic codes
(`E0`, `SP1`, `I1`, `D1`, `F1`, `N1`, `P1`). No FBref or MatchHistory mapping for leagues 2/3/5
can therefore be added without inventing an adapter key, so none was added. Sofascore remains
the only configured historical route for UCL/UEL/Nations League, exactly as
`PHASE1_INVENTORY.json` records (`provider_sources` for those competitions lists only
`soccerdata_sofascore`).

### 14.6 Inventory reconciliation

Re-derived from `PHASE1_INVENTORY.json` in this sweep: 106 targets (98 football, 8 basketball),
lifecycle states 75 `COMPLETED` / 16 `NOT_APPLICABLE` / 11 `ONGOING` / 4 `UNKNOWN`,
`knowledge_validation_problems: []`, `database_inspected: false`, evidence gaps 42
`authoritative_participant_universe`, 39 `expected_fixture_total`, 20 `edition_window`. The
provider-source claims above and the per-target `evidence_gaps` match the code; no target claims
completion from a provider response, and persisted counts remain `UNKNOWN` because no database
was read.
