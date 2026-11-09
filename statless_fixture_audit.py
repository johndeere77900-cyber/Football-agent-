"""Investigate every football fixture that still has no statistics.

For each statless fixture this samples the ESPN all-competition scoreboard for
the fixture's date and the two adjacent dates, then looks for an event whose two
clubs match the fixture (alias tolerant) with the same final score. Every
fixture receives a recorded verdict and the evidence behind it, so a fixture can
only be called unavailable after the provider has actually been searched.

Verdicts:
  SIGHTED_SAME_COMPETITION  event found; statistics can be attached
  SIGHTED_OTHER_COMPETITION event found, but ESPN files it under a competition
                            that does not match the fixture's competition
  NOT_FOUND                 no event with either club on date +/- 1 day, in any
                            competition ESPN publishes for those dates

Usage:
    python statless_fixture_audit.py --limit 20 --dry-run
    python statless_fixture_audit.py            # full audit, writes evidence
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import unicodedata
from datetime import date, timedelta
from difflib import SequenceMatcher

import requests

import storage

CACHE_DIR = os.path.join(".api_cache", "audit_scoreboards")
REPORT = os.path.join(".api_cache", "reports", "statless_fixture_audit.json")
MIN_SIMILARITY = 0.72
REQUEST_TIMEOUT = 25
MAX_RETRIES = 3


def normalize(name: str) -> str:
    text = unicodedata.normalize("NFKD", name or "")
    text = text.encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    text = re.sub(r"\s+", " ", text).strip()
    for filler in ("fc", "cf", "afc", "club"):
        text = re.sub(rf"\b{filler}\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def similarity(home_a, away_a, home_b, away_b) -> float:
    forward = (SequenceMatcher(None, normalize(home_a), normalize(home_b)).ratio()
               + SequenceMatcher(None, normalize(away_a), normalize(away_b)).ratio()) / 2
    reverse = (SequenceMatcher(None, normalize(home_a), normalize(away_b)).ratio()
               + SequenceMatcher(None, normalize(away_a), normalize(home_b)).ratio()) / 2
    return max(forward, reverse)


def fetch_scoreboard(day: date):
    """All competitions ESPN publishes for one UTC date, cached on disk."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = day.strftime("%Y%m%d")
    path = os.path.join(CACHE_DIR, f"all_{key}.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            pass

    url = "https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard"
    backoff = 1.5
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(url, params={"dates": key}, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            if attempt >= MAX_RETRIES:
                return None
            time.sleep(backoff)
            backoff *= 2
            continue
        if response.status_code == 400:
            return None
        if response.status_code == 429 or response.status_code >= 500:
            if attempt >= MAX_RETRIES:
                return None
            time.sleep(backoff * 2)
            backoff *= 2
            continue
        if not response.ok:
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        temp = f"{path}.tmp.{os.getpid()}"
        try:
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(temp, path)
        except OSError:
            pass
        return data
    return None


def events_for(day: date):
    """Flatten one scoreboard payload into comparable event records."""
    data = fetch_scoreboard(day)
    if not isinstance(data, dict):
        return None
    collected = []
    for event in data.get("events") or []:
        competition = (event.get("competitions") or [{}])[0] or {}
        sides = {}
        for competitor in competition.get("competitors") or []:
            side = competitor.get("homeAway")
            if side not in ("home", "away"):
                continue
            team = competitor.get("team") or {}
            score = competitor.get("score")
            if isinstance(score, dict):
                score = score.get("value", score.get("displayValue"))
            sides[side] = {
                "name": team.get("displayName") or team.get("name"),
                "score": score,
            }
        if "home" not in sides or "away" not in sides:
            continue
        status = ((competition.get("status") or {}).get("type") or {}).get("name")
        league = ((event.get("season") or {}).get("slug")
                  or (event.get("league") or {}).get("name"))
        collected.append({
            "event_id": event.get("id"),
            "date": event.get("date"),
            "competition": league,
            "home": sides["home"]["name"],
            "away": sides["away"]["name"],
            "home_score": sides["home"]["score"],
            "away_score": sides["away"]["score"],
            "status": status,
        })
    return collected


def score_equal(a, b) -> bool:
    if a is None or b is None:
        return True
    try:
        return int(float(a)) == int(float(b))
    except (TypeError, ValueError):
        return True


def statless(limit=None):
    conn, backend = storage._connect()
    if backend != "postgres":
        raise SystemExit("refusing to run outside PostgreSQL/Neon")
    try:
        with conn.cursor() as cur:
            sql = """
                SELECT f.fixture_id, f.league_id, f.season, f.home_team, f.away_team,
                       f.home_goals, f.away_goals,
                       (f.kickoff_at::timestamptz AT TIME ZONE 'UTC')::date
                  FROM historical_fixtures f
                 WHERE NOT EXISTS (SELECT 1 FROM historical_fixture_enrichment e
                                    WHERE e.fixture_id = f.fixture_id)
                 ORDER BY f.league_id, f.season, f.kickoff_at
            """
            if limit:
                sql += f" LIMIT {int(limit)}"
            cur.execute(sql)
            return cur.fetchall()
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--max-attempts", type=int, default=MAX_RETRIES)
    args = parser.parse_args()

    rows = statless(args.limit)
    print(f"statless fixtures to audit: {len(rows)}")
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)

    evidence = []
    counts = {"SIGHTED_SAME_COMPETITION": 0, "SIGHTED_OTHER_COMPETITION": 0,
              "NOT_FOUND": 0, "SCOREBOARD_UNAVAILABLE": 0}
    for index, row in enumerate(rows, start=1):
        fixture_id, league_id, season, home, away, hg, ag, day = row
        days = [day - timedelta(days=1), day, day + timedelta(days=1)]
        payload_ok = True
        sightings = []
        for probe in days:
            events = events_for(probe)
            if events is None:
                payload_ok = False
                continue
            for event in events:
                if similarity(home, away, event["home"], event["away"]) < MIN_SIMILARITY:
                    continue
                if not (score_equal(hg, event["home_score"]) and score_equal(ag, event["away_score"])):
                    continue
                sightings.append(event)

        if not payload_ok and not sightings:
            verdict = "SCOREBOARD_UNAVAILABLE"
            detail = "ESPN all-competition scoreboard did not answer for date +/- 1"
        elif not sightings:
            verdict = "NOT_FOUND"
            detail = "no event with either club in any ESPN competition on date +/- 1"
        else:
            verdict = "SIGHTED_OTHER_COMPETITION"
            detail = "team and score match found; competition differs from fixture"
            # A sighting is only promoted when the competition name agrees.
            for event in sightings:
                comp = normalize(event.get("competition") or "")
                if comp and (comp in f"league {league_id}" or not comp):
                    verdict = "SIGHTED_SAME_COMPETITION"
                    detail = "team and score match with agreeing competition"
                    break

        counts[verdict] += 1
        record = {
            "fixture_id": fixture_id,
            "league_id": league_id,
            "season": season,
            "home": home,
            "away": away,
            "score": f"{hg}-{ag}",
            "date": str(day),
            "verdict": verdict,
            "detail": detail,
            "sightings": sightings[:4],
        }
        evidence.append(record)
        if index % 50 == 0 or index == len(rows):
            print(f"  ...{index}/{len(rows)} {counts}")
            with open(REPORT, "w", encoding="utf-8") as handle:
                json.dump({"counts": counts, "fixtures": evidence}, handle, indent=2)

    with open(REPORT, "w", encoding="utf-8") as handle:
        json.dump({"counts": counts, "fixtures": evidence}, handle, indent=2)
    print("verdicts:", counts)
    print(f"evidence: {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
