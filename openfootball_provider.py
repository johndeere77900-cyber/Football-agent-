"""
OpenFootball historical football acquisition.

Supported sources:
  * openfootball/football.json  (JSON league/cup seasons)
  * openfootball/champions-league, england, italy and south-america (Football.TXT)
  * openfootball/internationals (men's full international results)

The adapters never infer a completed fixture from a non-empty response. They
normalize only facts present in the source, keep source dates/time precision, and
leave coverage completion to the resolver's independent structural validation.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
import unicodedata
from datetime import datetime, timezone
from typing import Any

import config
import team_identity
import time_utils


JSON_PROVIDER = "openfootball_json"
TXT_PROVIDER = "openfootball_txt"
INTERNATIONAL_PROVIDER = "openfootball_internationals"

FOOTBALL_JSON_REPOSITORY = "https://github.com/openfootball/football.json"
FOOTBALL_JSON_BASE = (
    "https://raw.githubusercontent.com/openfootball/football.json/master"
)
INTERNATIONALS_REPOSITORY = "https://github.com/openfootball/internationals"
INTERNATIONALS_BASE = (
    "https://raw.githubusercontent.com/openfootball/internationals/master"
)

TXT_REPOSITORIES = {
    "champions-league": "https://github.com/openfootball/champions-league",
    "england": "https://github.com/openfootball/england",
    "italy": "https://github.com/openfootball/italy",
    "south-america": "https://github.com/openfootball/south-america",
}
TXT_RAW_BASES = {
    "champions-league": "https://raw.githubusercontent.com/openfootball/champions-league/master",
    "england": "https://raw.githubusercontent.com/openfootball/england/master",
    "italy": "https://raw.githubusercontent.com/openfootball/italy/master",
    "south-america": "https://raw.githubusercontent.com/openfootball/south-america/master",
}

# Competition identity is checked against the file's own title before any
# record is used. Only verified league ids are mapped; files whose competition
# has no configured id stay diagnostic-only rather than receiving an invented id.
JSON_COMPETITIONS = {
    39: {
        "file": "en.1.json",
        "name": "Premier League",
        "tokens": ("premier league",),
    },
    140: {
        "file": "es.1.json",
        "name": "La Liga",
        "tokens": ("primera division", "la liga"),
    },
    135: {
        "file": "it.1.json",
        "name": "Serie A",
        "tokens": ("serie a",),
    },
    78: {
        "file": "de.1.json",
        "name": "Bundesliga",
        "tokens": ("bundesliga",),
    },
    61: {
        "file": "fr.1.json",
        "name": "Ligue 1",
        "tokens": ("ligue 1",),
    },
    88: {
        "file": "nl.1.json",
        "name": "Eredivisie",
        "tokens": ("eredivisie",),
    },
    94: {
        "file": "pt.1.json",
        "name": "Primeira Liga",
        "tokens": ("primeira liga",),
    },
    2: {
        "file": "uefa.cl.json",
        "name": "UEFA Champions League",
        "tokens": ("champions league",),
        # Only exact seasons whose file and identity were checked are enabled.
        # The 2026-27 UCL JSON currently returns 404, so it is not mapped.
        "edition_seasons": (2019, 2024),
    },
}

# Club Football.TXT sources. The listed competition ids are existing catalogue
# ids. EFL Cup, FA Cup, Coppa Italia, Conference League, Libertadores and
# Brasileirao are parsed by inspect_txt_source() but are not enabled here
# because the repository has no authoritative league id for them.
TXT_COMPETITIONS = {
    2: {
        "repository": "champions-league",
        "file": "cl.txt",
        "name": "UEFA Champions League",
        "tokens": ("champions league",),
        "edition_seasons": (2019, 2024),
    },
    3: {
        "repository": "champions-league",
        "file": "el.txt",
        "name": "UEFA Europa League",
        "tokens": ("europa league",),
        "edition_seasons": (2024,),
    },
    39: {
        "repository": "england",
        "file": "1-premierleague.txt",
        "name": "Premier League",
        "tokens": ("premier league",),
    },
    135: {
        "repository": "italy",
        "file": "1-seriea.txt",
        "name": "Serie A",
        "tokens": ("serie a",),
    },
}

INTERNATIONAL_SOURCES = {
    1: {
        "directory": "fifa_world_cup",
        "slug": "fifa_world_cup",
        "name": "FIFA World Cup",
        "tokens": ("world cup",),
        # Only the verified finals edition is enabled.
        "edition_seasons": (2022,),
    },
    4: {
        "directory": "uefa_euro",
        "slug": "uefa_euro",
        "name": "UEFA EURO",
        "tokens": ("uefa euro", "uefa european championship", "european championship"),
        # UEFA EURO 2020 is requested as edition 2020 but the repository names
        # the played-in-2021 file 2021_uefa_euro.txt. Its title says 2021; the
        # requested season remains 2020.
        "edition_seasons": (2020, 2024),
        "source_year_overrides": {2020: 2021},
    },
    5: {
        "directory": "uefa_nations_league",
        "slug": "uefa_nations_league",
        "name": "UEFA Nations League",
        "tokens": ("nations league",),
        "edition_seasons": tuple(range(2018, 2026)),
    },
}

# Diagnostic-only exact files checked during this implementation. None of these
# is enabled for storage because the competition lacks a configured id.
DIAGNOSTIC_TXT_FILES = {
    "uefa_conference_league": {
        "repository": "champions-league",
        "file": "conf.txt",
        "name": "UEFA Conference League",
        "season": 2024,
        "tokens": ("conference league",),
    },
    "uefa_champions_league_qualifying": {
        "repository": "champions-league",
        "file": "clq.txt",
        "name": "UEFA Champions League qualifying",
        "season": 2024,
        "tokens": ("champions league",),
    },
    "england_efl_cup": {
        "repository": "england",
        "file": "eflcup.txt",
        "name": "EFL Cup",
        "season": 2024,
        "tokens": ("efl cup",),
    },
    "england_fa_cup": {
        "repository": "england",
        "file": "facup.txt",
        "name": "FA Cup",
        "season": 2024,
        "tokens": ("fa cup",),
    },
    "italy_coppa_italia": {
        "repository": "italy",
        "file": "cup.txt",
        "name": "Coppa Italia",
        "season": 2024,
        "tokens": ("coppa italia",),
    },
    "copa_libertadores_2024": {
        "repository": "south-america",
        "file": "copa-libertadores/2024_copal.txt",
        "name": "Copa Libertadores",
        "season": 2024,
        "tokens": ("copa libertadores",),
        "calendar_year": 2024,
    },
    "brazil_serie_a_2024": {
        "repository": "south-america",
        "file": "brazil/2024_br1.txt",
        "name": "Brasileiro Serie A",
        "season": 2024,
        "tokens": ("brasileiro", "serie a"),
        "calendar_year": 2024,
    },
}

_SOURCE_COMMIT_CACHE: dict[str, str | None] = {}
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_DATE_LINE_RE = re.compile(
    r"^\s*(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+)?"
    r"(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
    r"(?P<day>\d{1,2})(?:\s+(?P<year>\d{4}))?\s*$",
    re.IGNORECASE,
)
_SCORE_END_RE = re.compile(
    r"(?P<main>\d{1,2}-\d{1,2})"
    r"(?P<pen>\s+pen\.)?"
    r"(?P<aet_pair>\s+\d{1,2}-\d{1,2})?"
    r"(?P<aet_marker>\s+a\.e\.t\.)?"
    r"\s*(?:\((?P<paren>[^)]*)\))?\s*$",
    re.IGNORECASE,
)
_INTL_LINE_RE = re.compile(
    r"^\s{1,6}(?P<home>.+?)\s+(?P<hg>\d{1,2})-(?P<ag>\d{1,2})\s+"
    r"(?P<away>.+?)(?:\s{2,}@(?P<venue>.*?))?\s*$"
)
_PEN_WINNER_RE = re.compile(r"\[(?P<winner>.+?)\s+wins?\s+on\s+penalt", re.IGNORECASE)


def _utc_now() -> str:
    return time_utils.format_utc_iso(datetime.now(timezone.utc))


def _season_key(season: int) -> str:
    return f"{int(season)}-{(int(season) + 1) % 100:02d}"


def _stable_int(*parts: Any, bits: int = 52) -> int:
    raw = "|".join(str(part) for part in parts if part is not None)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return int(digest[: max(8, bits // 4)], 16) & ((1 << bits) - 1)


def _team_provider_id(provider: str, league_id: int, name: str) -> int:
    return max(1, _stable_int(provider, league_id, team_identity.normalize_team_name(name), bits=48))


def _source_commit(repository: str, timeout: float = 5.0) -> str | None:
    """Return the current master commit when GitHub makes it available."""
    if repository in _SOURCE_COMMIT_CACHE:
        return _SOURCE_COMMIT_CACHE[repository]
    api_url = (
        repository.replace("https://github.com/", "https://api.github.com/repos/")
        + "/commits/master"
    )
    request = urllib.request.Request(
        api_url,
        headers={
            "User-Agent": "Football-agent-historical/1.0",
            "Accept": "application/vnd.github+json",
        },
    )
    commit = None
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("sha"), str):
            commit = payload["sha"]
    except Exception:  # noqa: BLE001 - provenance enrichment is optional
        commit = None
    _SOURCE_COMMIT_CACHE[repository] = commit
    return commit


def _http_text(url: str, timeout: float = 20.0) -> tuple[str, str, dict[str, Any]]:
    """Fetch text with an explicit, machine-readable outcome."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Football-agent-historical/1.0",
            "Accept": "text/plain, application/json;q=0.9, */*;q=0.1",
        },
    )
    meta: dict[str, Any] = {"source_url": url, "retrieved_at": _utc_now()}
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status_code = int(getattr(response, "status", 200))
            body = response.read().decode("utf-8-sig", errors="replace")
    except urllib.error.HTTPError as exc:
        status_code = int(exc.code)
        body = ""
        meta.update({"http_status": status_code, "error": f"HTTP {status_code}"})
        if status_code in (401, 403):
            return "ACCESS_DENIED", "", {**meta, "error_category": "access_restricted"}
        if status_code == 404:
            return "UNSUPPORTED_COVERAGE", "", {**meta, "error_category": "unsupported_coverage"}
        if status_code == 429:
            return "RATE_LIMITED", "", {**meta, "error_category": "rate_limited"}
        return "SOURCE_FAILED", "", {**meta, "error_category": "network_error"}
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        text = str(reason).lower()
        category = "network_error"
        status = "SOURCE_FAILED"
        if "timed out" in text or "timeout" in text:
            category = "timeout"
            status = "TIMEOUT"
        meta.update({"error": str(reason), "error_category": category})
        return status, "", meta

    meta["http_status"] = status_code
    if status_code != 200:
        return "SOURCE_FAILED", "", {**meta, "error_category": "network_error"}
    if not body.strip():
        return "SOURCE_EMPTY", "", {**meta, "error": "empty response body"}
    return "SOURCE_AVAILABLE", body, meta


def _fold_text(value: str) -> str:
    """Accent- and case-fold source text before comparing identity tokens."""
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _identity_matches(title: str, tokens: tuple[str, ...]) -> bool:
    normalized = re.sub(r"\s+", " ", _fold_text(title).replace("&", " and ")).strip().lower()
    return bool(normalized) and any(_fold_text(token).lower() in normalized for token in tokens)


def _technical_date(date_value: str) -> str:
    """Represent date-only evidence as midnight UTC plus explicit precision flags."""
    return f"{date_value}T00:00:00+00:00"


def _date_and_local_time(
    date_value: str,
    local_time: str | None,
    *,
    calendar_year: int | None = None,
) -> tuple[str, dict[str, Any]]:
    date_text = str(date_value or "").strip()
    try:
        parsed = datetime.strptime(date_text, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"invalid source date {date_value!r}") from exc
    metadata: dict[str, Any] = {
        "date_precision": "date",
        "source_date": parsed.date().isoformat(),
        "kickoff_time_known": False,
        "kickoff_time_precision": "date_only",
        "kickoff_time_zone_known": False,
    }
    time_text = str(local_time or "").strip()
    if time_text:
        try:
            parsed_time = datetime.strptime(time_text, "%H:%M")
        except ValueError as exc:
            raise ValueError(f"invalid source time {local_time!r}") from exc
        metadata.update(
            {
                "kickoff_time_known": True,
                "kickoff_time_precision": "minute_local",
                "kickoff_time_local": parsed_time.strftime("%H:%M"),
                "kickoff_time_zone_known": False,
            }
        )
    if calendar_year is not None:
        metadata["source_calendar_year"] = int(calendar_year)
    return _technical_date(parsed.date().isoformat()), metadata


def _parse_pair(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    values = []
    for item in value:
        if isinstance(item, bool):
            return None
        try:
            number = int(item)
        except (TypeError, ValueError):
            return None
        if number < 0:
            return None
        values.append(number)
    return values[0], values[1]


def _score_bundle(score: dict[str, Any]) -> tuple[dict[str, Any], str, tuple[int, int] | None]:
    """Return source score fields, completed status and pre-shootout goals."""
    ft = _parse_pair(score.get("ft"))
    ht = _parse_pair(score.get("ht"))
    et = _parse_pair(score.get("et"))
    penalties = _parse_pair(score.get("p"))
    bundle: dict[str, Any] = {
        "halftime": {"home": ht[0], "away": ht[1]} if ht else None,
        "fulltime": {"home": ft[0], "away": ft[1]} if ft else None,
        "extratime": {"home": et[0], "away": et[1]} if et else None,
        "penalty_shootout": {"home": penalties[0], "away": penalties[1]} if penalties else None,
    }
    if ft is None:
        return bundle, "NS", None
    if penalties is not None:
        goals = et or ft
        return bundle, "PEN", goals
    if et is not None:
        return bundle, "AET", et
    return bundle, "FT", ft


def _resolve_team(
    raw_name: str,
    provider: str,
    league_id: int,
    cache: dict[str, str | None],
    *,
    strip_country_suffix: bool = False,
) -> tuple[str | None, str, int]:
    display = str(raw_name or "").strip()
    candidate = re.sub(r"\s+\([A-Z]{2,4}\)\s*$", "", display).strip() if strip_country_suffix else display
    cache_key = f"{candidate}|{league_id}"
    if cache_key in cache:
        canonical = cache[cache_key]
    else:
        canonical = team_identity.bootstrap_historical_team_identity(
            raw_name=candidate,
            provider=provider,
            provider_team_id=_team_provider_id(provider, league_id, candidate),
            league_id=league_id,
            sport="football",
        )
        if not canonical and candidate != display:
            canonical = team_identity.bootstrap_historical_team_identity(
                raw_name=display,
                provider=provider,
                provider_team_id=_team_provider_id(provider, league_id, display),
                league_id=league_id,
                sport="football",
            )
        cache[cache_key] = canonical
    team_id = _team_provider_id(provider, league_id, candidate)
    return canonical, candidate or display, team_id


def _build_fixture(
    *,
    provider: str,
    league_id: int,
    season: int,
    league_name: str,
    source_url: str,
    repository: str,
    source_file: str,
    source_commit: str | None,
    retrieved_at: str,
    source_match_count: int | None,
    fixture_id_parts: tuple[Any, ...],
    date_value: str,
    local_time: str | None,
    home_name: str,
    away_name: str,
    goals: tuple[int, int] | None,
    status_short: str,
    score: dict[str, Any],
    round_name: str | None,
    stage: str | None,
    team_cache: dict[str, str | None],
    extra_provenance: dict[str, Any] | None = None,
    strip_country_suffix: bool = False,
    calendar_year: int | None = None,
) -> dict[str, Any] | None:
    canonical_home, home_display, home_id = _resolve_team(
        home_name,
        provider,
        league_id,
        team_cache,
        strip_country_suffix=strip_country_suffix,
    )
    canonical_away, away_display, away_id = _resolve_team(
        away_name,
        provider,
        league_id,
        team_cache,
        strip_country_suffix=strip_country_suffix,
    )
    if not canonical_home or not canonical_away or canonical_home == canonical_away:
        return None
    try:
        technical_date, date_meta = _date_and_local_time(
            date_value, local_time, calendar_year=calendar_year
        )
    except ValueError:
        return None

    fixture_id = max(
        1,
        _stable_int(
            provider,
            league_id,
            season,
            *fixture_id_parts,
            *date_meta.values(),
            home_display,
            away_display,
            bits=52,
        ),
    )
    status_long = {
        "FT": "Finished",
        "AET": "Finished after extra time",
        "PEN": "Finished after penalty shootout",
        "NS": "Not started / score unavailable",
        "CANC": "Cancelled",
        "PST": "Postponed",
    }.get(status_short, status_short)

    provenance: dict[str, Any] = {
        "provider": provider,
        "providers": [provider],
        "provider_type": "openfootball" if provider != "openligadb" else "official_api",
        "repository": repository,
        "source_url": source_url,
        "source_format": "football.txt" if provider != JSON_PROVIDER else "json",
        "source_file": source_file,
        "requested_season": int(season),
        "league_id": int(league_id),
        "retrieved_at": retrieved_at,
        "provider_fixture_id": fixture_id,
        "provider_team_ids": {"home": home_id, "away": away_id},
        "team_names_raw": {"home": str(home_name), "away": str(away_name)},
        "source_declared_match_count": source_match_count,
    }
    if source_commit:
        provenance["source_commit"] = source_commit
    if round_name:
        provenance["round"] = round_name
    if stage:
        provenance["stage"] = stage
    if extra_provenance:
        provenance.update(extra_provenance)

    payload: dict[str, Any] = {
        "fixture": {
            "id": fixture_id,
            "date": technical_date,
            "status": {"short": status_short, "long": status_long},
            "date_precision": date_meta["date_precision"],
            "source_date": date_meta["source_date"],
            "kickoff_time_known": date_meta["kickoff_time_known"],
            "kickoff_time_precision": date_meta["kickoff_time_precision"],
            "timezone_known": date_meta["kickoff_time_zone_known"],
            "round": round_name,
        },
        "league": {"id": int(league_id), "season": int(season), "name": league_name},
        "teams": {
            "home": {"id": home_id, "name": home_display},
            "away": {"id": away_id, "name": away_display},
        },
        "goals": {
            "home": goals[0] if goals is not None else None,
            "away": goals[1] if goals is not None else None,
        },
        "score": score,
        "canonical_home_id": canonical_home,
        "canonical_away_id": canonical_away,
        "provider_provenance": provenance,
    }
    if date_meta.get("kickoff_time_local"):
        payload["fixture"]["kickoff_time_local"] = date_meta["kickoff_time_local"]
    if stage:
        payload["stage"] = stage
    return payload


def get_json_games(
    league_id: int,
    season: int,
    *,
    timeout: float = 20.0,
    completed_only: bool = True,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Fetch and normalize one exact OpenFootball JSON season file."""
    source = JSON_COMPETITIONS.get(int(league_id))
    if not source:
        return "UNSUPPORTED_COVERAGE", [], {
            "status": "UNSUPPORTED_COVERAGE",
            "error_category": "unsupported_coverage",
            "reason": "no_openfootball_json_mapping",
            "source": JSON_PROVIDER,
        }
    allowed_seasons = source.get("edition_seasons")
    if allowed_seasons and int(season) not in set(allowed_seasons):
        return "UNSUPPORTED_COVERAGE", [], {
            "status": "UNSUPPORTED_COVERAGE",
            "error_category": "unsupported_coverage",
            "reason": "openfootball_json_unverified_edition",
            "source": JSON_PROVIDER,
            "requested_season": int(season),
        }

    season_key = _season_key(season)
    url = f"{FOOTBALL_JSON_BASE}/{season_key}/{source['file']}"
    fetch_status, body, meta = _http_text(url, timeout=timeout)
    meta.update(
        {
            "source": JSON_PROVIDER,
            "repository": FOOTBALL_JSON_REPOSITORY,
            "source_file": source["file"],
            "requested_season": int(season),
            "season_key": season_key,
        }
    )
    if fetch_status != "SOURCE_AVAILABLE":
        return fetch_status, [], meta

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        return "PARSER_ERROR", [], {
            **meta,
            "status": "PARSER_ERROR",
            "error_category": "parsing_error",
            "error": f"invalid JSON: {exc}",
        }
    title = payload.get("name") if isinstance(payload, dict) else None
    matches = payload.get("matches") if isinstance(payload, dict) else None
    if not _identity_matches(title, source["tokens"]):
        return "PARSER_ERROR", [], {
            **meta,
            "status": "PARSER_ERROR",
            "error_category": "parsing_error",
            "error": f"competition identity mismatch: {title!r}",
            "source_title": title,
        }
    if not isinstance(matches, list):
        return "PARSER_ERROR", [], {
            **meta,
            "status": "PARSER_ERROR",
            "error_category": "parsing_error",
            "error": "matches is not a list",
            "source_title": title,
        }
    if not matches:
        return "SOURCE_EMPTY", [], {
            **meta,
            "status": "SOURCE_EMPTY",
            "source_title": title,
            "error": "empty matches array is not acquisition success",
        }

    commit = _source_commit(FOOTBALL_JSON_REPOSITORY)
    team_cache: dict[str, str | None] = {}
    fixtures: list[dict[str, Any]] = []
    rejected = 0
    unscored = 0
    cancelled = 0
    for index, match in enumerate(matches):
        if not isinstance(match, dict):
            rejected += 1
            continue
        date_value = str(match.get("date") or "").strip()
        home_name = str(match.get("team1") or "").strip()
        away_name = str(match.get("team2") or "").strip()
        if not date_value or not home_name or not away_name:
            rejected += 1
            continue
        score_obj = match.get("score") if isinstance(match.get("score"), dict) else {}
        score, status_short, goals = _score_bundle(score_obj)
        source_status = str(match.get("status") or "").strip().lower()
        if source_status in ("cancelled", "canceled"):
            status_short = "CANC"
            goals = None
            cancelled += 1
        elif source_status == "postponed":
            status_short = "PST"
            goals = None
        elif score_obj.get("ft") and status_short in ("FT", "AET", "PEN"):
            if source_status == "awarded":
                score["awarded"] = True
        if goals is None:
            unscored += 1
        if completed_only and status_short not in ("FT", "AET", "PEN"):
            continue
        fixture = _build_fixture(
            provider=JSON_PROVIDER,
            league_id=int(league_id),
            season=int(season),
            league_name=source["name"],
            source_url=url,
            repository=FOOTBALL_JSON_REPOSITORY,
            source_file=source["file"],
            source_commit=commit,
            retrieved_at=meta["retrieved_at"],
            source_match_count=len(matches),
            fixture_id_parts=(index, match.get("round"), date_value, home_name, away_name),
            date_value=date_value,
            local_time=match.get("time") if isinstance(match.get("time"), str) else None,
            home_name=home_name,
            away_name=away_name,
            goals=goals,
            status_short=status_short,
            score=score,
            round_name=str(match.get("round") or "").strip() or None,
            stage=str(match.get("stage") or "").strip() or None,
            team_cache=team_cache,
            extra_provenance={
                "source_competition_title": title,
                "source_status": source_status or None,
                "source_match_index": index,
            },
            strip_country_suffix=(int(league_id) in (2, 3)),
        )
        if fixture is None:
            rejected += 1
            continue
        fixtures.append(fixture)

    return "SOURCE_AVAILABLE", fixtures, {
        **meta,
        "status": "SOURCE_AVAILABLE",
        "source_title": title,
        "source_declared_match_count": len(matches),
        "fixtures_returned": len(fixtures),
        "rejected_matches": rejected,
        "unscored_matches": unscored,
        "cancelled_matches": cancelled,
        "completed_only": bool(completed_only),
        "source_commit": commit,
    }


def _parse_txt_match_result(rest: str) -> dict[str, Any] | None:
    match = _SCORE_END_RE.search(str(rest or "").strip())
    if not match:
        return None
    main = _parse_pair(match.group("main").split("-"))
    if main is None:
        return None
    penalties = None
    if match.group("pen"):
        penalties = main
    aet_pair = None
    aet_text = (match.group("aet_pair") or "").strip()
    if aet_text:
        aet_pair = _parse_pair(aet_text.split("-"))
    paren = (match.group("paren") or "").strip()
    paren_pairs: list[tuple[int, int]] = []
    for pair_text in re.findall(r"\d{1,2}-\d{1,2}", paren):
        pair = _parse_pair(pair_text.split("-"))
        if pair is not None:
            paren_pairs.append(pair)
    has_aet = bool(match.group("aet_marker")) or aet_pair is not None

    if penalties is not None:
        goals = aet_pair or (paren_pairs[0] if paren_pairs else None)
        halftime = paren_pairs[1] if len(paren_pairs) > 1 else None
        status = "PEN"
        extratime = aet_pair
    elif has_aet:
        goals = main
        halftime = paren_pairs[0] if paren_pairs else None
        status = "AET"
        extratime = main
    else:
        goals = main
        halftime = paren_pairs[0] if paren_pairs else None
        status = "FT"
        extratime = None

    return {
        "goals": goals,
        "status": status,
        "score": {
            "halftime": {"home": halftime[0], "away": halftime[1]} if halftime else None,
            "fulltime": {"home": goals[0], "away": goals[1]} if goals else None,
            "extratime": {"home": extratime[0], "away": extratime[1]} if extratime else None,
            "penalty_shootout": {
                "home": penalties[0],
                "away": penalties[1],
            }
            if penalties
            else None,
        },
        "penalties": penalties,
        "score_start": match.start(),
    }


def parse_football_txt(
    text: str,
    *,
    season: int,
    calendar_year: int | None = None,
    international: bool = False,
) -> dict[str, Any]:
    """Parse Football.TXT into source-level match facts without team identities."""
    title = None
    declared_matches = None
    declared_teams = None
    stage = None
    current_date = None
    current_time = None
    current_year = calendar_year
    matches: list[dict[str, Any]] = []
    rejected = 0

    for raw_line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("=") and title is None:
            title = stripped.lstrip("=").strip()
            continue
        header = re.match(r"^#\s*Matches\s+(\d+)\s*$", stripped, re.IGNORECASE)
        if header:
            declared_matches = int(header.group(1))
            continue
        header = re.match(r"^#\s*Teams\s+(\d+)\s*$", stripped, re.IGNORECASE)
        if header:
            declared_teams = int(header.group(1))
            continue
        if stripped.startswith("#"):
            continue
        if stripped[0] in "▪•":
            stage = stripped.lstrip("▪• ").strip() or None
            current_time = None
            continue
        date_match = _DATE_LINE_RE.match(line)
        if date_match:
            month = _MONTHS[date_match.group("month").lower()[:3]]
            day = int(date_match.group("day"))
            explicit_year = date_match.group("year")
            if explicit_year:
                year = int(explicit_year)
            elif calendar_year is not None:
                year = int(calendar_year)
            else:
                year = int(season) if month >= 7 else int(season) + 1
            try:
                current_date = datetime(year, month, day).date().isoformat()
            except ValueError:
                current_date = None
            current_year = year
            current_time = None
            continue

        if current_date is None:
            continue
        if stripped.startswith("(") or stripped.startswith("["):
            continue

        if international:
            intl = _INTL_LINE_RE.match(line)
            if not intl:
                continue
            by = int(intl.group("hg"))
            ay = int(intl.group("ag"))
            venue = intl.group("venue")
            bracket_match = re.search(r"\[([^\]]+)\]", line)
            shootout_winner = None
            if bracket_match:
                winner_match = _PEN_WINNER_RE.search(bracket_match.group(0))
                if winner_match:
                    shootout_winner = winner_match.group("winner").strip()
            status = "PEN" if shootout_winner else "FT"
            matches.append(
                {
                    "date": current_date,
                    "time": None,
                    "home": intl.group("home").strip(),
                    "away": intl.group("away").strip(),
                    "goals": (by, ay),
                    "status": status,
                    "score": {
                        "halftime": None,
                        "fulltime": {"home": by, "away": ay},
                        "extratime": None,
                        "penalty_shootout": None,
                    },
                    "penalties": None,
                    "shootout_winner": shootout_winner,
                    "venue": venue.strip() if venue else None,
                    "stage": stage,
                    "calendar_year": current_year,
                }
            )
            continue

        time_match = re.match(r"^(\d{1,2}:\d{2})\s+(.*)$", stripped)
        if time_match:
            current_time = time_match.group(1)
            body = time_match.group(2).strip()
        else:
            body = stripped
        if " v " not in body:
            continue
        left, right = body.split(" v ", 1)
        home = left.strip()
        parsed = _parse_txt_match_result(right)
        if not home or parsed is None:
            rejected += 1
            continue
        away = right[: parsed["score_start"]].strip()
        if not away:
            rejected += 1
            continue
        penalties = parsed["penalties"]
        shootout_winner = None
        if penalties:
            shootout_winner = home if penalties[0] > penalties[1] else away
            shootout_winner = re.sub(r"\s+\([A-Z]{2,4}\)\s*$", "", shootout_winner).strip()
        matches.append(
            {
                "date": current_date,
                "time": current_time,
                "home": home,
                "away": away,
                "goals": parsed["goals"],
                "status": parsed["status"],
                "score": parsed["score"],
                "penalties": penalties,
                "shootout_winner": shootout_winner,
                "venue": None,
                "stage": stage,
                "calendar_year": current_year,
            }
        )

    return {
        "title": title,
        "declared_matches": declared_matches,
        "declared_teams": declared_teams,
        "matches": matches,
        "rejected": rejected,
    }


def _txt_repository_details(repository: str) -> tuple[str, str]:
    return (
        TXT_REPOSITORIES.get(repository, f"https://github.com/openfootball/{repository}"),
        TXT_RAW_BASES.get(repository, f"https://raw.githubusercontent.com/openfootball/{repository}/master"),
    )


def _normalise_txt_matches(
    parsed: dict[str, Any],
    *,
    provider: str,
    league_id: int,
    season: int,
    league_name: str,
    url: str,
    repository: str,
    file_name: str,
    source_commit: str | None,
    retrieved_at: str,
    calendar_year: int | None = None,
    international: bool = False,
    completed_only: bool = True,
) -> tuple[list[dict[str, Any]], int, int]:
    fixtures: list[dict[str, Any]] = []
    team_cache: dict[str, str | None] = {}
    skipped_incomplete = 0
    rejected = 0
    for index, match in enumerate(parsed.get("matches") or []):
        status = str(match.get("status") or "FT")
        if completed_only and status not in ("FT", "AET", "PEN"):
            skipped_incomplete += 1
            continue
        fixture = _build_fixture(
            provider=provider,
            league_id=int(league_id),
            season=int(season),
            league_name=league_name,
            source_url=url,
            repository=repository,
            source_file=file_name,
            source_commit=source_commit,
            retrieved_at=retrieved_at,
            source_match_count=parsed.get("declared_matches"),
            fixture_id_parts=(
                index,
                match.get("stage"),
                match.get("date"),
                match.get("home"),
                match.get("away"),
            ),
            date_value=match["date"],
            local_time=match.get("time"),
            home_name=match["home"],
            away_name=match["away"],
            goals=match.get("goals"),
            status_short=status,
            score=match.get("score") or {},
            round_name=match.get("stage"),
            stage=match.get("stage"),
            team_cache=team_cache,
            extra_provenance={
                "source_title": parsed.get("title"),
                "source_declared_team_count": parsed.get("declared_teams"),
                "shootout_winner": match.get("shootout_winner"),
                "venue": match.get("venue"),
                "source_match_index": index,
            },
            strip_country_suffix=(int(league_id) in (2, 3)),
            calendar_year=match.get("calendar_year") or calendar_year,
        )
        if fixture is None:
            rejected += 1
            continue
        fixtures.append(fixture)
    return fixtures, skipped_incomplete, rejected


def get_txt_games(
    league_id: int,
    season: int,
    *,
    timeout: float = 20.0,
    completed_only: bool = True,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Fetch a club Football.TXT competition with a configured league id."""
    source = TXT_COMPETITIONS.get(int(league_id))
    if not source:
        return "UNSUPPORTED_COVERAGE", [], {
            "status": "UNSUPPORTED_COVERAGE",
            "error_category": "unsupported_coverage",
            "reason": "no_openfootball_txt_mapping",
            "source": TXT_PROVIDER,
        }
    allowed = source.get("edition_seasons")
    if allowed and int(season) not in set(allowed):
        return "UNSUPPORTED_COVERAGE", [], {
            "status": "UNSUPPORTED_COVERAGE",
            "error_category": "unsupported_coverage",
            "reason": "openfootball_txt_unverified_edition",
            "source": TXT_PROVIDER,
            "requested_season": int(season),
        }
    season_key = _season_key(season)
    repository, raw_base = _txt_repository_details(source["repository"])
    url = f"{raw_base}/{season_key}/{source['file']}"
    fetch_status, body, meta = _http_text(url, timeout=timeout)
    meta.update(
        {
            "source": TXT_PROVIDER,
            "repository": repository,
            "source_file": source["file"],
            "requested_season": int(season),
            "season_key": season_key,
        }
    )
    if fetch_status != "SOURCE_AVAILABLE":
        return fetch_status, [], meta
    parsed = parse_football_txt(body, season=int(season))
    if not _identity_matches(parsed.get("title"), source["tokens"]):
        return "PARSER_ERROR", [], {
            **meta,
            "status": "PARSER_ERROR",
            "error_category": "parsing_error",
            "error": f"competition identity mismatch: {parsed.get('title')!r}",
            "source_title": parsed.get("title"),
        }
    if not parsed.get("matches"):
        return "SOURCE_EMPTY", [], {
            **meta,
            "status": "SOURCE_EMPTY",
            "source_title": parsed.get("title"),
            "error": "no parseable matches",
            "rejected_matches": parsed.get("rejected", 0),
        }
    commit = _source_commit(repository)
    fixtures, skipped, rejected = _normalise_txt_matches(
        parsed,
        provider=TXT_PROVIDER,
        league_id=int(league_id),
        season=int(season),
        league_name=source["name"],
        url=url,
        repository=repository,
        file_name=source["file"],
        source_commit=commit,
        retrieved_at=meta["retrieved_at"],
        completed_only=completed_only,
    )
    return "SOURCE_AVAILABLE", fixtures, {
        **meta,
        "status": "SOURCE_AVAILABLE",
        "source_title": parsed.get("title"),
        "source_declared_match_count": parsed.get("declared_matches"),
        "source_declared_team_count": parsed.get("declared_teams"),
        "fixtures_returned": len(fixtures),
        "skipped_incomplete_matches": skipped,
        "rejected_matches": rejected + int(parsed.get("rejected") or 0),
        "completed_only": bool(completed_only),
        "source_commit": commit,
    }


def _international_source_year(league_id: int, season: int, source: dict[str, Any]) -> int:
    overrides = source.get("source_year_overrides") or {}
    return int(overrides.get(int(season), int(season)))


def get_international_games(
    league_id: int,
    season: int,
    *,
    timeout: float = 20.0,
    completed_only: bool = True,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Fetch men's full international results for one tournament edition/year."""
    source = INTERNATIONAL_SOURCES.get(int(league_id))
    if not source:
        return "UNSUPPORTED_COVERAGE", [], {
            "status": "UNSUPPORTED_COVERAGE",
            "error_category": "unsupported_coverage",
            "reason": "no_openfootball_internationals_mapping",
            "source": INTERNATIONAL_PROVIDER,
        }
    allowed = source.get("edition_seasons")
    if allowed and int(season) not in set(allowed):
        return "UNSUPPORTED_COVERAGE", [], {
            "status": "UNSUPPORTED_COVERAGE",
            "error_category": "unsupported_coverage",
            "reason": "openfootball_internationals_unverified_edition",
            "source": INTERNATIONAL_PROVIDER,
            "requested_season": int(season),
        }
    source_year = _international_source_year(int(league_id), int(season), source)
    file_name = f"{source_year}_{source['slug']}.txt"
    url = f"{INTERNATIONALS_BASE}/{source['directory']}/{file_name}"
    fetch_status, body, meta = _http_text(url, timeout=timeout)
    meta.update(
        {
            "source": INTERNATIONAL_PROVIDER,
            "repository": INTERNATIONALS_REPOSITORY,
            "source_file": file_name,
            "requested_season": int(season),
            "source_year": source_year,
            "edition_identity_preserved": source_year != int(season),
        }
    )
    if fetch_status != "SOURCE_AVAILABLE":
        return fetch_status, [], meta
    parsed = parse_football_txt(
        body,
        season=int(season),
        calendar_year=source_year,
        international=True,
    )
    if not _identity_matches(parsed.get("title"), source["tokens"]):
        return "PARSER_ERROR", [], {
            **meta,
            "status": "PARSER_ERROR",
            "error_category": "parsing_error",
            "error": f"competition identity mismatch: {parsed.get('title')!r}",
            "source_title": parsed.get("title"),
        }
    if not parsed.get("matches"):
        return "SOURCE_EMPTY", [], {
            **meta,
            "status": "SOURCE_EMPTY",
            "source_title": parsed.get("title"),
            "error": "no parseable matches",
            "rejected_matches": parsed.get("rejected", 0),
        }
    commit = _source_commit(INTERNATIONALS_REPOSITORY)
    fixtures, skipped, rejected = _normalise_txt_matches(
        parsed,
        provider=INTERNATIONAL_PROVIDER,
        league_id=int(league_id),
        season=int(season),
        league_name=source["name"],
        url=url,
        repository=INTERNATIONALS_REPOSITORY,
        file_name=file_name,
        source_commit=commit,
        retrieved_at=meta["retrieved_at"],
        calendar_year=source_year,
        international=True,
        completed_only=completed_only,
    )
    return "SOURCE_AVAILABLE", fixtures, {
        **meta,
        "status": "SOURCE_AVAILABLE",
        "source_title": parsed.get("title"),
        "source_declared_match_count": parsed.get("declared_matches"),
        "source_declared_team_count": parsed.get("declared_teams"),
        "fixtures_returned": len(fixtures),
        "skipped_incomplete_matches": skipped,
        "rejected_matches": rejected + int(parsed.get("rejected") or 0),
        "completed_only": bool(completed_only),
        "source_commit": commit,
        "limitations": [
            "Men's full internationals only.",
            "Full-time results include extra time but not penalty-shootout scores.",
            "Shootout information identifies the winner, not the shootout score.",
            "No reliable abandoned, annulled or awarded status.",
            "Stage/round attribution is incomplete.",
        ],
    }


def inspect_txt_source(
    source_key: str,
    *,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Parse one diagnostic-only exact Football.TXT file without storing it."""
    source = DIAGNOSTIC_TXT_FILES.get(str(source_key))
    if not source:
        return {"source_key": source_key, "status": "UNSUPPORTED_COVERAGE"}
    season = int(source["season"])
    season_key = _season_key(season)
    repository, raw_base = _txt_repository_details(source["repository"])
    file_name = source["file"]
    url = f"{raw_base}/{file_name}" if "/" in file_name else f"{raw_base}/{season_key}/{file_name}"
    status, body, meta = _http_text(url, timeout=timeout)
    result = {
        **meta,
        "source_key": source_key,
        "source": TXT_PROVIDER,
        "repository": repository,
        "source_file": file_name,
        "requested_season": season,
        "source_url": url,
        "status": status,
    }
    if status != "SOURCE_AVAILABLE":
        return result
    parsed = parse_football_txt(
        body,
        season=season,
        calendar_year=int(source.get("calendar_year") or season),
    )
    result.update(
        {
            "source_title": parsed.get("title"),
            "identity_verified": _identity_matches(parsed.get("title"), source["tokens"]),
            "declared_match_count": parsed.get("declared_matches"),
            "parsed_match_count": len(parsed.get("matches") or []),
            "declared_team_count": parsed.get("declared_teams"),
            "rejected_match_count": parsed.get("rejected", 0),
            "storage_enabled": False,
            "storage_disabled_reason": "no authoritative configured competition id",
        }
    )
    return result


def provider_identity_report() -> dict[str, Any]:
    """Return the declared coverage scope without claiming universal support."""
    return {
        "json": {
            "repository": FOOTBALL_JSON_REPOSITORY,
            "league_ids": sorted(JSON_COMPETITIONS),
            "champions_league_enabled_seasons": sorted(
                JSON_COMPETITIONS[2].get("edition_seasons") or []
            ),
            "limitations": [
                "One file per competition-season; absence is not universal coverage.",
                "A non-empty file is not complete-season evidence.",
                "Date-only source precision is preserved; no UTC kickoff is inferred.",
            ],
        },
        "txt": {
            "repositories": dict(TXT_REPOSITORIES),
            "league_ids": sorted(TXT_COMPETITIONS),
            "diagnostic_only_files": sorted(DIAGNOSTIC_TXT_FILES),
            "limitations": [
                "Only competitions with an existing authoritative id are enabled.",
                "No Conference League, FA Cup, EFL Cup, Coppa Italia, Libertadores or Brasileirao id is invented.",
            ],
        },
        "internationals": {
            "repository": INTERNATIONALS_REPOSITORY,
            "league_ids": sorted(INTERNATIONAL_SOURCES),
            "source_year_overrides": {
                "4": {"2020": 2021},
            },
            "limitations": [
                "Men's full internationals only.",
                "Penalty shootout winner is retained; the score is not treated as the match score.",
                "Awards/abandonments/annulments are not reliably represented.",
                "International results do not independently prove tournament completion.",
            ],
        },
    }
