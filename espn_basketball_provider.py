"""
ESPN URL-based provider for historical NBA basketball games.

Primary route for historical basketball acquisition when league_id == 12 (NBA).
Queries ESPN's URL-based scoreboard endpoint for months October (start year)
through June (following year).

Endpoint URL:
https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard

Competition classification
--------------------------
ESPN states the competition in the payload itself: the scoreboard carries a
`leagues` entry whose uid is ``s:40~l:46`` (sport 40 = basketball, league 46 =
NBA), and every event and team uid repeats that league segment
(``s:40~l:46~e:<event>``, ``s:40~l:46~t:<team>``). NBA membership is therefore
never inferred from team names or from the requested URL alone: an event that
cannot be proven to belong to the NBA competition is quarantined, and a response
whose league metadata contradicts the NBA is treated as a failed acquisition unit
rather than a legitimate empty period.
"""

import json
import logging
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

ESPN_NBA_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
NBA_LEAGUE_ID = 12

# ESPN acquisition units: one monthly scoreboard request per NBA season month
# (October of the season year through June of the following year). Each month is
# an independent coverage unit and must never be reported as a single API page.
NBA_SEASON_MONTHS = [10, 11, 12, 1, 2, 3, 4, 5, 6]
NBA_SEASON_MONTH_COUNT = len(NBA_SEASON_MONTHS)

# Acquisition units are (year_offset, month) pairs relative to the season's start
# year, so each entry is exactly one monthly scoreboard request.
NBA_SEASON_MONTH_OFFSETS = [
    (0, 10), (0, 11), (0, 12), (1, 1), (1, 2), (1, 3), (1, 4), (1, 5), (1, 6)
]

# Season-specific coverage. The 2019-20 NBA season is the documented exception:
# it was suspended on 2020-03-11 and resumed in the Orlando bubble on
# 2020-07-30, with the Finals ending 2020-10-11. Those resumed months hold real
# regular-season and playoff games, so a season whose acquisition stops in June
# cannot have acquired the season and stays incomplete.
NBA_SEASON_MONTH_OFFSETS_2019 = [
    (0, 10), (0, 11), (0, 12), (1, 1), (1, 2), (1, 3), (1, 4), (1, 5), (1, 6),
    (1, 7), (1, 8), (1, 9), (1, 10)
]

_SEASON_MONTH_OFFSETS_OVERRIDES = {
    2019: NBA_SEASON_MONTH_OFFSETS_2019,
}

# --- Authoritative competition/event classification -------------------------
# Sport 40 is ESPN basketball and league 46 is the NBA in ESPN's uid scheme. Both
# are provider-native identifiers and are preserved verbatim on each record.
ESPN_NBA_SPORT_ID = "40"
ESPN_NBA_COMPETITION_ID = "46"
ESPN_NBA_LEAGUE_UID = f"s:{ESPN_NBA_SPORT_ID}~l:{ESPN_NBA_COMPETITION_ID}"

# ESPN `season.type` values. Anything outside this mapping fails closed: an event
# whose classification is unknown is quarantined instead of being assumed to be a
# regular-season game.
ESPN_SEASON_TYPE_CLASSIFICATION = {
    1: "preseason",
    2: "regular_season",
    3: "postseason",
    5: "play_in",
}

# `competitions[0].type` identifies special events that ESPN files inside the
# regular-season bucket, most importantly the All-Star Game (season.type 2 with
# competition type 4 / "ALLSTAR").
ESPN_ALLSTAR_COMPETITION_TYPE_IDS = {"4"}
ESPN_ALLSTAR_COMPETITION_ABBREVIATIONS = {"ALLSTAR"}


def get_season_month_offsets(season) -> list:
    """
    Return the (year_offset, month) acquisition units for a season.

    Falls back to the standard October-to-June window when the season is not an
    integer or has no season-specific coverage.
    """
    try:
        ssn = int(season)
    except (TypeError, ValueError):
        return list(NBA_SEASON_MONTH_OFFSETS)
    return list(_SEASON_MONTH_OFFSETS_OVERRIDES.get(ssn, NBA_SEASON_MONTH_OFFSETS))


def get_season_month_count(season) -> int:
    """Return the number of monthly scoreboards a season's coverage requires."""
    return len(get_season_month_offsets(season))


def _parse_espn_uid_parts(uid):
    """
    Split an ESPN uid such as ``s:40~l:46~e:401585183`` into its segments.

    Returns None when the uid is missing or not in the documented
    ``key:value~key:value`` form, so callers fail closed instead of guessing.
    """
    if not isinstance(uid, str) or not uid.strip():
        return None
    parts = {}
    for segment in uid.strip().split("~"):
        key, separator, value = segment.partition(":")
        key = key.strip()
        value = value.strip()
        if not separator or not key or not value:
            return None
        parts[key] = value
    return parts or None


def _uid_is_nba_basketball(uid) -> bool:
    """True when an ESPN uid provably belongs to NBA basketball (sport 40, league 46)."""
    parts = _parse_espn_uid_parts(uid)
    if not parts:
        return False
    return parts.get("s") == ESPN_NBA_SPORT_ID and parts.get("l") == ESPN_NBA_COMPETITION_ID


def _response_competition_verified(data):
    """
    Verify that a scoreboard payload is an NBA competition response.

    Returns (verified, reason). The payload's own `leagues` metadata is
    authoritative. When that metadata is absent the caller falls back to the
    per-event uids, which carry the same competition claim.
    """
    leagues = data.get("leagues")
    if not isinstance(leagues, list) or not leagues:
        return False, "missing_league_metadata"
    for league in leagues:
        if not isinstance(league, dict):
            continue
        if _uid_is_nba_basketball(league.get("uid")):
            return True, None
        if str(league.get("id") or "").strip() == ESPN_NBA_COMPETITION_ID:
            return True, None
    return False, "competition_metadata_mismatch"


def _classify_espn_event(event, competition, season, raw_event_id):
    """
    Classify one ESPN event against the NBA competition and the requested season.

    Returns ``(classification, reason)``. ``reason`` is a quarantine reason and is
    non-None when the event cannot be proven to belong to the NBA competition.
    Provider-native event ids are never rewritten; classification only decides
    whether the event may be accepted and records what it actually was.
    """
    event_uid = event.get("uid")
    uid_parts = _parse_espn_uid_parts(event_uid)
    if not uid_parts:
        return None, "event_uid_missing_or_unparseable"
    if not _uid_is_nba_basketball(event_uid):
        return None, "event_competition_uid_mismatch"
    uid_event_id = uid_parts.get("e")
    if uid_event_id is not None and str(uid_event_id).strip() != str(raw_event_id).strip():
        return None, "event_uid_id_mismatch"

    # Contradictory team-level competition metadata is evidence that the event is
    # not an NBA event. A missing team uid is not disqualifying on its own because
    # the event uid already establishes the competition.
    for competitor in competition.get("competitors") or []:
        if not isinstance(competitor, dict):
            continue
        team_obj = competitor.get("team")
        if not isinstance(team_obj, dict):
            continue
        team_uid = team_obj.get("uid")
        if team_uid is not None and not _uid_is_nba_basketball(team_uid):
            return None, "team_competition_uid_mismatch"

    season_obj = event.get("season") if isinstance(event.get("season"), dict) else {}
    try:
        season_year = int(season_obj.get("year"))
    except (TypeError, ValueError):
        return None, "unclassified_season_year"

    try:
        requested_season = int(season)
    except (TypeError, ValueError):
        return None, "unclassified_requested_season"
    # ESPN labels an NBA season by its ending year, so a season requested as
    # 2023 spans events tagged 2023 (early/late outliers) and 2024. Anything
    # outside that pair belongs to a different season and must not enter this
    # dataset.
    if season_year not in (requested_season, requested_season + 1):
        return None, "event_season_outside_requested_season"

    raw_season_type = season_obj.get("type")
    if raw_season_type is None:
        season_type_obj = event.get("seasonType")
        if isinstance(season_type_obj, dict):
            raw_season_type = season_type_obj.get("type")
            if raw_season_type is None:
                raw_season_type = season_type_obj.get("id")
        elif season_type_obj is not None:
            raw_season_type = season_type_obj
    try:
        season_type_id = int(raw_season_type)
    except (TypeError, ValueError):
        return None, "unclassified_season_type"

    competition_type = competition.get("type") if isinstance(competition.get("type"), dict) else {}
    competition_type_id = str(competition_type.get("id") or "").strip()
    competition_type_abbr = str(competition_type.get("abbreviation") or "").strip().upper()

    if (
        competition_type_id in ESPN_ALLSTAR_COMPETITION_TYPE_IDS
        or competition_type_abbr in ESPN_ALLSTAR_COMPETITION_ABBREVIATIONS
    ):
        season_type = "all_star"
    else:
        season_type = ESPN_SEASON_TYPE_CLASSIFICATION.get(season_type_id)
        if season_type is None:
            return None, f"unclassified_season_type_id_{season_type_id}"

    return {
        "season_type": season_type,
        "season_type_id": season_type_id,
        "season_slug": season_obj.get("slug") if isinstance(season_obj.get("slug"), str) else None,
        "espn_season_year": season_year,
        "competition_uid": ESPN_NBA_LEAGUE_UID,
        "competition_type_id": competition_type_id or None,
        "competition_type_abbreviation": competition_type_abbr or None,
    }, None


def fetch_espn_nba_season_games(
    league_id: int, season: int, per_request_timeout: int = 15, total_deadline_seconds: int = 60
) -> tuple[list, dict]:
    """
    Fetch and normalize historical NBA games from ESPN scoreboard API for a given season.

    For NBA league_id (12) and season YYYY, queries months Oct YYYY through June YYYY+1.
    Deduplicates events by ESPN event ID.

    Returns:
        tuple (normalized_games, coverage_metadata)
    """
    coverage_meta = {
        "months_attempted": 0,
        "months_succeeded": 0,
        "months_failed": 0,
        "all_months_succeeded": False,
        "monthly_details": [],
        # Competition classification evidence: only events proven to belong to
        # the NBA competition are accepted, so rejected counts describe events
        # quarantined by competition/season classification rather than by scores.
        "events_accepted": 0,
        "events_rejected": 0,
        "rejection_reasons": {},
        "competition_verified": True,
        "competition_uid": ESPN_NBA_LEAGUE_UID,
    }

    try:
        lid = int(league_id)
        ssn = int(season)
    except (ValueError, TypeError):
        logger.warning(f"Invalid league_id ({league_id}) or season ({season}) provided to ESPN provider.")
        return [], coverage_meta

    # Confirm configured league ID represents the NBA (12)
    if lid != NBA_LEAGUE_ID:
        logger.info(f"League ID {lid} is not NBA ({NBA_LEAGUE_ID}). Skipping ESPN NBA provider.")
        return [], coverage_meta

    months = [
        (ssn + year_offset, month)
        for year_offset, month in get_season_month_offsets(ssn)
    ]

    seen_event_ids = set()
    normalized_games = []

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    start_time = time.time()

    for year, month in months:
        elapsed = time.time() - start_time
        remaining = total_deadline_seconds - elapsed
        if remaining <= 0:
            logger.warning(f"ESPN NBA acquisition total deadline ({total_deadline_seconds}s) exceeded.")
            coverage_meta["months_failed"] += 1
            coverage_meta["monthly_details"].append(
                {"month": f"{year}{month:02d}", "status": "FAILED", "error": "total_deadline_exceeded"}
            )
            continue

        req_timeout = max(1, min(per_request_timeout, int(remaining)))
        date_param = f"{year}{month:02d}"
        url = f"{ESPN_NBA_SCOREBOARD_URL}?dates={date_param}&limit=1000"
        coverage_meta["months_attempted"] += 1

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=req_timeout) as resp:
                raw_bytes = resp.read()
                data = json.loads(raw_bytes.decode("utf-8", errors="ignore"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            logger.warning(f"ESPN scoreboard query failed for date {date_param}: {exc}")
            coverage_meta["months_failed"] += 1
            coverage_meta["monthly_details"].append({"month": date_param, "status": "FAILED", "error": str(exc)})
            continue

        if not isinstance(data, dict):
            coverage_meta["months_failed"] += 1
            coverage_meta["monthly_details"].append({"month": date_param, "status": "FAILED", "error": "non_object_json"})
            continue

        events = data.get("events")
        if not isinstance(events, list):
            coverage_meta["months_failed"] += 1
            coverage_meta["monthly_details"].append({"month": date_param, "status": "FAILED", "error": "missing_events_list"})
            continue

        # Competition verification happens before any event is accepted. League
        # metadata that contradicts the NBA is a failed acquisition unit; when the
        # metadata is absent, the per-event uids must prove NBA membership.
        competition_verified, competition_reason = _response_competition_verified(data)
        if events and not competition_verified and competition_reason != "missing_league_metadata":
            coverage_meta["months_failed"] += 1
            coverage_meta["months_failed_reason"] = competition_reason
            coverage_meta["monthly_details"].append(
                {
                    "month": date_param,
                    "status": "FAILED",
                    "error": competition_reason,
                    "competition_verified": False,
                    "event_count": len(events),
                }
            )
            continue

        month_games = []
        month_rejected = 0
        month_rejection_reasons = {}

        def _reject(reason):
            nonlocal month_rejected
            month_rejected += 1
            month_rejection_reasons[reason] = month_rejection_reasons.get(reason, 0) + 1

        for ev in events:
            if not isinstance(ev, dict):
                _reject("event_not_object")
                continue

            eid_raw = ev.get("id")
            if not eid_raw:
                _reject("event_missing_id")
                continue

            try:
                raw_eid = int(eid_raw)
                if raw_eid <= 0:
                    raise ValueError
                game_id = raw_eid
            except (ValueError, TypeError):
                _reject("event_invalid_id")
                continue

            if game_id in seen_event_ids:
                continue

            iso_date = ev.get("date")
            if not iso_date or not isinstance(iso_date, str):
                _reject("event_missing_date")
                continue

            competitions = ev.get("competitions")
            if not isinstance(competitions, list) or not competitions or not isinstance(competitions[0], dict):
                _reject("event_missing_competition")
                continue

            comp = competitions[0]

            # Authoritative competition/season classification: an event that
            # cannot be proven to belong to this NBA season is quarantined rather
            # than written into NBA historical data.
            classification, classification_reason = _classify_espn_event(ev, comp, ssn, raw_eid)
            if classification_reason:
                _reject(classification_reason)
                continue

            status_obj = comp.get("status", {}) if isinstance(comp.get("status"), dict) else {}
            status_type = status_obj.get("type", {}) if isinstance(status_obj.get("type"), dict) else {}

            completed = status_type.get("completed", False)
            status_name = status_type.get("name", "")
            is_final = completed or status_name in ("STATUS_FINAL", "STATUS_FULL_TIME", "FT")
            if not is_final:
                _reject("event_not_final")
                continue

            status_short = "FT"

            competitors = comp.get("competitors")
            if not isinstance(competitors, list) or len(competitors) < 2:
                _reject("event_missing_competitors")
                continue

            home_comp = None
            away_comp = None
            for c in competitors:
                if isinstance(c, dict):
                    if c.get("homeAway") == "home":
                        home_comp = c
                    elif c.get("homeAway") == "away":
                        away_comp = c

            if not home_comp or not away_comp:
                _reject("event_missing_home_or_away")
                continue

            home_team_obj = home_comp.get("team", {}) if isinstance(home_comp.get("team"), dict) else {}
            away_team_obj = away_comp.get("team", {}) if isinstance(away_comp.get("team"), dict) else {}

            try:
                home_team_id = int(home_team_obj.get("id"))
                away_team_id = int(away_team_obj.get("id"))
                if home_team_id <= 0 or away_team_id <= 0 or home_team_id == away_team_id:
                    raise ValueError
            except (ValueError, TypeError):
                _reject("event_invalid_team_ids")
                continue

            home_name = home_team_obj.get("displayName") or home_team_obj.get("name")
            away_name = away_team_obj.get("displayName") or away_team_obj.get("name")
            if not home_name or not isinstance(home_name, str) or not home_name.strip():
                _reject("event_invalid_home_team_name")
                continue
            if not away_name or not isinstance(away_name, str) or not away_name.strip():
                _reject("event_invalid_away_team_name")
                continue

            home_score_raw = home_comp.get("score")
            away_score_raw = away_comp.get("score")
            if home_score_raw is None or away_score_raw is None:
                _reject("event_missing_score")
                continue

            try:
                home_points = int(home_score_raw)
                away_points = int(away_score_raw)
                if home_points < 0 or away_points < 0:
                    raise ValueError
            except (ValueError, TypeError):
                _reject("event_invalid_score")
                continue

            game_dict = {
                "id": game_id,
                "date": iso_date,
                "status": {
                    "short": status_short,
                    "long": status_type.get("description", "Final"),
                },
                "league": {
                    "id": lid,
                    "season": ssn,
                    "name": "NBA",
                },
                "teams": {
                    "home": {"id": home_team_id, "name": home_name.strip()},
                    "away": {"id": away_team_id, "name": away_name.strip()},
                },
                "scores": {
                    "home": {"total": home_points},
                    "away": {"total": away_points},
                },
                "provider_provenance": {
                    "provider": "espn_url",
                    "provider_type": "primary_url",
                    "provider_game_id": raw_eid,
                    "provider_team_ids": {"home": home_team_id, "away": away_team_id},
                    # Provider-native competition/season classification. Persisted
                    # so validation can tell regular-season coverage from
                    # preseason, postseason and All-Star events instead of
                    # assuming every scoreboard event is a regular-season game.
                    "competition_uid": classification["competition_uid"],
                    "event_uid": event_uid if isinstance((event_uid := ev.get("uid")), str) else None,
                    "season_type": classification["season_type"],
                    "season_type_id": classification["season_type_id"],
                    "season_slug": classification["season_slug"],
                    "espn_season_year": classification["espn_season_year"],
                    "competition_type_id": classification["competition_type_id"],
                    "competition_type_abbreviation": classification["competition_type_abbreviation"],
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                },
            }

            seen_event_ids.add(raw_eid)
            seen_event_ids.add(game_id)
            month_games.append(game_dict)

        # A month whose league metadata was absent and whose events could not be
        # proven to belong to the NBA is not a legitimate empty period: it is an
        # unverified acquisition unit and fails closed.
        if (
            not competition_verified
            and competition_reason == "missing_league_metadata"
            and not month_games
            and month_rejected > 0
        ):
            coverage_meta["months_failed"] += 1
            coverage_meta["monthly_details"].append(
                {
                    "month": date_param,
                    "status": "FAILED",
                    "error": "competition_unverified",
                    "competition_verified": False,
                    "event_count": len(events),
                    "events_rejected": month_rejected,
                }
            )
            continue

        coverage_meta["months_succeeded"] += 1
        coverage_meta["events_accepted"] += len(month_games)
        coverage_meta["events_rejected"] += month_rejected
        for reason, count in month_rejection_reasons.items():
            coverage_meta["rejection_reasons"][reason] = (
                coverage_meta["rejection_reasons"].get(reason, 0) + count
            )
        coverage_meta["monthly_details"].append(
            {
                "month": date_param,
                "status": "SUCCESS",
                "event_count": len(events),
                "events_accepted": len(month_games),
                "events_rejected": month_rejected,
                "competition_verified": bool(competition_verified),
                "rejection_reasons": month_rejection_reasons or None,
            }
        )
        normalized_games.extend(month_games)

    coverage_meta["competition_verified"] = coverage_meta["months_failed"] == 0
    coverage_meta["all_months_succeeded"] = (
        coverage_meta["months_attempted"] == len(months)
        and coverage_meta["months_failed"] == 0
        and coverage_meta["months_succeeded"] == len(months)
    )

    logger.info(
        f"ESPN NBA provider fetched {len(normalized_games)} games for season {ssn} "
        f"(succeeded {coverage_meta['months_succeeded']}/{coverage_meta['months_attempted']} months, "
        f"accepted {coverage_meta['events_accepted']} events, rejected {coverage_meta['events_rejected']})."
    )
    return normalized_games, coverage_meta
