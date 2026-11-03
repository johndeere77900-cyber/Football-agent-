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

## 15. Whole-system sweep — CI #215, acquisition fallback and completion evidence

### 15.1 CI #215 reproduction and the local/CI difference

CI #215 (`41364653920`, head `643cce1`) reports 710 passed / 9 failed. A first local run
reported 696 passed / 23 failed. The difference was environmental, not behavioural: the local
virtualenv had no `psycopg`, so `migrate_sqlite_to_neon.psycopg` was `None` and every migration
test raised `AttributeError: None has no attribute 'connect'` (2 in
`test_migrate_sqlite_to_neon_integrity`, 12 in `test_neon_persistence_b2`). Installing
`psycopg[binary]` exactly as `requirements.txt` specifies reproduced CI: **710 passed /
9 failed**, with the same nine node IDs and messages. Both environments run Python 3.12.3 and
collect 719 tests. No suite content, ordering or environment variable differed.

### 15.2 The nine CI nodes and the provider contract

Eight nodes persisted synthetic basketball games with no provider-native phase. API-Basketball
reports the stage of every game (`stage: "Regular Season"`, `Playoffs`, `Preseason`, ...); the
repository already modelled that contract in `tests/test_basketball_backtest.py`. The synthetic
API-Basketball fixtures now carry `stage: "Regular Season"`, so they represent the real payload
and are classified as regular season by the existing production path. The ESPN fixture now
carries `provider_provenance.season_type = regular_season` and a coverage helper that emits one
`monthly_details` entry per season-specific month (13 for 2019-20, 8 for 2020-21, 9 otherwise)
with positive NBA competition evidence, matching `fetch_espn_nba_season_games()`. The completion
validator and every assertion were left strict; no phase is inferred from dates or league.

### 15.3 Root cause of the failed API-Basketball fallback (requests 646-653)

`historical.yml` exports the basketball credential as `API_BASKETBALL_KEY`, but `config.py`
only read `API_FOOTBALL_KEY` and `basketball_api._api_key()` consumed a quota unit **before**
`_headers()` validated the credential. Each of the eight NBA seasons therefore recorded one
`api_basketball` request while no HTTP call was made, produced zero pages and zero records, and
was stored as `FAILED` with the uninformative `api_error_during_acquisition`. That matches the
observed 646-653 request rows and the eight failed manifests exactly. Corrections: resolve
`API_BASKETBALL_KEY` first with a compatible `API_FOOTBALL_KEY` fallback, validate credentials
before reserving quota, and persist the exception type/message and failure stage in
`provider_coverage` (`error`, `failure_stage`). Runtime proof: a missing key now raises
`ValueError` with zero requests consumed.

### 15.4 Empty API-Basketball page could be promoted to COMPLETE

`evidence_empty_units` counted only ESPN's legitimate empty months. An API-Basketball page inside
the provider's own page range that returned no games was dropped from the completion evidence,
so the dataset could be marked COMPLETE with missing games. Empty API pages are now carried into
the completion evidence; the existing strict guard then refuses completion, as the empty-middle-
page regression test requires.

### 15.5 Decorated API-Sports stage labels

API-Sports may label a stage as `"NBA - Regular Season"`. The normalizer now recognises the
documented stage suffix after `" - "` while unknown suffixes still return unclassified. Verified:
`Regular Season`, `NBA - Regular Season` -> `regular_season`; `Playoffs` -> `postseason`;
`NBA - Cup Final` -> `None`.

### 15.6 Run 42

No repository or runtime record defines a historical acquisition run identifier. The manifests,
`api_request_counts` and `operation_requests` expose provider request IDs, not acquisition runs;
GitHub Actions run numbers and dataset seasons are different namespaces. Run 42 is therefore
**not identifiable** from available evidence; the exact workflow run URL or acquisition start
timestamp is required. The latest confirmed activity (requests 646-653, 2026-11-01 18:44-19:30
UTC) is explained in 15.3.

### 15.7 Football overlap counts

The 342 previously reported groups and the 1,696 / 754 candidate groups are not the same query.
The larger counts group by league + canonical teams + kickoff date across all sources, including
rows that are not the same logical fixture; the smaller count was restricted. Neither authorises
a merge. ChatGPT should run the definitions side by side and compare on exact `(source, fixture_id)`
plus the canonical tuple before any repair; no code path merges or deletes rows.

### 15.8 Neon items for ChatGPT (read-only evidence required)

1. For requests 646-653, confirm `api_request_counts` rows and that no `historical_basketball_games`
   row was written for those seasons by `api_basketball`.
2. Persisted NBA rows whose `raw_json.provider_provenance.season_type` is absent: a fresh ESPN run
   now enriches existing `(espn_url, event_id)` rows with `season_type`; rows acquired under a
   different source/ID need a re-acquisition, never an inferred phase.
3. Manifests whose generic `expected_pages`/`coverage_unit` disagree with
   `provider_coverage_json` (for example 9/9 pages alongside 13 ESPN months): record the manifest
   `updated_at` and the run that wrote it. The current code writes the unit of the route that
   actually contributed validated coverage.
4. Football manifests with `secondary_provider_returned_no_fixtures`: capture the provider-level
   error text, which the acquisition report now prints, and the exact competition/season.

## 16. Acquisition reliability and runtime investigation

### 16.1 Run identification

The referenced "run 42" is still **unverified**. There is no acquisition-run identifier in the
repository: manifests are keyed by `(sport, league_id, season)`, `api_request_counts` records
provider requests, and `operation_requests` is unrelated; GitHub run numbers and season labels are
different namespaces. Mapping 42 requires the workflow run URL or its start timestamp.

### 16.2 Real end-to-end traces (live providers, isolated SQLite)

Football, league 39 / 2024, direct CSV provider: **380 fixtures received, 380 valid, 380
persisted**, source `football_data_csv`, total 4.97 s including 1.5 s of deliberately instrumented
SoccerData stubs. The dataset stayed INCOMPLETE in this clean database because no authoritative
team universe exists there, so structural coverage cannot verify; that is the documented guard,
not fixture loss. In a populated database the same CSV result returns after the CSV stage.

NBA, season 2024, live ESPN: **1,397 games received and persisted**, phases recorded as 1,231
regular season, 73 preseason, 84 postseason, 6 play-in and 3 All-Star; ESPN coverage VALIDATED
and dataset COMPLETE in **3.39 s with zero API-Basketball requests**. This proves the current
normalizer persists provider-native phase for real payloads. The production database's 9,361
rows without `raw_json.provider_provenance.season_type` are therefore historical rows written
before that classification was recorded, not a current-pipeline failure. A fresh ESPN run
enriches rows with the same `(espn_url, event_id)`; other source/id pairs need re-acquisition.

### 16.3 Runtime breakdown

The queue processes targets sequentially (`historical_sync.run_historical_queue`). Measured
healthy targets are seconds: 4.97 s for a 380-fixture CSV season (including deliberate stubs) and
3.39 s for a 1,397-game ESPN season. The only hard upper bound visible in code is the shared
primary-provider deadline of 60 s per football target, so a queue of ~96 football targets plus
eight basketball seasons cannot reach five hours from provider work alone unless targets are
spending their full deadline or the database round trips are slow. The five-hour duration is
therefore **not attributed** without the run's logs; the likely candidates that remain are
Neon round-trip latency during per-row persistence, targets exhausting the 60 s SoccerData
budget, and runs executed with `--with-enrichment` (API-Football calls per missing fixture).

To make this measurable, `run_historical_queue` now records `started_at`, `finished_at`,
`total_duration_seconds`, a per-target `target_timings` list
(`duration_seconds`, `coverage_check_seconds`, `acquisition_seconds`, `status`,
`final_stored_count`) and the ten `slowest_targets`, and prints a timing summary. Verified with a
12-target harness: all 12 timings recorded and the summary printed. No acquisition behaviour,
provider order, quota accounting or validation changed.

### 16.4 Neon checks required (ChatGPT)

1. For the reported five-hour run: the workflow run URL/start time, the dataset that was running
   when elapsed, and whether `--with-enrichment` was set.
2. Confirm how many of the 9,361 phase-less basketball rows match an existing
   `(espn_url, event_id)` that a re-run would enrich.
3. Manifests whose `error_reason` is `secondary_provider_returned_no_fixtures`: capture the
   provider-level error printed by the sync (the report now carries `error_detail` for the
   basketball route; the football route still needs resolver error propagation).

## 17. Football failure diagnostics and NBA phase recovery

### 17.1 Football provider failure evidence

`secondary_provider_returned_no_fixtures` was the only persisted explanation of a football
failure. `historical_sync._football_provider_diagnostics()` now records, per approved provider,
the classified outcome, fixture count, provider status, `error_category`, HTTP status and the
truncated provider error. Verified with a runtime harness against league 39 / 2024: CSV failure,
FBref `ACCESS_DENIED` (403), MatchHistory `TIMEOUT` (read timed out) and Sofascore
`PARSER_ERROR` (invalid JSON) were each classified distinctly and the same structure was read
back from the persisted manifest `provider_coverage_json`. The generic manifest `error_reason`
is unchanged for compatibility; the diagnostics explain it.

### 17.2 NBA phase recovery

A phase-less legacy row was created with a real ESPN event identity (`source=espn_url`,
`game_id=401704627`) and the current pipeline was re-run. Result: 1,397 fetched, 1,397 stored,
1,397 unique ids (no duplicate), the legacy row's phase went from unclassified to
`regular_season` (`season_type_id=2`), provider provenance stayed `espn_url`, and one row was
reported as enriched. Records without a matching ESPN event identity cannot be enriched this way
and require reacquisition; unknown phase is still never treated as regular season.

### 17.3 Reconciliation and remaining gaps

Configured football targets now enumerate as: 39/140/135/78/61 CSV+FBref+MatchHistory+Sofascore;
88/94 CSV+MatchHistory+Sofascore; 2/3/5 Sofascore only; 1 FBref+Sofascore; 4 Sofascore (documented
total 51). Independent fixture totals remain unknown for all but league 4 and NBA, so those
datasets stay evidence-limited and INCOMPLETE rather than being asserted complete. UCL/UEL/Nations
and tournament participant universes remain the material gaps.

The five-hour runtime is still unattributed: the instrumented queue is committed, but no
production queue run with Neon latency was available to Freebuff, and a local run cannot
reproduce it. The exit criterion therefore remains open.

## 18. Run traceability, reconciliation reports and attempt timings

### 18.1 Traceable acquisition runs

`run_historical_queue` now creates a unique acquisition run id
(`acq-<UTC timestamp>-<8 hex>`), prints it at queue start, attaches it to every target report and
timing record, and writes a machine-readable run report to
`data/historical_runs/<run_id>.json`. The report records start/finish, total and per-target
duration, coverage-check and acquisition seconds, persistence seconds, database write calls,
API requests consumed by sport, skipped targets, slowest targets and slowest operations. This id
is a new namespace: it does not identify the historical Run 42, which still has no recorded
acquisition id.

Football provider attempts now carry `duration_seconds` in their resolver metadata (success,
failure and timeout paths) and `_football_provider_diagnostics()` includes it alongside
outcome, status, error category, HTTP status and truncated error. The diagnostics are now built
for successful early-return acquisitions as well as exhausted ones; providers that were never
reached are marked `NOT_ATTEMPTED` instead of leaving a misleading null.

### 18.2 NBA phase recovery report

`build_basketball_phase_recovery_report()` (CLI: `--report-nba-phase-recovery`) performs a
read-only exact `(source, event_id)` comparison between persisted NBA records and a fresh ESPN
season acquisition. It reports persisted/ESPN classified and unclassified records, exact
matches, records safely enrichable from a provider-native phase, matches that still carry no
phase, records requiring reacquisition because their identity is absent from the fresh payload,
phase conflicts, provider rejections and fresh events not yet persisted. It writes nothing and
does not infer a phase from dates. Runtime harness (local SQLite, live ESPN 2024): 1,397 fresh
events; one legacy phase-less ESPN row was reported as 1 exact match and 1 enrichable, with 0
conflicts and 0 reacquisition requirements. Empty-database run additionally showed 1,397 fresh
events and 6 provider-rejected events. The production 9,361-row comparison still requires a
Neon read; no recovery percentage is claimed here.

### 18.3 Football manifest reconciliation report

`build_football_manifest_reconciliation()` (CLI: `--report-football-reconciliation`) lists every
persisted football manifest and reconciles it against stored records: manifest versus stored
count, valid distinct identities, duplicates, logical duplicates across sources, source
breakdown, invalid records and reasons, expected/observed/missing canonical teams, date range,
provider outcomes and diagnostics, expected fixture total where independently documented,
structural coverage, lifecycle and the exact remaining blocker. It is read-only and reports an
absent expected-team universe instead of treating it as zero missing teams. Runtime harness
(league 39/2024, 380 real CSV fixtures, manually marked INCOMPLETE): 380 stored, 380 valid
distinct, 0 duplicates, source `football_data_csv`, date range 2024-08-16 to 2025-05-25, and the
exact blocker `expected_team_universe_unavailable`. Running it against Neon is required to
produce the per-target production table for all 96 manifests; that access is not available here.

### 18.4 Verification and limitations

Targeted historical tests: 77 passed, 0 failed (16.36 s) across the queue, sync, completion,
integrity-repair, phase-1 reliability and workflow-verification suites. The reports were run
locally as CLI commands and as the seeded harness described above. CI has not been waited on.
The five-hour runtime remains unattributed, Run 42 remains unverified, and production manifest,
phase-recovery and team-universe counts still require ChatGPT's Neon inspection.

## 19. Persistence bottleneck, identity reconciliation and interruption safety

### 19.1 Fixture persistence batch path

`storage.save_historical_fixtures()` previously called
`team_identity.bootstrap_historical_team_identity()` twice per fixture, and each bootstrap
performed several identity reads/writes. The save path now resolves each distinct
(provider, provider-id/name, competition) team once per batch, then batches fixture inserts and
metadata updates with `executemany`, preserving `ON CONFLICT` semantics and the existing
source-scoped identity, canonical id, raw JSON, provenance, validation and ambiguity rules.

Measured on 380 real Football-Data.co.uk fixtures in a temporary SQLite database:

- 760 team appearances resolved through **20 distinct identity resolutions and 740 cache
  hits** (previously 760 bootstrap calls);
- one batched insert for 380 rows (0 updates on the first run);
- identity resolution 0.0658 s, insert 0.0017 s, commit 0.0038 s, total persistence 0.0814 s;
- repeat run: 380 already existing, 0 inserts, 20 resolutions / 740 hits, no duplicate rows.

The result exposes `identity_resolution_duration`, `insert_duration`, `update_duration`,
`commit_duration`, total persistence, records processed, identity calls/cache hits and
insert/update batch counts. No identity validation was weakened.

### 19.2 Credential and phase contracts

`basketball_api.is_configured()` reports whether a dedicated `API_BASKETBALL_KEY` or the
existing shared API-Football key is available. When the provider client rejects a missing
credential, the sync records `api_basketball_unavailable_missing_credential`, stage
`credentials`, and consumes zero quota instead of reporting a generic provider failure. Direct
harness: unavailable=False, one fallback attempt, zero HTTP requests, zero quota consumed.
ESPN remains primary and the fallback order is unchanged. ESPN season type `4` is explicitly
classified as All-Star alongside the existing preseason/regular/postseason/play-in mapping.

### 19.3 Team identity aliases and conflict report

Configured aliases now cover Nottingham Forest / Nott'm Forest, AZ Alkmaar / AZ, Go Ahead
Eagles / Go Ahead, Fortuna Sittard / For Sittard, NEC Nijmegen / Nijmegen, FC Volendam /
Volendam, RKC Waalwijk / Waalwijk and Sparta Rotterdam / Sparta. A direct harness resolved each
full and short name to the same non-null canonical id. The new read-only
`build_identity_conflict_report()` (CLI: `--report-team-identity-conflicts`) groups persisted
mappings by normalized name and competition, lists divergent canonical ids and only proposes a
target where the configured alias map names one. A seeded two-mapping conflict was reported with
a proposal; no production mapping was changed.

### 19.4 Queue interruption, checkpoints and workflow evidence

A SIGINT/SIGTERM handler stops new targets, lets the current target's transaction finish, writes
a checkpoint and marks the run interrupted. Runtime harness (two queued targets, SIGTERM during
the first): exactly one target processed, checkpoint present, report `interrupted=true`,
`in_progress_target` recorded, and `main.py --historical-queue` returned **130**. The run report
now carries the run id, last completed target, in-progress target and interrupt signal. The
workflow adds job/step timeouts and always-upload of `data/historical_runs/` diagnostics; YAML
parsed successfully.

Both report commands expose a recommended recovery action. The football report proposes
expected-team-universe resolution, missing-coverage acquisition, lifecycle verification or
diagnostic inspection; the NBA report distinguishes ESPN enrichment, reacquisition of unmatched
identities, unavailable phase evidence and no action.

**Verification:** full suite **719 passed, 0 failed (91.21 s)**. No production Neon row, schema or
mapping was modified. Production reconciliation, authoritative tournament participant universes
and the five-hour run cause remain external-evidence blockers.

## 20. Comprehensive edition knowledge, provider routing and completion gating

### 20.1 Edition-level catalogue

`sports_knowledge/` now has a version-2 edition layer (`football_editions.json`,
`basketball_editions.json`) loaded beside the existing competition catalogues. Each recorded
edition carries its own participants and canonical ids, competition-context aliases, format and
stages, fixture/coverage expectation, promotion/relegation context, source URL, retrieval date,
confidence and explicit evidence gaps. Participant lists are never inferred from acquired
fixtures or from a current-season roster.

Verified participants are present for:

- Premier League 2019/20 through 2025/26 (seven edition-specific rosters);
- La Liga 2019/20, 2023/24 and 2024/25;
- Serie A, Bundesliga, Ligue 1, Eredivisie and Primeira Liga 2023/24 and 2024/25;
- FIFA World Cup 2018 and 2022;
- UEFA EURO 2020 and 2024;
- NBA 2019/20 through 2026/27, with phase-specific coverage expectations.

The remaining configured seasons have no fabricated roster. `sports_knowledge.knowledge_gap_report()`
covers every configured competition from 2019 through the 2030 horizon and reports each missing
window, participant universe or format expectation explicitly. The generated inventory summarises
this as 31 CONFIRMED, 99 PROVISIONAL, 4 UNKNOWN and 24 NOT_APPLICABLE edition evaluations.

### 20.2 Operational integration

- Team identity: catalogue participant aliases are merged into the sport/competition alias map;
  config aliases retain precedence. Every alias remains scoped by league id.
- Expected universes: `_get_authoritative_historical_team_ids()` checks a verified edition roster
  before provider metadata or a persisted manifest. A provider-declared universe is treated as a
  competing claim and is never silently replaced; unresolved or unregistered catalogue ids fail
  closed.
- Structural validation: cup, international and tournament formats now consume verified catalogue
  participants; a match total alone cannot prove completion.
- Coverage expectation: edition fixture totals and NBA regular-season totals feed the acquisition
  expectation. Unverified editions stay UNKNOWN rather than inheriting today’s team count.
- Provider routing: `provider_route()` filters the approved order by the competition’s declared
  provider support. Unsupported providers are skipped and the route, blockers and evidence status
  are attached to acquisition diagnostics.
- Completion: `get_competition_lifecycle()` now requires CONFIRMED knowledge evidence in addition
  to verified structural coverage. PROVISIONAL and UNKNOWN editions cannot become COMPLETE.
- Prediction context: football and basketball predictions attach read-only
  `sports_knowledge_context` with edition evidence status, coverage expectations, gaps and verified
  roster membership. It never changes probabilities.
- Refreshability: `refresh_source()` updates source retrieval metadata without rewriting edition
  facts; future annual editions and explicitly cadenced international editions are enumerated
  through 2030 as provisional.

### 20.3 Verification and remaining evidence work

Full suite: **730 passed, 0 failed (109.84 s)**. `sports_knowledge.validate()` reports no problems;
the regenerated inventory has 150 targets, 16 NOT_APPLICABLE and 0 knowledge problems; NBA 2026/27
correctly remains PROVISIONAL because no final regular-season total is asserted.

No Neon database was read or modified. Remaining authoritative evidence gaps are the unverified
historical rosters/coverage totals reported by the knowledge report, especially UCL, UEL, Nations
League and the non-verified domestic seasons. Those gaps are reported, not guessed.



## 21. Acquisition recovery and production reconciliation (final pass)

### 21.1 What was corrected

- **Football manifest audit could hide invalid data.** It treated every
  `COMPLETE` label as `blocker=None`. The audit now checks the persisted rows
  first: count reconciliation, per-row validation, duplicates, authoritative
  participant universe, structural coverage and lifecycle. A `COMPLETE` manifest
  that fails any check is reported as inconsistent and is never counted as a
  pass. `valid_distinct_count` is now counted from valid identities rather than
  derived by subtraction.
- **NBA phase gaps had no manifest-level diagnosis.** A new read-only
  `build_basketball_manifest_reconciliation()` reports, per NBA manifest, the
  actual row count, source and phase counts, unclassified records by source,
  invalid/duplicate identities, provider coverage units and statuses, the
  independently established regular-season expectation, lifecycle and the first
  exact blocker. Missing provider-native phase is reported as
  `season_type_metadata_missing`; it is never folded into the regular season.
- **The phase-recovery report only explained `espn_url` rows.** It now reports
  every source, exact identity matches, phase counts, missing phase by source,
  matched-but-unclassified records and non-ESPN phase gaps. An API-Basketball id
  is never treated as an ESPN event identity, so it is never counted as
  recoverable from ESPN evidence.
- **Basketball sync now names the validation blocker.** When persisted
  validation fails because records have no provider-native phase, the manifest
  reason is `season_type_metadata_missing`; when the season total is unavailable
  it is `expected_season_coverage_unavailable`; when the regular season is short
  it is `regular_season_coverage_insufficient_<actual>_of_<expected>`. The generic
  `api_error_during_acquisition` string is no longer the only explanation.
- **Basketball persistence is batched.** One bounded pre-read per page feeds one
  batched insert and one batched additive update. The merge contract is
  unchanged: identity, date, scores, completed status and canonical team
  identities are preserved, and conflicts are reported rather than overwritten.
- **Provider outcomes use the full vocabulary.** `UNSUPPORTED_COVERAGE` now
  identifies a 404/unsupported endpoint instead of collapsing it into a generic
  failure. Access denial, rate limiting, timeouts, network errors and parser
  failures remain distinct, and none is ever a valid empty result.

### 21.2 Commands

```bash
python3 main.py --report-football-reconciliation
python3 main.py --report-basketball-reconciliation
python3 main.py --report-nba-phase-recovery --seasons 2019 2020 2021 2022 2023 2024 2025 2026
python3 main.py --verify-historical-datasets --historical-queue --seasons 2019 2020 2021 2022 2023 2024 2025 2026
```

The reports are read-only. They write their JSON evidence under
`data/historical_runs/` and do not change a manifest, a record or a provider
counter.

### 21.3 Local verification actually performed

- `python -m py_compile` on `storage.py`, `historical_sync.py`,
  `data_resolver.py`, `main.py` and `config.py`.
- Seeded local SQLite smoke: an ESPN row with a verified phase plus one row with
  no phase produced
  `phase_counts={'regular_season': 1, 'unclassified': 1}`,
  `phase_missing_by_source={'espn_url': 1}` and the exact blocker
  `season_type_metadata_missing`. The phase-recovery report, with no network
  call, reported the same source split and one missing ESPN phase.
- Selected regression suites: `tests/test_phase1_reliability_knowledge.py`,
  `tests/test_historical_sync.py`, `tests/test_storage.py` and
  `tests/test_p0_p1_integrity_repair.py` -> **57 passed**.
- `python main.py --help` parses and exposes both reconciliation commands.

### 21.4 Production reconciliation requirements (ChatGPT / Neon only)

No production Neon connection, SQL, migration or write was performed here. The
following read-only checks are required to turn the production observations into
a reconciled manifest table.

1. **Football count discrepancy (23,110 earlier vs 19,698 on 3 November).** Run a
   table count and identity decomposition, not a deletion assumption:

   ```sql
   SELECT count(*) AS raw_rows FROM historical_fixtures;
   SELECT count(DISTINCT (source, fixture_id)) AS distinct_source_ids
   FROM historical_fixtures;
   SELECT source, count(*) AS rows, count(DISTINCT fixture_id) AS distinct_ids
   FROM historical_fixtures GROUP BY source ORDER BY source;
   SELECT league_id, season, count(*) AS rows
   FROM historical_fixtures GROUP BY league_id, season
   ORDER BY league_id, season;
   ```

   Then group by canonical identity
   `(canonical_home_id, canonical_away_id, date_trunc('day', kickoff_at))` to
   separate legitimate rows from pre-existing cross-provider duplicates. The
   difference between raw rows and distinct logical fixtures is the
   reconciliation gap; neither historical total is accepted as correct until
   this decomposition is recorded.

2. **All 96 football manifests.** Run
   `python3 main.py --report-football-reconciliation` against Neon. For each
   manifest compare `fixture_count` with the persisted count, `expected_team_ids_json`
   with available canonical teams, and the recorded `provider_outcomes` with the
   stored `source`/provenance values. The report's `exact_blocker` is the
   database-side action; do not mark a manifest COMPLETE to clear it.

3. **All 8 NBA manifests.** Run
   `python3 main.py --report-basketball-reconciliation` and
   `python3 main.py --report-nba-phase-recovery`. For each season capture:

   ```sql
   SELECT league_id, season, source, count(*) AS rows
   FROM historical_basketball_games
   WHERE league_id = 12
   GROUP BY league_id, season, source
   ORDER BY season, source;

   SELECT league_id, season, count(*) AS missing_phase
   FROM historical_basketball_games
   WHERE league_id = 12
     AND (raw_json->'provider_provenance'->>'season_type' IS NULL
          OR raw_json->'provider_provenance'->>'season_type' IN ('', 'unclassified', 'unknown'))
   GROUP BY league_id, season ORDER BY season;
   ```

   The exact ESPN `(source, game_id)` rows are enrichable by the normal
   ESPN-first sync. Rows from another source require that source's own refresh or
   reacquisition; they are not counted as recovered. The production total of
   9,361 remains an observed input, not a recovery result, until this comparison
   is run over the database.

4. **Manifest/schema checks.** Confirm `historical_datasets` has 104 rows (96
   football, 8 basketball), that every `provider_coverage_json` is valid JSON,
   and that no `status='COMPLETE'` manifest has a count mismatch or a failed
   persisted validator. Do not delete rows to make a count agree.

### 21.5 Remaining external limitations

- UCL, UEL, Nations League and the non-verified domestic editions still lack
  authoritative participant universes and/or independently documented match
  totals. Their datasets stay fail-closed and are not claimed complete.
- No production run of the new reconciliation commands was possible here. The
  reports must be run by ChatGPT/Neon against production.
- The five-hour Run 42 remains unattributed: no acquisition-run id, workflow
  timestamp or logs identify it. New runs now emit a unique `acq-*` id,
  per-target timings, checkpoints and run reports; that does not retroactively
  identify Run 42.
- No live, prediction, backtest, grading, training or Telegram path was changed.

## 22. Data-integrity implementation sweep (2026-11-03)

This section records repository implementation only. It does **not** claim that
production Neon data was repaired. ChatGPT/Neon owns production inspection,
SQL/migrations, manifest changes and data repair.

### 22.1 Logical football reads versus physical audit reads

- `storage.py` exposes `get_historical_fixtures(..., include_reconciliation_candidates=False)`
  and `get_team_historical_fixtures(..., include_reconciliation_candidates=False)`.
  The default reads exclude rows whose
  `reconciliation_metadata.legacy_duplicate_candidate` is true.
- PostgreSQL uses `raw_json #>> '{reconciliation_metadata,legacy_duplicate_candidate}'`;
  SQLite uses `json_extract` and treats its boolean as `1`. Malformed SQLite JSON
  is not hidden by the predicate.
- `get_historical_fixture_count()` remains the physical count. Manifest/storage
  count validation was not weakened.
- Acquisition verification, raw inventory, manifest reconciliation and duplicate
  audits explicitly request the physical set. This includes `historical_sync.py`,
  `build_phase1_inventory.py`, and the internal persisted-record helper.
- `build_football_manifest_reconciliation()` now reports physical rows, eligible
  rows, annotated candidates, eligible logical identities, unmarked duplicate
  groups/rows and ambiguous identities separately. The 12 annotated production
  candidates are therefore retained and visible rather than silently deduplicated
  or hidden from raw counts.

### 22.2 Persisted NBA manifest revalidation

- `_basketball_persisted_revalidation()` rebuilds completion evidence from
  persisted games and provider-native coverage. It independently checks count,
  source-scoped identities, structural validity, provider phase, exact ESPN
  monthly units, monthly success/NBA verification, and the configured regular-season
  expectation.
- A failed API-Basketball fallback that persisted zero games is reported as
  `ignored_provider_coverage` and does not invalidate a validated ESPN dataset.
- The result exposes `persisted_revalidation_eligible`, exact blocker,
  `another_provider_request_necessary`, provider-specific expected/completed units
  and each validation check. The reconciliation report includes the same evidence.
- The sync uses this path before acquisition and promotes a completed season with
  the existing storage completion API when the evidence passes. It does not trust
  the old `INCOMPLETE` label.
- The active 2026/27 NBA season has no configured completed-season expectation and
  is blocked by its ongoing lifecycle before the expected-total branch. No total
  is invented and no scheduled game is treated as missing historical coverage.
- `espn_basketball_provider.py` now separates non-final scheduled events from
  malformed/rejected events: they are counted diagnostically as
  `events_non_final`, not as corruption or failed HTTP coverage units.
- Persisted monthly evidence is checked against the exact keys derived from
  `get_season_month_offsets(season)`, including the 2019-20 13-month and 2020-21
  8-month contracts. Missing, duplicate, unexpected or malformed keys fail closed.
- Monthly `events_accepted`, `events_rejected`, `events_non_final` and
  `rejection_reasons` are reconciled. Historical `event_not_final` rejections
  remain unresolved until a provider refresh establishes their outcome, and any
  other rejected event blocks promotion as well. Active-season lifecycle is
  evaluated first, so the 2026/27 season keeps its ongoing blocker and does not
  request historical revalidation it cannot use.
- The reconciliation report now exposes expected/actual/missing/unexpected/
  duplicate month keys, per-month rejection totals and reasons, unresolved
  historical non-final evidence, and whether the rejection evidence is internally
  consistent.

### 22.3 Enrichment quality and provenance

- `save_historical_enrichment()` populates the typed statistic columns,
  `field_mask` and `provider_version`; `football_data_csv` is a recognized
  enrichment source. Existing verified values are not erased by a later empty or
  partial payload, and an identical same-source refresh remains idempotent.
- `get_historical_enrichment()` reconstructs missing statistics from the typed
  columns while retaining the raw provider payload.
- Football enrichment status is no longer based on row count. It is computed per
  finished fixture from usable home/away values in either the main fixture or its
  enrichment record. `COMPLETE` requires every configured required field
  (shots, shots on target, corners and yellow/red card values); `PARTIAL` means at
  least one usable field; empty rows alone produce `NONE`.
- The report carries field-level coverage for shots, shots on target, corners,
  cards, possession and xG. No verified xG source is present in the inspected
  data, so xG is reported as unavailable rather than invented.
- `backtest.py::_merge_enriched_fixture()` now uses
  `storage.merge_historical_fixture_metadata()`: nested statistics merge
  additively, existing non-null values win, conflicts retain provenance, and
  fixture identity/result fields are not replaced by a partial enrichment.

### 22.4 Remaining production evidence gaps

- Production Neon still needs the read-only reconciliation commands and the
  controlled post-reconciliation acquisition. This code does not prove the 12
  annotations are correct, that 9,565 NBA rows all pass the new persisted check,
  or that any manifest has been promoted.
- The 23,110 vs 19,698 football count decomposition and the Run 42 attribution
  remain external investigations.
- UCL/UEL/Nations League/domestic participant universes and documented totals
  remain unavailable; no totals were invented.

---

## 14. Acquisition integrity update (2026-11-03) — providers, source cohort, route

Branch: `feature/historical-football-acquisition-integrity`
Base: `main` at `7dc9fe83101c876898a5174cf3125fc536d73ada`

This section supersedes the earlier four-provider route described in sections 1-13;
the earlier sections are retained as the audit trail for that branch.

### 14.1 Runtime route

`config.FOOTBALL_APPROVED_HISTORICAL_PROVIDERS` and
`sports_knowledge/football_competitions.json::approved_historical_acquisition_order`
now contain exactly:

`openfootball_json -> openfootball_txt -> openfootball_internationals -> openligadb -> football_data_csv -> soccerdata_fbref -> soccerdata_match_history`

`DataResolver.get_league_fixtures_page()` calls those providers in that order,
after checking competition/edition eligibility through
`sports_knowledge.provider_route()`. Sofascore is not in the automatic route.
Its catalogue entry remains diagnostic-only. If the knowledge route cannot be
loaded or disagrees with `config`, the route fails closed with
`knowledge_provider_route_unavailable` / `knowledge_provider_order_mismatch`.

### 14.2 New adapters

- `openfootball_provider.py` implements OpenFootball JSON, Football.TXT club
  competitions and men's senior internationals. It preserves exact source URLs,
  repositories, source commits where available, requested season, retrieval
  time, native/derived ids and source facts. JSON/TXT fixtures without native
  ids use deterministic indexed SHA-256 ids; OpenLigaDB keeps its native
  `matchID`.
- `openligadb_provider.py` discovers the exact season shortcut through
  `getavailableleagues/{season}`, caches discovery per acquisition run, paces
  requests to 60/minute, selects the full-time result row, preserves
  `matchIsFinished`, goals and result types, and uses UTC timestamps.
- Date-only sources are stored as technical midnight UTC with
  `date_precision="date"`, `kickoff_time_known`, local time where available and
  `timezone_known=false`. No UTC kickoff is inferred.
- Unscored/future fixtures are not marked completed. UCL JSON 2024 has 189 rows
  but only 188 completed because the final is unscored in that file.

### 14.3 Verified direct coverage

OpenFootball JSON (2019-20, 2024-25, 2026-27 exact files checked):

| Competition | 2019 | 2024 | 2026 |
|---|---:|---:|---:|
| Premier League | 380 | 380 | 45 |
| La Liga | 380 | 370 | 64 |
| Serie A | 380 | 370 | 50 |
| Bundesliga | 306 | 306 | 33 |
| Ligue 1 | 279 | 306 | 41 |
| Eredivisie | 232 | 306 | 62 |
| Primeira Liga | 306 | 306 | 58 |
| Champions League JSON | 119 | 188 completed of 189 | 404, not enabled |

Ligue 1 2019-20 (279) and Eredivisie 2019-20 (232) are legitimate curtailed
seasons and validate as complete against their authoritative roster and recorded
season exception. La Liga/Serie A 2024-25 files contain 370 of 380 rows, so they
do not complete from JSON alone and correctly fall through to the next eligible source (Football-Data CSV for La Liga; Football.TXT then CSV for Serie A).
The 2026-27 files are in progress and do not validate as complete.

OpenFootball Football.TXT 2024-25: Champions League 189, Europa League 189,
Premier League 380, Serie A 380. Diagnostic-only exact files: Conference League
153, EFL Cup 93, FA Cup 123, Coppa Italia 45, Copa Libertadores 155,
Brasileiro 380. The last six are not stored because no authoritative competition
id is configured.

OpenFootball internationals: Nations League 2024 158 matches / 54 teams, EURO
2024 51 / 24, EURO 2020 51 / 24, World Cup 2022 64 / 32. EURO and World Cup
validate as complete against catalogue participant universes and documented
totals. Nations League remains unverified because its edition participant/format
evidence is not in the catalogue; it returns fixtures with `is_complete=false`.
UCL/UEL remain `completion_allowed=false` for lack of an authoritative edition
participant universe, even though the source files contain the expected 189
matches.

OpenLigaDB Bundesliga 2024-25: exact shortcut `bl1`, 306 native-id fixtures,
halftime and fulltime rows preserved, validates as complete. Season discovery is
cached per run.

### 14.4 Storage and integrity

`get_historical_fixtures()` / `get_historical_fixture_count()` accept optional
`sources=`; `sources=None` still returns/counts every physical row. Approved
cohort reads accept `mixed` writer rows only when record-level provenance names
an approved provider. Direct SQLite verification: 306 approved Bundesliga rows
validate while 2 legacy `api_football` rows remain physically present; the
all-source caller sees 308 and the approved cohort sees 306. Native fixture ids
are unchanged by the filter. `save_historical_fixtures()` now resolves identity
from the record's own provider, not the `mixed` writer label, and rejects
unapproved record provenance in a `mixed` batch.

Bundesliga 2024-25 stage exclusion: the two SV Elversberg relegation-playoff
fixtures are excluded from regular-season coverage (306 + 2 excluded) without
deleting or rewriting them.

### 14.5 Provider outcomes and retries

`_classify_provider_attempt_outcome()` now distinguishes `PROVIDER_UNAVAILABLE`
(configuration/credential missing), `UNSUPPORTED_COVERAGE`, `ACCESS_DENIED`,
`RATE_LIMITED`, `TIMEOUT`, `NETWORK_ERROR` and `PARSER_ERROR`, preserving the
provider error and HTTP status. `soccerdata_provider._attempt_timeout_budget()`
divides the remaining shared deadline across remaining attempts: a 15-second,
two-attempt budget allocates 7.5s + 7.5s rather than 15s + 0s.

### 14.6 Remaining gaps

- No authoritative participant universe is configured for UCL, UEL, Nations
  League 2024, Conference League, FA Cup, EFL Cup, Coppa Italia, Libertadores or
  Brasileirao. No totals or participants were invented; those datasets fail
  closed for completion until official edition rosters and totals are recorded.
- OpenFootball JSON 2024-25 La Liga/Serie A and all 2026-27 domestic files are
  incomplete by source and do not complete on their own.
- OpenFootball JSON UCL 2026 is a 404 and remains disabled; TXT enables only the
  exact 2019 and 2024 files that were checked.
- No API-Football historical provider, alias, fallback, credential requirement or
  legacy-record merge was added.
- No production Neon access, migration, deletion or record rewrite was performed.

---

## 23. Integration sweep (2026-11-03) — CI SIGKILL diagnosis, hermetic tests, comprehensive basketball knowledge

### 23.1 CI failure was a runner OOM kill, not a test result

PR #55's CI run `41639327368` (head `517c2ad`) completed with **exit code 137** after
about 8m31s. 137 is SIGKILL; the workflow step is `python -m pytest tests -v`.
No pytest summary was produced, so no pass/fail count can be quoted from that run.
The check-run annotation only records `Process completed with exit code 137`.

Local reproduction found the cause: `tests/test_architecture_boundary.py` exercised
resolver fallback legs that reached live SoccerData downloads. The three slowest
tests were 55.73s, 29.45s and 27.23s, and the suite visibly stalled inside
`soccerdata_provider.get_match_history_games` / `get_sofascore_historical_games`
(`subprocess.Popen` -> `_communicate`). Those downloads are unbounded in memory on
a CI runner, which is consistent with a SIGKILL rather than an assertion failure.

Correction: the boundary suite now installs autouse stubs for every live provider
entry point it can reach (SoccerData MatchHistory/FBref/Sofascore/team history,
API-Football and football-data.org), while each test still overrides the provider it
asserts on. `tests/test_architecture_boundary.py` now runs **22 passed in 2.86s**
(previously 118.69s), and the live-network tests 3 and 6 are explicitly stubbed.
No assertion was weakened and the static architecture-boundary check is unchanged.

### 23.2 Provider-route contract updates

The approved route is the seven-provider order (`openfootball_json`,
`openfootball_txt`, `openfootball_internationals`, `openligadb`,
`football_data_csv`, `soccerdata_fbref`, `soccerdata_match_history`). Two tests still
asserted the retired four-provider order:

- `tests/test_zero_network_and_provider_order.py` now mocks every approved stage,
  proves the early stop at the first sufficient provider, asserts the approved
  execution order, and keeps the retired API-Football / football-data.org providers
  off the historical path.
- `tests/test_phase1_reliability_knowledge.py::test_approved_provider_orders_are_unchanged`
  now records the seven-provider order.

`DataResolver.get_league_fixtures_page()` also served page 2 of a COMPLETE dataset
through the provider route instead of the read-first page. The read-first branch now
runs for every page and returns the fixtures for page 1 and an empty page for page > 1,
without a provider call.

### 23.3 Approved-cohort test fixtures

Backtest tests persisted synthetic fixtures with no provider provenance, so the
default legacy `api_football` source put them outside the approved historical cohort
introduced by this PR; `mark_historical_dataset_complete()` then correctly refused
the dataset. The fixtures now declare the approved `football_data_csv` source.
The completion guard was **not** relaxed: a dataset with only legacy rows still fails
closed. Five backtest suites now pass: **43 passed in 3.37s**.

### 23.4 Comprehensive basketball knowledge

`sports_knowledge/basketball_editions.json` now carries, for every NBA edition
2019-2026:

- competition access: `NOT_APPLICABLE` with the evidenced explanation that the NBA is
  a closed 30-franchise league (route `franchise_membership`);
- promotion/relegation: `NOT_APPLICABLE`, no promoted/relegated clubs;
- qualification: a 16-team postseason (12 direct + 4 play-in) for 2020-2026, and the
  documented conditional 2020-bubble play-in for 2019-20;
- results: verified champions, runners-up and Finals series scores for 2019-20 through
  2024-25; 2025-26 is `STRUCTURE_VERIFIED_RESULTS_UNKNOWN`; 2026-27 is `UNKNOWN`.
  No unplayed result is invented.

`telegram_bot.handle_knowledge_op()` now answers NBA champion, qualification, access,
transition and results questions from those evidenced records instead of refusing
basketball. `sports_knowledge.validate()` returns `[]`.

### 23.5 Verification performed

- `tests/test_phase1_reliability_knowledge.py` + `tests/test_telegram_bot.py`:
  **53 passed** (includes the new NBA knowledge and agent tests).
- Backtest suites: **43 passed**.
- Zero-network/provider-order + boundary suites: **27 passed**.
- Targeted coverage/data-resolver route tests: **6 passed**.
- `python -m py_compile` on `data_resolver.py` and `telegram_bot.py`; catalogue
  validation clean.

### 23.6 Remaining failures (not claimed as fixed)

A full parallel run still reports failures in legacy tests that assume the retired
acquisition chain (notably `tests/test_data_resolver.py::test_F_soccerdata_with_complete_metadata_complete`
and `tests/test_data_resolver.py::test_sofascore_fallback_error_reporting_captured`,
which still expect SoccerData/Sofascore to be in the automatic route). Those tests
were not rewritten in this sweep; no assertion was deleted or relaxed, and no
production guard was weakened to accommodate them. The full run did not produce a
final summary because the pytest session stalled in teardown (xdist controller did
not exit), so this section does not quote a total pass/fail count.

No production Neon connection, SQL, migration, deletion or record rewrite was
performed. The 23,110 vs 19,698 production count decomposition remains outside this
change and reserved for ChatGPT.

Final parallel sweep record: **695 passed, 38 failed, 0 skipped** (JUnit report, 733 tests). Four of those 38 were fixed after the report was written (both coverage-fallback scenarios, the data-resolver provider-order test and the page-2 read-first regression, all verified passing in targeted runs). The remaining failures are pre-existing legacy-chain tests that this sweep did not rewrite; they are listed in the CI log and are not claimed fixed.
