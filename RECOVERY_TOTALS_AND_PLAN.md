# Recovery Totals and Ranked Plan

Status: **corrected** after the November 2026 verification cycle. Figures previously
in this file (2,065 international fixtures "missing", 4,268 total) were wrong: those
numbers were counts of rows we **hold**, not gaps. Everything below is measured
against the live source or explicitly labelled as an estimate.

Evidence: [SOURCE_VERIFICATION_2026-11.md](SOURCE_VERIFICATION_2026-11.md).
No production writes were made.

## 1. Football — measured inventory

### 1.1 Competitions verified complete (nothing to recover)

| Competition | Season | Source | Matched | Result |
|---|---|---|---|---|
| Champions League | 2022/23 | 214 | 212 | 99.1% (1 conflict) |
| Champions League | 2024/25 | 279 | 279 | 100% |
| Europa League | 2022/23 | 175 | 175 | 100% |
| Europa League | 2023/24 | 175 | 175 | 100% |
| Europa League | 2024/25 | 269 | 269 | 100% |
| World Cup | 2022 | 64 | 64 | 100% (FIFA API) |
| Bundesliga | 2024/25 | 306 | 306 | 100% (two independent feeds) |
| Serie A | 2019/20 | 380 | 380 | 100% |
| Primeira Liga | 2019/20 | 306 | 306 | 100% |
| La Liga | 2024/25 | 380 | 380 | 100% |

### 1.2 Genuine gaps, verified absent and available

| Competition | Season | Missing | Why | Source |
|---|---|---|---|---|
| European Championship qualifying | 2024 cycle | 239 | Dataset stores finals only (51) | UEFA v5 `competitionId=3` |
| European Championship qualifying | 2020 cycle | 262 | Dataset stores finals only (51) | UEFA v5 `competitionId=3` |
| Champions League qualifying | 2019/20 | 91 | No July/August 2019 rows | UEFA v5 `competitionId=1` |
| Champions League qualifying | 2025/26 | 95 | No July/August 2025 rows | UEFA v5 `competitionId=1` |
| **Football total** | | **687** | | |

Domestic gap: **0** on every league-season tested. The earlier 2,203 domestic figure
is withdrawn.

### 1.3 Identified data-integrity defects (not missing records)

- **6 Euro 2020 finals with home/away inverted** (e.g. `Spain 5-0 Slovakia` held
  where UEFA has `Slovakia 0-5 Spain`).
- **1 shootout convention mismatch**: `2026-05-30 Paris 1-1 Arsenal` held as
  `PSG 5-4 Arsenal`. Our record appears to carry the penalty result; the source
  reports the 120-minute score. A convention decision is required before any import.
- **Alias gaps**: without a canon, Champions League 2022/23 appeared to be 84%
  matched instead of 99.1%. Matching defects masquerade as missing data.

### 1.4 Not measured this cycle

- **Nations League** (162 + 188 rows held): competition ID not found in UEFA v5, so
  completeness is unverified.
- **Conference League**: not present in UEFA v5 IDs 1–40 or 2019–2022; no route found
  and no fixtures held, so there is nothing to reconcile yet.
- Remaining domestic leagues beyond the four verified.

## 2. Basketball — measured inventory

| Category | Count | Status |
|---|---|---|
| Games held | 9,584 | Complete — 0 missing (81/81 sample) |
| Games with team statistics | 847 | 2 rows each |
| Games with player statistics | 847 | ~26 rows each |
| Games with period scores | 847 | 10 rows each (4 quarters + OT, both teams) |
| Games missing statistics | **8,737** | Recoverable |

Estimated volume if all 8,737 are completed (estimate, not measured): ~17,474 team
rows, ~227,000 player rows, ~87,370 period rows.

### 2.1 Verified basketball statistics sources

| Source | Team | Player | Period | Constraint |
|---|---|---|---|---|
| ESPN summary (current implementation) | yes | yes | yes | None; already proven on 847 games |
| basketball-reference box scores | yes (+ advanced) | yes | yes (quarters and halves) | **Terms prohibit automated scraping** — clearance required |
| shufinskiy/nba_data | play-by-play archives | indirect | via events | Contents unparsed; licence unverified |

## 3. Ranked recovery plan

1. **European Championship qualifying — 501 matches.** Largest measured pool, fully
   verified in the source and fully absent here. UEFA v5 `competitionId=3`,
   `seasonYear` 2020 and 2024.
2. **Champions League qualifying — 186 matches.** 2019/20 (91) and 2025/26 (95).
   UEFA v5 `competitionId=1`, same seasons.
3. **Basketball statistics — 8,737 games.** Use the existing ESPN summary route;
   basketball-reference is the fallback only if its terms are cleared.
4. **Nations League verification.** Find the competition ID, then measure; do not
   assume a gap exists.
5. **Conference League route.** Needs a different provider; nothing to reconcile until
   such fixtures exist.
6. **Data-integrity repairs** (6 inverted Euro fixtures, 1 shootout convention):
   small in volume, high in correctness value, and must precede any import that would
   otherwise duplicate or contradict them.

## 4. Superseded claims

| Previous claim | Corrected finding |
|---|---|
| 2,065 international fixtures missing | 687 genuinely absent |
| 2,203 domestic fixtures missing | 0 on tested feeds |
| Champions League/Europa broadly unmatched (47%) | 99–100% matched with alias canon |
| 60 basketball games missing (25.9%) | 0 missing; UTC/local date artefact |
| basketball-reference exposes only 54 game ids | 1,319 via monthly pages |
