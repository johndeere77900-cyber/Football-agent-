"""One evidence-backed outcome for every fixture and statistic field.

For each of the 25,111 fixtures and each of the seven tracked fields (possession, xG,
shots, shots on target, corners, yellow cards, red cards) this emits exactly one
outcome. Outcomes are derived from evidence, never assumed:

- ``acquired_verified``: a paired home/away value is stored.
- ``provider_does_not_supply_field``: the cached payload has a statistics block but no
  key for this field.
- ``provider_no_statistics_block``: the matched fixture has no statistics at all.
- ``provider_no_discipline_block``: payload has statistics but no cards group.
- ``provider_predates_xg_era``: the fixture belongs to a season before the 2020/21
  season, when the provider published no xG systematically.
- ``no_provider_match``: a cached day listing was read and contains no eligible match.
- ``ambiguous_match_rejected``: several candidates fitted equally well; refused.
- ``evidence_missing_request_not_attempted``: the day listing or match payload used as
  evidence is absent from the cache, so absence of a match is NOT established.
- ``request_failed``: the provider request was made and failed.
- ``not_played``: the fixture has not been played.

The durable handover artifacts are ``artifacts/fotmob_field_ledger.jsonl.gz`` (one line
per fixture/field) and ``artifacts/fotmob_field_ledger_summary.json`` (totals plus the
outcome definitions). Counts are per distinct fixture, never per enrichment row.
"""

import collections
import datetime
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import storage
import fotmob_enrichment as fm

BASE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(BASE, ".api_cache", "reports")
ARTIFACTS = os.path.join(BASE, "artifacts")
MATCH_CACHE = os.path.join(BASE, ".api_cache", "fotmob", "match")
DAY_CACHE = os.path.join(BASE, ".api_cache", "fotmob", "day")
OUT = os.path.join(ARTIFACTS, "fotmob_field_ledger.jsonl.gz")
SUMMARY = os.path.join(ARTIFACTS, "fotmob_field_ledger_summary.json")

COMPETITIONS = {
    1: "World Cup", 2: "Champions League", 3: "Europa League", 4: "European Championship",
    5: "Nations League", 39: "Premier League", 61: "Ligue 1", 78: "Bundesliga",
    88: "Eredivisie", 94: "Primeira Liga", 135: "Serie A", 140: "LaLiga",
}

FIELDS = {
    "possession": ("home_possession", "away_possession", ("ballpossesion",)),
    "xg": ("home_xg", "away_xg", ("expected_goals",)),
    "shots": ("home_shots", "away_shots", ("total_shots",)),
    "shots_on_target": ("home_shots_on_target", "away_shots_on_target", ("shotsontarget",)),
    "corners": ("home_corners", "away_corners", ("corners",)),
    "yellow_cards": ("home_yellow_cards", "away_yellow_cards", ("yellow_cards",)),
    "red_cards": ("home_red_cards", "away_red_cards", ("red_cards",)),
}

DEFINITIONS = {
    "acquired_verified": "paired home and away value persisted",
    "provider_does_not_supply_field": "cached payload has statistics but no key for this field",
    "provider_no_statistics_block": "matched provider fixture has no statistics block",
    "provider_no_discipline_block": "cached payload has statistics but no cards group",
    "provider_predates_xg_era": "season predates the provider's 2020/21 xG coverage",
    "no_provider_match": "a cached day listing was read and holds no eligible match",
    "ambiguous_match_rejected": "multiple provider matches fitted equally well; refused",
    "evidence_missing_request_not_attempted":
        "day listing or match payload absent from cache; absence of a match is not established",
    "request_failed": "provider request made and failed",
    "provider_value_missing_in_payload": "key present but no usable numeric value",
    "not_played": "fixture not played",
}


def predates_xg_era(season, day):
    """Whether a fixture precedes the provider's systematic xG coverage.

    Measured in production: the 2019 season has xG on 27 of 3,794 played fixtures (12 of
    1,059 played in January-June 2020), while the 2020 season has 3,416 of 3,878. The
    season start year is therefore the boundary, not the calendar year -- a fixture
    played in early 2020 still belongs to the 2019/20 season.
    """
    if season is not None:
        return int(season) < 2020
    return day < "2020-09-01"


def stat_keys(match_id):
    """(statistic keys, evidence reference) for a cached match payload."""
    path = os.path.join(MATCH_CACHE, f"{match_id}.json")
    if not os.path.exists(path):
        return "missing", f"cache_missing:match/{match_id}.json"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return "missing", f"cache_unreadable:match/{match_id}.json"
    periods = ((payload.get("content") or {}).get("stats") or {}).get("Periods") or {}
    groups = ((periods.get("All") or {}).get("stats")) or []
    evidence = f"cache:match/{match_id}.json"
    if not groups:
        return "no_block", evidence
    return ({str((item.get("key") or "")).lower()
             for group in groups for item in (group.get("stats") or [])}, evidence)


def local_match_state(fixture):
    """Classify a fixture with no matched id using cached day listings.

    A day whose listing cache does not exist is missing evidence, not proof that the
    provider listed no such match, so the two are reported separately.
    """
    day = datetime.date.fromisoformat(fixture["day"])
    aliases = fm.LEAGUE_ALIASES.get(fixture["league_id"], ())
    strong, weak, checked = [], [], []
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
        checked.append(f"cache:day/{probe}.json")
        for league in payload.get("leagues") or []:
            name = (league.get("name") or "").lower()
            if aliases and not any(alias in name for alias in aliases):
                continue
            if any(word in name for word in fm.NON_SENIOR):
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
                similarity = min(fm.name_ratio(fixture["home"], home),
                                 fm.name_ratio(fixture["away"], away))
                if fm.candidate_score({"home_team": fixture["home"], "away_team": fixture["away"],
                                       "home_goals": fixture["home_goals"],
                                       "away_goals": fixture["away_goals"],
                                       "day": fixture["day"]},
                                      {"home": home, "away": away,
                                       "home_score": hs, "away_score": aws,
                                       "utc": (match.get("status") or {}).get("utcTime"),
                                       "league": name}) < 2:
                    strong.append(similarity)
                else:
                    weak.append(similarity)
    evidence = ";".join(checked) or f"cache_missing:day/{day.strftime('%Y%m%d')}.json"
    if not checked:
        return "evidence_missing", evidence
    if strong or (len(weak) == 1 and weak[0] >= 0.55):
        return "would_match_now", evidence
    if len(weak) > 1:
        return "ambiguous_match_rejected", evidence
    return "no_provider_match", evidence


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
        select f.fixture_id, f.league_id, f.season, f.status_short,
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
                     "season": record["season"], "status": record["status_short"],
                     "day": record["d"], "home": record["home_team"],
                     "away": record["away_team"], "home_goals": record["home_goals"],
                     "away_goals": record["away_goals"],
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
    state_cache = {}
    lines = 0
    os.makedirs(ARTIFACTS, exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8") as handle:
        for fid, entry in per_fixture.items():
            keys, keys_evidence = (None, None)
            if entry["match_id"]:
                keys, keys_evidence = stat_keys(entry["match_id"])
            outcome = outcomes.get(str(fid), {})
            competition = COMPETITIONS.get(entry["league_id"], str(entry["league_id"]))
            for field, (_h, _a, field_keys) in FIELDS.items():
                if entry["paired"].get(field):
                    reason, evidence = "acquired_verified", "database:paired_value"
                elif not entry["has_row"]:
                    state = outcome.get("outcome")
                    if state == "matched_no_statistics_block":
                        reason = "provider_no_statistics_block"
                        evidence = f"cache:match/{outcome.get('fotmob_match_id')}.json"
                    elif state == "matched_with_statistics":
                        reason, evidence = "provider_value_missing_in_payload", "cache:match"
                    elif entry["status"] == "Scheduled":
                        reason, evidence = "not_played", "database:status"
                    else:
                        if fid not in state_cache:
                            state_cache[fid] = local_match_state(entry)
                        local, evidence = state_cache[fid]
                        reason = {"ambiguous_match_rejected": "ambiguous_match_rejected",
                                  "would_match_now": "not_attempted",
                                  "evidence_missing": "evidence_missing_request_not_attempted",
                                  }.get(local, "no_provider_match")
                elif field == "xg" and predates_xg_era(entry["season"], entry["day"]):
                    reason = "provider_predates_xg_era"
                    evidence = "provider_coverage:season<%s" % 2020
                elif entry["match_id"] is None:
                    if fid not in state_cache:
                        state_cache[fid] = local_match_state(entry)
                    local, evidence = state_cache[fid]
                    reason = {"ambiguous_match_rejected": "ambiguous_match_rejected",
                              "would_match_now": "not_attempted",
                              "evidence_missing": "evidence_missing_request_not_attempted",
                              }.get(local, "no_provider_match")
                elif keys == "missing":
                    reason, evidence = "evidence_missing_request_not_attempted", keys_evidence
                elif keys == "no_block":
                    reason, evidence = "provider_no_statistics_block", keys_evidence
                elif any(key in keys for key in field_keys):
                    reason, evidence = "provider_value_missing_in_payload", keys_evidence
                elif field in ("yellow_cards", "red_cards"):
                    reason, evidence = "provider_no_discipline_block", keys_evidence
                else:
                    reason, evidence = "provider_does_not_supply_field", keys_evidence
                summary[(field, reason)] += 1
                per_league[(entry["league_id"], field, reason)] += 1
                handle.write(json.dumps({
                    "fixture_id": fid, "field": field, "outcome": reason,
                    "competition": competition, "league_id": entry["league_id"],
                    "season": entry["season"], "day": entry["day"],
                    "status": entry["status"], "match_id": entry["match_id"],
                    "evidence": evidence,
                    "ambiguous_name_match": bool(outcome.get("weak_name_match")),
                }, sort_keys=True) + "\n")
                lines += 1

    acquired = collections.Counter()
    for entry in per_fixture.values():
        for field in entry["paired"]:
            acquired[field] += 1

    payload = {
        "generated_for": "historical enrichment closeout",
        "fixtures": len(per_fixture),
        "fields": list(FIELDS),
        "ledger_rows": lines,
        "outcome_definitions": DEFINITIONS,
        "xg_era_rule": "season start year < 2020; measured: 2019 season 27/3,794 with xG "
                       "(12/1,059 in Jan-Jun 2020) vs 2020 season 3,416/3,878",
        "acquired_paired_by_field": dict(acquired),
        "outcomes_by_field": {field: {reason: n for (f, reason), n in summary.items() if f == field}
                              for field in FIELDS},
        "gaps_by_league": {str(k[0]): {f"{k[1]}:{k[2]}": v for kk, v in per_league.items()
                                       if kk[0] == k[0]} for k in per_league},
    }
    with open(SUMMARY, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, sort_keys=True)
    print(json.dumps({"fixtures": len(per_fixture), "ledger_rows": lines,
                      "acquired": dict(acquired)}, indent=1))
    for field in FIELDS:
        print(field, json.dumps({r: n for (f, r), n in summary.items() if f == field}))


if __name__ == "__main__":
    main()
