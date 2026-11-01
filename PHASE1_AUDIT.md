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
| K | `storage.py::save_historical_fixtures` season allow-list | Storage accepts only `config.TARGET_SEASONS` (2019-2026) | The catalogued World Cup 2018 edition could not be persisted even if acquired | Football | **Reported.** The queue never requests 2018 because it derives seasons from the same target list; widening the stored season range is a separate contract change |
| L | `espn_basketball_provider` ongoing-season coverage | A season's monthly coverage units span October .. June of the following year; an ONGOING season's future months cannot succeed | An ongoing season can never be marked `COMPLETE` | Basketball | **Correct fail-closed behaviour, now made explicit** in the inventory (`ONGOING`) and the knowledge catalogue (2026 expectation `null`) |

Additional confirmed-but-benign behaviour that was deliberately not changed: provider order
(Football-Data CSV -> FBref -> MatchHistory -> Sofascore), API-Football/football-data.org
exclusion from historical fixtures, basketball ESPN-first then API-Basketball, and every
fail-closed check in both validators.

---

## 3. Competition and season inventory

`PHASE1_INVENTORY.json` is generated by `python build_phase1_inventory.py` from the current
configuration and `sports_knowledge/`. Current generated totals: **104 competition-season
targets** (96 football, 8 basketball), 16 `NOT_APPLICABLE` editions, 74 `COMPLETED`,
11 `ONGOING`, 3 `UNKNOWN`, and **zero knowledge-validation problems**.

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
