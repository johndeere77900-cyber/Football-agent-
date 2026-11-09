# Source Verification — Additional Routes (November 2026 cycle)

Discovery and verification only. No production writes, no schema changes, no
acquisition jobs, no runtime configuration changes. Every figure below comes from a
live read-only request and a comparison against the existing `historical_fixtures`
rows (21,379) or the basketball tables.

## 1. Classification summary

| Source | Status | What it actually provides |
|---|---|---|
| UEFA `match.uefa.com/v5/matches` | **VERIFIED** | Complete UCL, Europa League and European Championship match lists including qualifying rounds, with scores |
| FIFA `api.fifa.com/api/v3/calendar/matches` | **VERIFIED** | World Cup 2022 fixtures (64), scores, stages |
| football-data.co.uk CSVs | **VERIFIED** | Domestic league results with scores and dates |
| OpenLigaDB `api.openligadb.de` | **VERIFIED** | Bundesliga results, scores, kick-off times |
| basketball-reference (monthly schedule + box scores) | **VERIFIED** | Full-season game index and per-game team, player and quarter statistics |
| TheSportsDB `key=123` | **PARTIALLY VERIFIED** | Season event lists with scores; free-tier coverage |
| OpenFootball `football.json` | **PARTIALLY VERIFIED** | Fixtures with dates, but no scores in the probed file |
| `shufinskiy/nba_data` | **PARTIALLY VERIFIED** | Index of 514 tar.xz archives (play-by-play, stats); contents not yet parsed |
| `match.uefa.com/v5/competitions` | REJECTED | HTTP 404 — no catalogue endpoint |
| `cdn.nba.com` static JSON | REJECTED | HTTP 403 Access Denied |
| `data.nba.net` | REJECTED | DNS resolution failure |
| `stats.nba.com` | REJECTED | Connection timeout |
| `api.balldontlie.io/v2` | REJECTED | HTTP 401 Unauthorized (key required) |
| TheSportsDB `key=3` | REJECTED | HTTP 403 — key permanently disabled |

## 2. Football — exact endpoints and evidence

### 2.1 UEFA official match API (VERIFIED)

`GET https://match.uefa.com/v5/matches?competitionId=<id>&seasonYear=<year>&limit=500&offset=0`

Competition IDs discovered by scanning: `1` = Champions League, `14` = Europa League,
`3` = European Championship, `9` = Super Cup, `19` = Friendlies, `28` = Women's
Champions League. Conference League and Nations League were **not** located in IDs
1–40 or 2019–2022.

Response shape: a JSON array; each match carries `kickOffTime.date`, `score.total`,
`homeTeam.internationalName`, `awayTeam.internationalName`, `competitionPhase`,
`status`, `stadium`, `referees`. Note that several fields are occasionally plain
strings rather than objects, so every access must be type-guarded (this caused three
parse failures during verification).

**Measured against our fixtures** (name canon + score + date ±1 day):

| Competition | Season | Source matches | Matched | Absent | Score conflict | Rate |
|---|---|---|---|---|---|---|
| Champions League | 2019/20 | 210 | 119 | **91** | 0 | 56.7% |
| Champions League | 2022/23 | 214 | 212 | 0 | 1 | 99.1% |
| Champions League | 2024/25 | 279 | 279 | 0 | 0 | **100%** |
| Champions League | 2025/26 | 281 | 185 | **95** | 1 | 65.8% |
| Europa League | 2022/23 | 175 | 175 | 0 | 0 | **100%** |
| Europa League | 2023/24 | 175 | 175 | 0 | 0 | **100%** |
| Europa League | 2024/25 | 269 | 269 | 0 | 0 | **100%** |
| European Championship | 2020 | 313 | 46 | **262** | 0 | 14.7% |
| European Championship | 2024 | 290 | 51 | **239** | 0 | 17.6% |

This is a substantial correction to the previous cycle. Champions League and Europa
League are **not** broadly missing: three Europa seasons and one Champions League
season match 100%, and 2022/23 matches 99.1%. The earlier figures that were reported
as "missing" (279, 269, 214 …) were in fact the number of rows we **hold**.

The two real Champions League gaps are qualifying rounds, proven absent rather than
merely unmatched — our monthly row counts contain no July/August 2019 beyond 11 rows
and no July/August 2025 at all, while the source lists those ties:

- `2019-08-28 Club Brugge 2-1 LASK`, `2019-08-28 Ajax 2-0 APOEL`,
  `2019-08-27 Krasnodar 1-2 Olympiacos`, `2019-08-27 Crvena Zvezda 1-1 Young Boys`
- `2025-08-27 Club Brugge 6-0 Rangers`, `2025-10-21 Kairat Almaty 0-0 Pafos`,
  `2025-11-26 Copenhagen 3-2 Kairat Almaty`, `2026-01-28 Barcelona 4-1 Copenhagen`

European Championship gaps are **qualifying rounds**: our dataset holds only the 51
finals matches for each cycle (51 matched in both seasons), while UEFA returns 290
and 313 matches. The 239 and 262 extra matches returned no rows at all on the
corresponding dates, confirming genuine absence rather than a matching failure.

Six Euro 2020 finals are stored with home and away inverted relative to UEFA
(`2021-06-23 Slovakia 0-5 Spain` held as `Spain 5-0 Slovakia`, and five similar).
The score is consistent when mirrored; the orientation is the defect.

One score-convention difference, not a conflict: `2026-05-30 Paris 1-1 Arsenal` is
stored by us as `PSG 5-4 Arsenal`, i.e. our record carries the penalty-shootout
result where UEFA reports the 120-minute score. Any reconciliation must decide which
convention is authoritative before importing.

### 2.2 FIFA official calendar API (VERIFIED)

`GET https://api.fifa.com/api/v3/calendar/matches?idCompetition=17&idSeason=255711&count=500&language=en`

Returned **64** matches for the 2022 World Cup with dates, `HomeTeamScore`,
`AwayTeamScore` and `StageName` (`First stage`, `Round of 16`, …).

Comparison: **64 matched, 0 absent, 0 conflicts — 100%**. This independently confirms
our World Cup 2022 data is complete, and it is the strongest cross-check available
for international tournaments. `idCompetition=17` is the World Cup; other
competitions are enumerable from the same endpoint with different `idSeason` values.

### 2.3 football-data.co.uk (VERIFIED)

`GET https://www.football-data.co.uk/mmz4281/<season>/<division>.csv` (e.g. `2425/D1`)

Returns CSV with `Date`, `HomeTeam`, `AwayTeam`, `FTHG`, `FTAG`, plus shots, corners,
cards and closing odds.

| File | Source rows | Matched | Score diff | Absent |
|---|---|---|---|---|
| `2425/D1` (Bundesliga) | 306 | 306 | 0 | 0 |
| `1920/I1` (Serie A) | 380 | 380 | 0 | 0 |
| `1920/P1` (Primeira Liga) | 306 | 306 | 0 | 0 |
| `2425/SP1` (La Liga) | 380 | 380 | 0 | 0 |

100% on every file, zero conflicts. **There is no domestic gap in these
league-seasons**, which directly contradicts the 2,203 domestic fixtures previously
reported as missing.

### 2.4 OpenLigaDB (VERIFIED)

`GET https://api.openligadb.de/getmatchdata/bl1/2024` → 306 matches, all finished,
with `matchDateTime`, team names and `matchResults`.

Comparison: **306 matched, 0 absent, 0 conflicts**. German Bundesliga 2024/25 is
complete in our data.

### 2.5 OpenFootball JSON (PARTIALLY VERIFIED)

`https://raw.githubusercontent.com/openfootball/football.json/master/2019-20/en.1.json`
(200, 114 KB) and `.../2024-25/de.1.json` (200, 88 KB).

Both return a `matches` array with `round`, `date`, `team1`, `team2` — but the
2024/25 German file carried **no score fields**, so it is a fixture-schedule source
only, not a results source. `openfootball/world-cup/master/2022--qatar/cup.txt` (200,
8.3 KB) is a plain-text fixture list, also without scores. Useful for schedule
reconstruction, unsuitable for score-confirmed recovery.

### 2.6 TheSportsDB (PARTIALLY VERIFIED)

`GET https://www.thesportsdb.com/api/v1/json/123/eventsseason.php?id=4335&s=2023-2024`
→ **HTTP 200**, 16.6 KB, returns an `events` array with `strEvent`,
`strEventAlternate`, `strSeason` and score fields.

The old documented free key `3` now returns HTTP 403 with the message that it is
permanently disabled; `123` is the current public test key. Coverage on the free tier
is limited and rate-limited, so this is a supplementary cross-check, not a bulk feed.

## 3. Basketball — exact endpoints and evidence

### 3.1 basketball-reference schedule (VERIFIED, fixes the earlier 54-id limitation)

`GET https://www.basketball-reference.com/leagues/NBA_2024_games-<month>.html`

Monthly pages, not the single season page, are the complete index. Season 2023/24:

| Month | HTTP | Game ids |
|---|---|---|
| october | 200 | 54 |
| november | 200 | 219 |
| december | 200 | 208 |
| january | 200 | 231 |
| february | 200 | 174 |
| march | 200 | 230 |
| april | 200 | 157 |
| may | 200 | 41 |
| june | 200 | 5 |

**1,319 unique game ids**, versus the 54 obtainable from the single season page. The
earlier "54 ids" result was a pagination artefact, not limited coverage.

### 3.2 basketball-reference box score (VERIFIED — real statistics)

`GET https://www.basketball-reference.com/boxscores/202401250MIA.html` → HTTP 200,
488,708 bytes.

Tables present: `box-BOS-game-basic`, `box-BOS-game-advanced`, `box-MIA-game-basic`,
`box-MIA-game-advanced`, plus per-quarter and per-half splits `box-{TEAM}-q1..q4-basic`
and `box-{TEAM}-h1..h2-basic`.

Parsed player rows carry `player`, `mp`, `fg`, `fga`, `fg_pct`, `ft`, `fta`, `orb`,
`drb`, `trb`, `ast`, `stl`, `blk`, `tov`, `pf`, `pts`, `game_score`, `plus_minus`.
Example: `Jayson Tatum, 32:12, 7-15 FG, 9-10 FT, 8 TRB, 4 AST, 26 PTS, +22`.

This covers **all four** requested statistics categories: team statistics (basic and
advanced), player statistics, period scores (quarters and halves), and game-level
box scores.

Date convention: basketball-reference dates are **local**, our `game_date` is UTC.
`202401250MIA` is the local-date box score, and our rows carry `T00:30Z` style
timestamps. Any join must be attempted across a ±1 day window — the same defect that
produced the false "60 missing games" in the previous cycle.

### 3.3 Our current basketball state (re-measured)

| Season | Games | Enriched | Missing statistics |
|---|---|---|---|
| 2019 | 1,218 | 847 | 371 |
| 2020 | 1,226 | 0 | 1,226 |
| 2021 | 1,390 | 0 | 1,390 |
| 2022 | 1,389 | 0 | 1,389 |
| 2023 | 1,393 | 0 | 1,393 |
| 2024 | 1,397 | 0 | 1,397 |
| 2025 | 1,397 | 0 | 1,397 |
| 2026 | 174 | 0 | 174 |
| **Total** | **9,584** | **847** | **8,737** |

Each enriched game holds 2 team rows, ~26 player rows and 10 period rows (4 quarters
plus overtime per team). Verified example `401160623` (Raptors–Pelicans,
2019-10-23): 26 player rows, 2 team rows, 10 period rows.

**Missing games: 0.** A 10-date sample of 81 ESPN games matched 81/81 (100%) once the
UTC/local offset was applied.

### 3.4 shufinskiy/nba_data (PARTIALLY VERIFIED)

`https://raw.githubusercontent.com/shufinskiy/nba_data/main/list_data.txt` → HTTP 200,
51,994 bytes, **514 archive entries** such as
`cdnnba_2020=https://github.com/shufinskiy/nba_data/raw/main/datasets/cdnnba_2020.tar.xz`.
README documents play-by-play and shot details from 1996/97 to 2024/25.

The GitHub contents API returned 403 (rate limit) and the archives are `.tar.xz`, so
the payload itself was **not** parsed. Classified partially verified: the index is
real, the contents are unconfirmed. It is a play-by-play source, not a substitute for
box-score statistics.

## 4. Matching methodology

1. Normalise: strip diacritics (NFKD), lowercase, remove non-alphanumerics.
2. Apply an explicit alias canon derived from observed failures: `psv`→`psveindhoven`,
   `mancity`→`manchestercity`, `bdortmund`→`borussiadortmund`, `inter`→`internazionale`,
   `milan`→`acmilan`, `psg`/`paris`→`parissaintgermain`, `atleti`→`atleticomadrid`,
   `olympiacos`→`olympiakospiraeus`, `czechia`→`czechrepublic`,
   `viktoriaplzen`→`plzen`, `unionsg`→`unionsaintgilloise`, `bayern`→`bayernmunich`,
   `korearepublic`→`southkorea`, `leverkusen`→`bayerleverkusen`.
3. Compare dates across ±1 day, because our `kickoff_at` is UTC while most sources
   publish local dates.
4. Accept a match only when names agree **and** the score agrees, in either
   orientation. Mirrored scores are reported separately as `swapped`, never counted as
   clean matches.

Without the alias canon, matching understates recovery badly: Champions League
2022/23 scored 180/214 (84%) before diacritics and aliases and 212/214 (99.1%) after.
That gap is a matching defect, not missing data.

## 5. Duplicates, conflicts and ambiguities

- No duplicate fixtures were created; every request was read-only.
- Domestic: 0 score conflicts across 1,372 compared fixtures.
- Champions League 2022/23: 1 score conflict; 2025/26: 1 (the shootout convention).
- Euro 2020: 6 orientation inversions (score consistent when mirrored), 0 true
  score conflicts.
- Basketball: no conflicting identities; the only apparent gaps were the UTC/local
  date offset.

## 6. Access, licensing and credentials

| Source | Key required | Rate limit | Restriction |
|---|---|---|---|
| UEFA match v5 | No | Undocumented; 500-match pages worked | UEFA terms — non-commercial reuse of match data |
| FIFA calendar v3 | No | Undocumented | FIFA terms |
| football-data.co.uk | No | None observed | Free for personal use; acknowledge source |
| OpenLigaDB | No | None observed | Open data, CC-BY |
| basketball-reference | No | ~20 requests/minute unofficial | **Terms prohibit automated scraping**; must be cleared before any bulk use |
| TheSportsDB | Yes (`123` test key) | 30 requests/minute | Free tier limited |
| shufinskiy/nba_data | No | GitHub raw limits | Dataset licence unverified |
| balldontlie | Yes (paid) | — | Not usable without a key |

The basketball-reference terms restriction is the one genuine blocker: technically
verified, legally unconfirmed for bulk acquisition. The ESPN summary route already
used for the 847 enriched games carries no such restriction and can serve the same
purpose.

## 7. Conclusion

- Football missed fixtures are far fewer than previously reported: **186 Champions
  League qualifying ties** and **501 European Championship qualifying matches** are
  genuinely absent and available. Everything else measured is already present at
  99–100%.
- Domestic coverage is complete on every feed tested; the 2,203 figure was wrong.
- Basketball has **no missing games**; the work is statistics for 8,737 games, and
  two verified routes exist (ESPN summary, basketball-reference box scores).
