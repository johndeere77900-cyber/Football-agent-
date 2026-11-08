"""Repair midnight kickoff timestamps from an already-verified twin fixture.

Many fixture rows imported from date-only providers carry a 00:00 UTC kickoff
even though the same real-world match is stored a second time (from UEFA or a
CSV provider) with its true kickoff time. This script copies that verified time
onto the date-only row.

Safety rules:
  * only rows whose stored kickoff is exactly 00:00 UTC are touched;
  * the donor must sit in the same competition and season, on the same UTC date,
    with team names that match (alias tolerant), and must have a non-midnight time;
  * exactly one donor is required, so an ambiguous day never moves a timestamp;
  * the kickoff date is never changed, only the time of day.

Usage:
    python kickoff_twin_repair.py --league 4 --season 2020 --dry-run
    python kickoff_twin_repair.py --league 4 --season 2020 --apply
"""

from __future__ import annotations

import argparse
import re
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher

import storage

MODEL = 0.0
MIN_SIMILARITY = 0.72


def normalize(name: str) -> str:
    text = unicodedata.normalize("NFKD", name or "")
    text = text.encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    text = re.sub(r"\s+", " ", text).strip()
    for filler in ("fc", "cf", "afc", "club", "national team"):
        text = re.sub(rf"\b{filler}\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def as_datetime(value):
    """Parse the text timestamps the fixtures table stores."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def similarity(home_a: str, away_a: str, home_b: str, away_b: str) -> float:
    forward = (SequenceMatcher(None, normalize(home_a), normalize(home_b)).ratio()
               + SequenceMatcher(None, normalize(away_a), normalize(away_b)).ratio()) / 2
    reverse = (SequenceMatcher(None, normalize(home_a), normalize(away_b)).ratio()
               + SequenceMatcher(None, normalize(away_a), normalize(home_b)).ratio()) / 2
    return max(forward, reverse)


def midnight_rows(cur, league_id, season):
    cur.execute(
        """
        SELECT fixture_id, home_team, away_team, kickoff_at
          FROM historical_fixtures
         WHERE league_id = %s AND season = %s
           AND to_char(kickoff_at::timestamptz AT TIME ZONE 'UTC', 'HH24:MI') = '00:00'
         ORDER BY kickoff_at, home_team
        """,
        (league_id, season),
    )
    return cur.fetchall()


def donors_on_date(cur, league_id, season, day):
    cur.execute(
        """
        SELECT fixture_id, home_team, away_team, kickoff_at
          FROM historical_fixtures
         WHERE league_id = %s AND season = %s
           AND (kickoff_at::timestamptz AT TIME ZONE 'UTC')::date = %s
           AND to_char(kickoff_at::timestamptz AT TIME ZONE 'UTC', 'HH24:MI') <> '00:00'
        """,
        (league_id, season, day),
    )
    return cur.fetchall()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--league", type=int, required=True)
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--min-similarity", type=float, default=MIN_SIMILARITY)
    args = parser.parse_args()

    conn, backend = storage._connect()
    if backend != "postgres":
        raise SystemExit("refusing to run outside PostgreSQL/Neon")

    matched, ambiguous, unmatched = [], [], []
    try:
        with conn.cursor() as cur:
            targets = midnight_rows(cur, args.league, args.season)
            for fixture_id, home, away, kickoff in targets:
                day = as_datetime(kickoff).date()
                candidates = []
                for donor in donors_on_date(cur, args.league, args.season, day):
                    score = similarity(home, away, donor[1], donor[2])
                    if score >= args.min_similarity:
                        candidates.append((score, donor))
                if len(candidates) == 1:
                    matched.append((fixture_id, home, away, day, candidates[0][1]))
                elif len(candidates) > 1:
                    ambiguous.append((fixture_id, home, away, day,
                                      [(round(c[0], 3), c[1][1], c[1][2], str(c[1][3])) for c in candidates]))
                else:
                    unmatched.append((fixture_id, home, away, day))

            print(f"league {args.league} season {args.season}: {len(targets)} rows at 00:00")
            print(f"  twin found      : {len(matched)}")
            print(f"  ambiguous       : {len(ambiguous)}")
            print(f"  no twin         : {len(unmatched)}")
            for item in ambiguous:
                print("  AMBIGUOUS", item[0], item[1], "v", item[2], item[3], item[4])
            for item in unmatched:
                print("  NO-TWIN  ", item[0], item[1], "v", item[2], item[3])

            if args.apply and matched:
                updated = 0
                for fixture_id, home, away, day, donor in matched:
                    donor_kickoff = donor[3]
                    if as_datetime(donor_kickoff).date() != day:
                        continue
                    cur.execute(
                        "UPDATE historical_fixtures SET kickoff_at = %s WHERE fixture_id = %s",
                        (donor_kickoff, fixture_id),
                    )
                    updated += cur.rowcount
                conn.commit()
                print(f"  updated         : {updated}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
