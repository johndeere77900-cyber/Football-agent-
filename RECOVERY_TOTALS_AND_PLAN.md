# Recovery Totals and Ranked Plan (Expansion Cycle)

Scope: additional recovery routes for missing historical fixtures and games.
No production writes were made in this cycle; every probe was read-only.

## 1. New sources and methods discovered

| # | Route | Classification |
|---|---|---|
| 1 | ESPN **qualifying-round slugs** (`uefa.champions_qual`, `uefa.europa_qual`) | **Verified and recommended** |
| 2 | **Team alias canon** map (PSV, Union SG, Benfica, Qarabag, Dortmund, Inter, Sporting) | **Verified and recommended** (method, not a source) |
| 3 | **Home/away swap + score-confirmation gating** | **Verified and recommended** (method) |
| 4 | basketball-reference season schedule page | Partially usable (54 of ~1,230 games parseable) |
| 5 | balldontlie v1 API | Rejected this cycle (HTTP 401, key required) |
| 6 | ESPN NBA scoreboard re-check | **Verified**: no missing games |

## 2. Exact endpoints investigated

Verified working:

- `https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.champions_qual/scoreboard?dates=YYYYMMDD`
- `https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.europa_qual/scoreboard?dates=YYYYMMDD`
- `https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.champions/scoreboard?dates=YYYYMMDD` (main competition)
- `https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=YYYYMMDD`
- `https://www.basketball-reference.com/leagues/NBA_2024_games.html` (partial)

Rejected this cycle:

- `https://api.balldontlie.io/v1/games` → HTTP **401** Unauthorized
- `https://www.rsssf.org/tablesc/champ2024.html` → HTTP **404**
- `https://eu-football.info/_matches.php?id=17000` → HTTP 200, **empty body**
- `https://www.11v11.com/competitions/uefa-champions-league/2024/` → HTTP **403** (Cloudflare)
- `https://wildstat.com/` → HTTP **403** (Cloudflare)
- `https://raw.githubusercontent.com/openfootball/champions-league/master/2019-20/champions-league.txt` → HTTP **404** (path does not exist)
- Conference League slug guesses `uefa.europa.conf`, `uefa.conference`, `uefa.europa_conf`, `uefa.conference_qual` → HTTP 200 but **0 events**; inconclusive, no Conference League fixtures are in our dataset

## 3. Evidence of returned historical fixtures

- `uefa.champions_qual` on **2024-07-09** returned **6 events** where the main `uefa.champions` slug returned **0**. These are exactly the qualifying ties our dataset holds (Panevezys–HJK, Hamrun–Lincoln Red Imps, Santa Coloma–Ballkani).
- `uefa.champions_qual` on **2022-06-21** returned **2 events**, matching our La Fiorita–Inter Escaldes and Levadia–Vikingur records.
- `uefa.europa_qual` on **2024-07-11** returned **6 events**.
- ESPN NBA scoreboard on each sampled date returned the full slate (3–13 games), all of which we already hold.

## 4. Match and reconciliation rates (football)

192 sampled fixtures, both slugs, ±1 day window, alias canon, home/away both orientations, score confirmation:

| League | Season | Probed | Matched | Score-confirmed | Rate |
|---|---|---|---|---|---|
| Champions League | 2019 | 24 | 9 | 9 | 38% |
| Champions League | 2022 | 24 | 21 | 21 | 88% |
| Champions League | 2024 | 24 | 23 | 23 | 96% |
| Champions League | 2025 | 24 | 18 | 18 | 75% |
| Champions League | 2026 | 24 | 17 | 17 | 71% |
| Europa League | 2022 | 24 | 19 | 19 | 79% |
| Europa League | 2023 | 24 | 19 | 19 | 79% |
| Europa League | 2024 | 24 | 23 | 23 | 96% |
| **Total** | | **192** | **149** | **149** | **78%** |

Compared with the previous cycle (main slug only, exact date, 0.86 threshold): overall 47%, with Champions League at 0–17%. **The qualifying slug plus aliases is the difference.**

Conflicts: **0**. No sampled fixture produced contradictory score evidence. Uncertain matches stay unresolved by construction, because a fixture is accepted only when both teams and the score agree.

## 5. Football: competition-by-competition recoverability

| Competition | Seasons missing | Missing fixtures | Measured rate | Est. recoverable |
|---|---|---|---|---|
| FIFA World Cup | 2022 | 128 | 100% | 128 |
| Champions League | 2019–2026 | 866 | 38–96% | ~683 |
| Europa League | 2022–2024 | 619 | 79–96% | ~534 |
| UEFA Euro | 2020, 2024 | 102 | 52–100% | ~78 |
| Nations League | 2020, 2024 | 350 | 82–93% | ~308 |
| **International total** | | **2,065** | **78%** | **~1,731** |
| Domestic (7 divisions) | 38 league-seasons | 2,203 | CSV-only | 482 already applied; remainder unmatched |

Separate football total: **2,065 international fixtures, ~1,731 recoverable (84%)**; domestic recovery remains limited to fixtures that have a real CSV counterpart.

## 6. Basketball: season-by-season coverage

| Season | Games held | Enriched | Outstanding | Missing games found |
|---|---|---|---|---|
| 2019 | 1,218 | 847 | 371 | 0 |
| 2020 | 1,226 | 0 | 1,226 | 0 |
| 2021 | 1,390 | 0 | 1,390 | 0 |
| 2022 | 1,389 | 0 | 1,389 | 0 |
| 2023 | 1,393 | 0 | 1,393 | 0 |
| 2024 | 1,397 | 0 | 1,397 | 0 |
| 2025 | 1,397 | 0 | 1,397 | 0 |
| 2026 | 174 | 0 | 174 | 0 |
| **Total** | **9,584** | **847** | **8,737** | **0** |

Separate basketball total: **0 missing games** (81 of 81 sampled games across ten dates matched, 100%), and **8,737 games needing statistics**, all recoverable through the existing ESPN summary route.

An earlier measurement in this cycle reported 60 "missing" games at 25.9%. That was my own error: `game_date` is stored in UTC while ESPN lists local dates, so evening games shifted a day. With a ±1 day window the result is 100% matched.

## 7. Duplicate and conflict findings

- No duplicate fixtures were created by any probe (read-only).
- No score conflicts among 149 confirmed football matches.
- Basketball: no conflicting game identities; the only apparent gaps were timezone artefacts.
- basketball-reference ids (54 parsed) all correspond to games we already hold; nothing new recovered there.

## 8. Sources verified and recommended

1. **ESPN qualifying slugs** — recommended for acquisition; converts 0–17% to 71–96% on UCL and 79–96% on Europa League.
2. **Alias canon + swap + score gating** — recommended as the mandatory matching method for all ESPN football batches.
3. **ESPN NBA summary** — recommended; no missing games, statistics coverage proven on 847 games.

## 9. Remaining records that cannot currently be recovered

- **Champions League 2019 qualifying**: only 38% matched. That date (2019-08-07) in our data contains fixtures that did not occur then (Juventus–Lyon, Man City–Real Madrid), so those records need a source-level date audit before they can ever match.
- **~334 international fixtures** that fail the score-confirmed match test even with aliases and both slugs.
- **Domestic fixtures without a CSV counterpart** (186 already proven unmatched; the rest needs re-acquisition, not enrichment).
- **xG** for all competitions (StatsBomb only to UCL 2018/19, unapproved).
- **Player-level football statistics** for competitions 1–5 (ESPN soccer returns none).

## 10. Ranked recovery plan (by expected records recovered)

1. **ESPN football batch with qualifying slugs + aliases** (~1,731 international fixtures): highest yield, measured, no licence question. Requires your authorisation to write.
2. **Basketball statistics batches** (8,737 games): capability already implemented and proven on 847.
3. **Date-integrity audit** of Champions League 2019 records: unlocks the 38% remainder and prevents false matches.
4. **StatsBomb 2018/19 UCL xG**: needs coverage measurement and licence decision; small volume, unique xG value.
5. **Domestic CSV re-acquisition**: only after confirming which fixtures legitimately have no CSV row.
6. **basketball-reference**: spot verification only; schedule page serves 54 games, not a full season.
