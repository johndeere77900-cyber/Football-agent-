"""One evidence-backed outcome for every fixture and statistic field.

For each fixture and each tracked field (possession, xG, shots, shots on target,
corners, yellow cards, red cards) the ledger records either that a paired value is
stored, or the precise reason it is not. Reasons are derived from evidence rather
than assumed:

- ``provider_does_not_supply_field``: the cached provider payload for the matched
  fixture has a statistics block but no key for this field.
- ``provider_no_statistics_block``: the matched fixture has no statistics at all.
- ``no_provider_match``: no candidate in the provider's day listings matched on
  competition, clubs, date and identical score.
- ``ambiguous_match_rejected``: several candidates fitted equally well and the
  matcher refused to guess.
- ``request_failed``: the provider's match detail was requested and failed.
- ``not_attempted``: no acquisition attempt covers this fixture yet.

Output: one JSONL line per fixture/field plus a summary JSON, both under
``.api_cache/reports``. Counts are per distinct fixture, never per enrichment row.
"""

import collections
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import storage
import espn_football_stats as espn
import fotmob_enrichment as fm

BASE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(BASE, ".api_cache", "reports")
MATCH_CACHE = os.path.join(BASE, ".api_cache", "fotmob", "match")
DAY_CACHE = os.path.join(BASE, ".api_cache", "fotmob", "day")
OUT = os.path.join(REPORTS, "fotmob_field_ledger.jsonl")
SUMMARY = os.path.join(REPORTS, "fotmob_field_ledger_summary.json")

FIELDS = {
    "possession": ("home_possession", "away_possession", ("ballpossesion",)),
    "xg": ("home_xg", "away_xg", ("expected_goals",)),
    "shots": ("home_shots", "away_shots", ("total_shots",)),
    "shots_on_target": ("home_shots_on_target", "away_shots_on_target", ("shotsontarget",)),
    "corners": ("home_corners", "away_corners", ("corners",)),
    "yellow_cards": ("home_yellow_cards", "away_yellow_cards", ("yellow_cards",)),
    "red_cards": ("home_red_cards", "away_red_cards", ("red_cards",)),
}


def stat_keys(match_id):
    """Statistic keys present in the cached payload, or None when unavailable."""
    path = os.path.join(MATCH_CACHE, f"{match_id}.json")
    if not os.path.exists(path):
        return "missing"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return "missing"
    periods = ((payload.get("content") or {}).get("stats") or {}).get("Periods") or {}
    groups = ((periods.get("All") or {}).get("stats")) or []
    if not groups:
        return "no_block"
    return {str((item.get("key") or "")).lower()
            for group in groups for item in (group.get("stats") or [])}


def local_match_state(fixture):
    """Classify a fixture with no matched id using the cached day listings only."""
    day = datetime.date.fromisoformat(fixture["day"])
    aliases = fm.LEAGUE_ALIASES.get(fixture["league_id"], ())
    strong, weak = [], []
    for delta in (0, 1, -1):
        probe = (day + datetime.timedelta(days=delta)).isoformat().replace("-", "")
        path = os.path.join(DAY_CACHE, f"{probe}.json")
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            continue
        for league in payload.get("leagues") or []:
            name = (league.get("name") or "").lower()
            if aliases and not any(alias in name for alias in aliases):
                continue
            for match in league.get("matches") or []:
                home = (match.get("home") or {}).get("name")
                away = (match.get("away") or {}).get("name")
                hs = (match.get("home") or {}).get("score")
                aws = (match.get("away") or {}).get("score")
                try:
                    if (int(fixture["home_goals"]), int(fixture["away_goals"])) != (int(hs), int(aws)):
                        continue
                except (TypeError, ValueError):
                    continue
                sim = min(fm.name_ratio(fixture["home"], home),
                          fm.name_ratio(fixture["away"], away))
                if fm.candidate_score({"home_team": fixture["home"], "away_team": fixture["away"],
                                       "home_goals": fixture["home_goals"],
                                       "away_goals": fixture["away_goals"],
                                       "day": fixture["day"]},
                                      {"home": home, "away": away,
                                       "home_score": hs, "away_score": aws,
                                       "utc": (match.get("status") or {}).get("utcTime"),
                                       "league": name}) < 2:
                    strong.append(sim)
                else:
                    weak.append(sim)
    if strong:
        return "would_match_now"
    if len(weak) == 1 and weak[0] >= 0.55:
        return "would_match_now"
    if len(weak) > 1:
        return "ambiguous_match_rejected"
    return "no_provider_match"


def main():
    outcomes = {}
    path = os.path.join(REPORTS, "fotmob_outcomes.jsonl")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                outcomes[str(row["fixture_id"])] = row

    conn, _ = storage._connect()
    cur = conn.cursor()
    cur.execute("""
        select f.fixture_id, f.league_id, f.status_short,
               to_char(f.kickoff_at::timestamptz at time zone 'utc','YYYY-MM-DD') as d,
               f.home_team, f.away_team, f.home_goals, f.away_goals,
               e.source, e.home_possession, e.away_possession, e.home_xg, e.away_xg,
               e.home_shots, e.away_shots, e.home_shots_on_target, e.away_shots_on_target,
               e.home_corners, e.away_corners, e.home_yellow_cards, e.away_yellow_cards,
               e.home_red_cards, e.away_red_cards,
               (e.raw_json->>'fotmob_match_id') as fotmob_match_id
          from historical_fixtures f
          left join historical_fixture_enrichment e on e.fixture_id = f.fixture_id
         order by f.fixture_id
    """)
    columns = [d[0] for d in cur.description]
    per_fixture = collections.OrderedDict()
    for row in cur.fetchall():
        record = dict(zip(columns, row))
        fid = record["fixture_id"]
        entry = per_fixture.get(fid)
        if entry is None:
            entry = {"fixture_id": fid, "league_id": record["league_id"],
                     "status": record["status_short"], "day": record["d"],
                     "home": record["home_team"], "away": record["away_team"],
                     "home_goals": record["home_goals"], "away_goals": record["away_goals"],
                     "has_row": False, "match_id": None, "paired": {}}
            per_fixture[fid] = entry
        if record["source"] is None:
            continue
        entry["has_row"] = True
        if record["fotmob_match_id"]:
            entry["match_id"] = record["fotmob_match_id"]
        for field, (home_col, away_col, _keys) in FIELDS.items():
            if record[home_col] is not None and record[away_col] is not None:
                entry["paired"][field] = True

    summary = collections.Counter()
    per_league = collections.Counter()
    match_state_cache = {}
    lines = 0
    with open(OUT, "w", encoding="utf-8") as handle:
        for fid, entry in per_fixture.items():
            keys = None
            if entry["match_id"]:
                keys = stat_keys(entry["match_id"])
            outcome = outcomes.get(str(fid), {})
            for field, (_h, _a, field_keys) in FIELDS.items():
                if entry["paired"].get(field):
                    reason = "acquired_verified"
                elif not entry["has_row"]:
                    state = outcome.get("outcome")
                    if state == "matched_no_statistics_block":
                        reason = "provider_no_statistics_block"
                    elif state == "matched_with_statistics":
                        reason = "provider_value_missing_in_payload"
                    elif entry["status"] == "Scheduled":
                        reason = "not_played"
                    else:
                        if fid not in match_state_cache:
                            match_state_cache[fid] = local_match_state(entry)
                        local = match_state_cache[fid]
                        if local == "ambiguous_match_rejected":
                            reason = "ambiguous_match_rejected"
                        elif local == "would_match_now":
                            reason = "not_attempted"
                        else:
                            reason = "no_provider_match"
                elif field == "xg" and entry["day"] < "2020-01-01":
                    # Skipped by design: the provider has no xG before the 2020/21
                    # season, so possession-only fixtures from those years are terminal.
                    reason = "provider_predates_xg_era"
                elif entry["match_id"] is None:
                    # A row exists but no provider match was ever attached, so the gap
                    # is a matching outcome, not a field the provider withheld.
                    if fid not in match_state_cache:
                        match_state_cache[fid] = local_match_state(entry)
                    local = match_state_cache[fid]
                    if local == "ambiguous_match_rejected":
                        reason = "ambiguous_match_rejected"
                    elif local == "would_match_now":
                        reason = "not_attempted"
                    else:
                        reason = "no_provider_match"
                elif keys == "no_block":
                    reason = "provider_no_statistics_block"
                elif keys == "missing":
                    reason = "request_failed"
                elif any(key in keys for key in field_keys):
                    reason = "provider_value_missing_in_payload"
                elif field == "xg" and entry["day"] < "2020-01-01":
                    reason = "provider_predates_xg_era"
                elif field in ("yellow_cards", "red_cards"):
                    reason = "provider_no_discipline_block"
                else:
                    reason = "provider_does_not_supply_field"
                summary[(field, reason)] += 1
                per_league[(entry["league_id"], field, reason)] += 1
                handle.write(json.dumps({
                    "fixture_id": fid, "field": field, "outcome": reason,
                    "league_id": entry["league_id"], "status": entry["status"],
                    "day": entry["day"], "match_id": entry["match_id"],
                    "ambiguous_name_match": bool(outcome.get("weak_name_match")),
                }) + "\n")
                lines += 1

    acquired = collections.Counter()
    for entry in per_fixture.values():
        for field in entry["paired"]:
            acquired[field] += 1

    payload = {
        "fixtures": len(per_fixture),
        "ledger_rows": lines,
        "acquired_paired_by_field": dict(acquired),
        "outcomes_by_field": {field: {reason: n for (f, reason), n in summary.items() if f == field}
                              for field in FIELDS},
        "gaps_by_league": {str(k[0]): {f"{k[1]}:{k[2]}": v for kk, v in per_league.items()
                                       if kk[0] == k[0]} for k in per_league},
    }
    with open(SUMMARY, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1)
    print(json.dumps({"fixtures": len(per_fixture), "ledger_rows": lines,
                      "acquired": dict(acquired)}, indent=1))
    for field in FIELDS:
        print(field, json.dumps({r: n for (f, r), n in summary.items() if f == field}))


if __name__ == "__main__":
    main()
