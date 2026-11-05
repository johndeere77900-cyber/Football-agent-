# Historical Data Source Inventory

Status legend:

- **VERIFIED AND APPROVED** — tested against the live source, evidence recorded, usable for the named competitions.
- **VERIFIED BUT CONDITIONAL ON IDENTITY RECONCILIATION** — the source demonstrably carries the data, but our stored fixtures cannot yet be matched to it reliably. Not usable until a reviewed reconciliation reaches an acceptable rate.
- **DISCOVERED / NOT YET VERIFIED** — reachable, but at least one verification criterion is unmet. Not usable.
- **REJECTED** — tested and unsuitable for this purpose.

No production rows were written while producing this inventory. Every probe below was read-only.

## 1. VERIFIED AND APPROVED

### 1.1 `espn_soccer_summary`

| Attribute | Value |
|---|---|
| Access | `GET https://site.api.espn.com/apis/site/v2/sports/soccer/{competition}/scoreboard?dates=YYYYMMDD` then `.../summary?event={id}` |
| Competitions | `fifa.world`, `uefa.euro`, `uefa.nations` (also `uefa.champions`, `uefa.europa` — see 2.1) |
| Fields | `totalShots`, `shotsOnTarget`, `wonCorners`, `yellowCards`, `redCards`, `possessionPct`, `foulsCommitted`, `offsides`, `saves`, plus passing/crossing metrics, `penaltyKickGoals`; period scores in `linescores` |
| Player statistics | **None** — `boxscore.players` empty on every soccer match sampled |
| xG | **None** |
| Evidence | HTTP 200 on all probes. Euro 2020 (played 2021) and 2024, World Cup 2022, Nations League 2020 and 2024 each returned all nine required fields. Measured identity match rate: World Cup 2022 **100%** (30/30), Euro 2024 **100%** (29/29), Nations League 2024 **93%** (28/30), Nations League 2020 **82%** (23/28), Euro 2020 **52%** (15/29). |
| Limitations | Date **ranges return HTTP 400** (one date per request); no player data; no xG; display names differ from ours. |

### 1.2 Existing approved sources (unchanged, already in production use)

- `espn_boxscore` (basketball): team boxscores, period scores, player lines. In use for 847 games.
- `football_data_csv`: shots, shots on target, corners, cards, fouls for seven mapped domestic divisions. In use for 17,111 statistics rows.
- `openfootball_json`, `openfootball_txt`, `openfootball_internationals`, `openligadb`: fixtures and results only, no match statistics.

## 2. VERIFIED BUT CONDITIONAL ON IDENTITY RECONCILIATION

### 2.1 `espn_soccer_summary` for Champions League and Europa League

The provider demonstrably has the data — UCL 2019 returned 28 events across 16 dates with statistics on every date sampled — but our stored fixtures do not match reliably:

| League-season | Probed | Exact date | Offset ±1 | Rate |
|---|---|---|---|---|
| UCL 2019 | 12 | 1 | 0 | 8% |
| UCL 2022 | 12 | 0 | 0 | 0% |
| UCL 2024 | 12 | 0 | 0 | 0% |
| UCL 2025 | 12 | 2 | 0 | 17% |
| UCL 2026 | 12 | 4 | 0 | 33% |
| Europa League 2022 | 28 | 12 | 0 | 43% |
| Europa League 2023 | 27 | 14 | 0 | 52% |
| Europa League 2024 | 28 | 6 | 0 | 21% |

Diagnosis from inspecting the failures directly (this is evidence, not inference):

1. **Qualifying rounds are missing from the main slug.** Our stored UCL fixtures include preliminary and qualifying rounds — e.g. `2024-07-09 Panevezys vs HJK Helsinki`, `2022-06-21 La Fiorita vs Inter Escaldes`. The `uefa.champions` scoreboard returns `[]` for those dates. ESPN publishes qualifying under a different competition slug that has not yet been identified.
2. **Team aliases, not missing data.** `2025-09-16 PSV vs Union SG` exists in ESPN as `psveindhoven vs unionstgilloise`; `Benfica` appears as `benfica` against our `slbenfica`. A similarity threshold of 0.86 rejects these, so an explicit alias table would convert real matches into positives.
3. **Suspect stored dates.** Our dataset places `2019-08-07 Juventus vs Lyon` and `2019-08-07 Manchester City vs Real Madrid` on a date when ESPN lists only third-qualifying-round ties (Krasnodar–Porto, Basaksehir–Olympiacos, Cluj–Celtic). Those fixtures are not real for that date, so they can never match and must not be forced.
4. **Offset windows do not help.** ±1 day added zero matches across every season tested, so date drift is not the explanation.

## 3. DISCOVERED / NOT YET VERIFIED

### 3.1 StatsBomb Open Data — the only verified xG source found

| Attribute | Value |
|---|---|
| Access | `raw.githubusercontent.com/statsbomb/open-data` (competitions, matches and events JSON) |
| Verified content | A sampled Champions League match (`3888711`, 1971 final) returned **3,529 events, 34 shots, 34 with `statsbomb_xg`** (sample 0.21402292, outcome Goal). xG genuinely works. |
| Coverage found | Champions League 1970/71 through **2018/19** (season_id 4); World Cup 2022, 2018, 1990, 1986, 1974, 1970, 1962, 1958; Africa Cup of Nations 2023 — 80 competition-seasons in total |
| Coverage absent | **No Euro, no Nations League, no Champions League after 2018/19, no Europa League after 1988/89** |
| Why not approved | (a) Per-season match counts are **not measured** — some season files contain a single match, so coverage cannot be assumed; (b) records are keyed by StatsBomb match ids our fixtures do not store, so identity resolution is unsolved; (c) the dataset is licensed for non-commercial use, which needs an explicit decision. |

### 3.2 basketball-reference.com

- `https://www.basketball-reference.com/boxscores/202401150LAL.html` → HTTP 200, 29 tables, `id="box-"` present, team totals present, and per-player columns `fg, fga, fg3, fta, trb, ast, stl, blk, tov, pf` parse cleanly.
- Value: an independent cross-check for ESPN basketball statistics, and a possible source for seasons where ESPN summaries are thin.
- Why not approved: scraping terms, request rate limits and page-structure stability are unverified; no coverage measurement across 9,584 games has been done.

### 3.3 Not established

- **eu-football.info** returned HTTP 200 with an empty body for the probed match URL — inconclusive, needs a valid id.
- **flashscore.com** returned HTTP 200 but is JavaScript-rendered; no structured feed identified.
- **FotMob** match API returned HTTP 404 for a probed id and its search endpoint also 404 — no usable contract established.

## 4. REJECTED

| Source | Probe | Reason |
|---|---|---|
| balldontlie | `api/v1/games` → **HTTP 404** | v1 API retired; current API requires a key. |
| cdn.nba.com boxscore | → **HTTP 403** "Access Denied" | Blocked. |
| stats.nba.com | connection failed (HTTP 000) | Blocked from this environment. |
| worldfootball.net | → **HTTP 403** (Cloudflare challenge) | Bot protection. |
| api.sofascore.com | `event/{id}/statistics` → **HTTP 403** | Access denied. |
| football-data.org | `competitions/CL/matches` → **HTTP 403** | Requires an API key; not an approved historical provider. |
| api-football (api-sports) | `fixtures?id=1` → **HTTP 403** | Requires a key; legacy provider, not approved for historical use. |
| TheSportsDB | `eventsseason.php` → **HTTP 403** | Access denied. |
| Understat | HTTP 200 | Page carries none of its expected data objects; no xG extractable. |
| `soccerdata` / fbref | module absent; fbref **403** | Non-functional in this environment. |
| football-data.co.uk "new leagues" | `new/ARG.csv` → HTTP 200 | Results only (Country, League, Season, Date, Home, Away, HG, AG); no statistics. |
| OpenLigaDB | HTTP 200, 306 matches | Results only. |

## 5. Additional recovery methods identified

1. **Team alias table** — the single highest-value fix for UCL/Europa matching: `psv`↔`psveindenhoven`, `unionsg`↔`unionstgilloise`, `slbenfica`↔`benfica`, `qarabagagdam`↔`fkqarabag`, `dortmund`↔`borussiadortmund`. Evidence: these appear as real neighbours in the same ESPN fixture lists.
2. **Competition-slug discovery** — find ESPN's qualifying-round slug so preliminary/qualifying fixtures stop returning empty scoreboards.
3. **Stored-date integrity review** — fixtures like `2019-08-07 Juventus vs Lyon` are placed on dates where the fixture did not occur. These must be re-examined at source rather than matched to whatever ESPN lists that day.
4. **Score-based confirmation** — accept a match only when date, both teams and the final score agree; this prevents alias-driven false positives.
5. **StatsBomb 2018/19 UCL** — a measured match-count pass could supply real xG for that single season, which is the only xG route identified.

## 6. Recovery assessment by competition and season

| Competition | Seasons missing | Fixtures | Best available route | Assessment |
|---|---|---|---|---|
| World Cup (1) | 2022 | 128 | ESPN soccer | **Recoverable** — 100% measured match rate |
| Champions League (2) | 2019 | 130 | ESPN soccer + StatsBomb (xG) | Partially recoverable — alias table and date-integrity review required; qualifying rounds need the qualifying slug |
| Champions League (2) | 2022 | 214 | ESPN soccer | Blocked by qualifying-round coverage; sampled windows are qualifying ties |
| Champions League (2) | 2024 | 279 | ESPN soccer | Same as 2022: sampled failures are qualifying rounds |
| Champions League (2) | 2025 | 189 | ESPN soccer | Partially recoverable with aliases (PSV, Union SG, Benfica confirmed present) |
| Champions League (2) | 2026 | 54 | ESPN soccer | Partially recoverable; several stored dates look wrong |
| Europa League (3) | 2022 | 175 | ESPN soccer | Partially recoverable (43% measured) |
| Europa League (3) | 2023 | 175 | ESPN soccer | Partially recoverable (52% measured) |
| Europa League (3) | 2024 | 269 | ESPN soccer | Partially recoverable (21% measured) |
| Euro (4) | 2020 | 51 | ESPN soccer | Partially recoverable (52% measured) |
| Euro (4) | 2024 | 51 | ESPN soccer | **Recoverable** — 100% measured |
| Nations League (5) | 2020 | 162 | ESPN soccer | **Recoverable** — 82% measured |
| Nations League (5) | 2024 | 188 | ESPN soccer | **Recoverable** — 93% measured |
| Domestic 39/61/78/88/94/135/140 | 38 league-seasons | 2,203 | football_data_csv | Recoverable only where a CSV row exists; 186 rows already proven unmatched (38 + 40 + 108) |

## 7. Gaps that remain unrecoverable

- **xG**: only StatsBomb offers it, and only through 2018/19 for the Champions League — not for Euro, Nations League, Europa League or any later UCL season.
- **Player-level football statistics** for competitions 1–5: ESPN soccer returns none and no approved source covers them.
- **Possession for already-enriched domestic fixtures**: the CSV provider does not publish it.
- **Champions League 2022/2024 qualifying rounds**: provider slug unverified, so those fixtures remain unmatched rather than guessed.
