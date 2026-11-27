"""Central provider health, failure classification and isolation.

This module is deliberately independent from ``provider_registry`` capability
verification:

* capability status answers "may this provider be routed for this field?";
* health status answers "is this provider responding right now?".

A provider can be VERIFIED for a capability and temporarily isolated for
health reasons. Neither state is inferred from the other.

The health ledger is persisted through the existing ``storage._connect()``
abstraction so the same code works against local SQLite and PostgreSQL. It is
append-safe and bounded: repeated failures increase a consecutive counter, and
once the circuit opens the provider is skipped until ``isolation_until``.

Failure handling is explicit. A 401/403 is not retried like a transient
network error, and an empty response is never treated as proof that the
underlying dataset is empty.
"""

from __future__ import annotations

import datetime as _dt
import random
import re
import time
from typing import Any, Callable, Dict, Optional, Tuple

import storage

# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------

NETWORK_TIMEOUT = "network_timeout"
CONNECTION_FAILURE = "connection_failure"
HTTP_401 = "http_401"
HTTP_403 = "http_403"
HTTP_404 = "http_404"
HTTP_429 = "http_429"
HTTP_5XX = "http_5xx"
INVALID_JSON = "invalid_json"
UNEXPECTED_SCHEMA = "unexpected_schema"
MISSING_REQUIRED_FIELD = "missing_required_field"
EMPTY_RESPONSE = "empty_response"
NO_MATCHING_FIXTURE = "no_matching_fixture"
UNSUPPORTED_COMPETITION = "unsupported_competition"
UNSUPPORTED_SEASON = "unsupported_season"
EXPIRED_CREDENTIALS = "expired_credentials"
PARSER_FAILURE = "parser_failure"
IDENTITY_MISMATCH = "identity_mismatch"
CONFIGURATION_ERROR = "configuration_error"
UNKNOWN = "unknown"

# Failures that must not be retried inside one request.
PERMANENT_FAILURES = {
    HTTP_401,
    HTTP_403,
    HTTP_404,
    EXPIRED_CREDENTIALS,
    CONFIGURATION_ERROR,
    UNSUPPORTED_COMPETITION,
    UNSUPPORTED_SEASON,
    IDENTITY_MISMATCH,
}

# Failures that say nothing about dataset emptiness.
EMPTY_NOT_PROOF_FAILURES = {
    HTTP_401,
    HTTP_403,
    HTTP_404,
    HTTP_429,
    HTTP_5XX,
    NETWORK_TIMEOUT,
    CONNECTION_FAILURE,
    INVALID_JSON,
    UNEXPECTED_SCHEMA,
    EMPTY_RESPONSE,
    PARSER_FAILURE,
}

HEALTHY = "healthy"
DEGRADED = "degraded"
ISOLATED = "isolated"
AUTH_FAILED = "authentication_failure"
RATE_LIMITED = "rate_limited"
ACCESS_DENIED = "access_denied"
DISABLED = "disabled_by_policy"


def classify_http_status(status: Optional[int]) -> str:
    if status is None:
        return UNKNOWN
    if status == 401:
        return HTTP_401
    if status == 403:
        return HTTP_403
    if status == 404:
        return HTTP_404
    if status == 429:
        return HTTP_429
    if 500 <= status <= 599:
        return HTTP_5XX
    return UNKNOWN


def classify_exception(exc: BaseException) -> str:
    """Map an exception to a stable, machine-readable failure category."""
    name = type(exc).__name__.lower()
    text = str(exc).lower()

    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if isinstance(status, int):
        category = classify_http_status(status)
        if category != UNKNOWN:
            return category

    # Some adapters raise plain RuntimeError("... HTTP 401 ..."). Classify the
    # status from the message rather than treating a credential failure as a
    # transient unknown error that would be retried.
    match = re.search(r"\b(http\s*)?([45]\d{2})\b", text)
    if match:
        category = classify_http_status(int(match.group(2)))
        if category != UNKNOWN:
            return category

    if isinstance(exc, (TimeoutError,)) or "timeout" in name or "timed out" in text:
        return NETWORK_TIMEOUT
    if isinstance(exc, (ConnectionError,)):
        return CONNECTION_FAILURE
    if "connection" in name or "connection" in text:
        return CONNECTION_FAILURE
    if isinstance(exc, ValueError) and "json" in name:
        return INVALID_JSON
    if "jsondecode" in name or "json decode" in text:
        return INVALID_JSON
    if "keyerror" in name or "missing" in text and "field" in text:
        return MISSING_REQUIRED_FIELD
    if "schema" in name or "schema" in text:
        return UNEXPECTED_SCHEMA
    if "parse" in name:
        return PARSER_FAILURE
    if "not configured" in text or "api key" in text or "credential" in text:
        return CONFIGURATION_ERROR
    return UNKNOWN


_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|apikey|token|secret|authorization)=([^&\s]+)"),
    re.compile(r"(?i)(x-apisports-key|x-rapidapi-key)\s*[:=]\s*\S+"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"),
)


def redact(text: Any, limit: int = 400) -> str:
    """Remove credential-looking fragments before persisting an error."""
    value = str(text or "")
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(lambda m: (m.group(1) + "=REDACTED") if m.lastindex else "REDACTED", value)
    value = value.replace("\n", " ").strip()
    return value[:limit]


# ---------------------------------------------------------------------------
# Persistent health ledger
# ---------------------------------------------------------------------------

_SCHEMA_READY_FOR = set()


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def ensure_schema() -> None:
    conn, db_type = storage._connect()
    if db_type == "postgres":
        key = (db_type, str(getattr(storage.config, "NEON_DATABASE_URL", "")))
    else:
        key = (db_type, str(getattr(storage.config, "DB_PATH", "")))
    if key in _SCHEMA_READY_FOR:
        conn.close()
        return
    try:
        if db_type == "postgres":
            # PostgreSQL schema is owned by the reviewed migration; the
            # application never executes DDL against production.
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.provider_health')")
                if cur.fetchone()[0] is None:
                    raise RuntimeError(
                        "provider_health table is missing. Apply "
                        "migrations/phase2_lifecycle_postgres.sql before running the lifecycle."
                    )
        else:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_health (
                    provider                TEXT PRIMARY KEY,
                    state                   TEXT NOT NULL,
                    consecutive_failures    INTEGER NOT NULL DEFAULT 0,
                    consecutive_successes   INTEGER NOT NULL DEFAULT 0,
                    last_success_at         TEXT,
                    last_failure_at         TEXT,
                    last_failure_category   TEXT,
                    last_error              TEXT,
                    isolation_until         TEXT,
                    updated_at              TEXT NOT NULL
                )
                """
            )
            conn.commit()
    finally:
        conn.close()
    _SCHEMA_READY_FOR.add(key)


def get_health(provider: str) -> Dict[str, Any]:
    ensure_schema()
    conn, db_type = storage._connect()
    ph = "%s" if db_type == "postgres" else "?"
    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT provider, state, consecutive_failures, consecutive_successes, "
                    "last_success_at, last_failure_at, last_failure_category, last_error, "
                    "isolation_until, updated_at FROM provider_health WHERE provider = %s",
                    (provider,),
                )
                row = cur.fetchone()
        else:
            row = conn.execute(
                "SELECT provider, state, consecutive_failures, consecutive_successes, "
                "last_success_at, last_failure_at, last_failure_category, last_error, "
                "isolation_until, updated_at FROM provider_health WHERE provider = ?",
                (provider,),
            ).fetchone()
    finally:
        conn.close()

    if not row:
        return {
            "provider": provider,
            "state": "unknown",
            "consecutive_failures": 0,
            "consecutive_successes": 0,
            "last_success_at": None,
            "last_failure_at": None,
            "last_failure_category": None,
            "last_error": None,
            "isolation_until": None,
        }
    keys = (
        "provider",
        "state",
        "consecutive_failures",
        "consecutive_successes",
        "last_success_at",
        "last_failure_at",
        "last_failure_category",
        "last_error",
        "isolation_until",
    )
    data = dict(zip(keys, row))
    data["updated_at"] = row[-1]
    return data


def record(
    provider: str,
    *,
    ok: bool,
    failure_category: Optional[str] = None,
    error: Optional[Any] = None,
    latency_ms: Optional[float] = None,
    isolation_seconds: int = 300,
    failure_threshold: int = 3,
    observed_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Record one health observation and update the circuit state."""
    ensure_schema()
    stamp = observed_at or _now()
    previous = get_health(provider)
    failures = int(previous.get("consecutive_failures") or 0)
    successes = int(previous.get("consecutive_successes") or 0)
    isolation_until = previous.get("isolation_until")

    if ok:
        state = HEALTHY
        successes += 1
        failures = 0
        isolation_until = None
        last_error = None
        category = None
        last_failure_at = previous.get("last_failure_at")
        last_success_at = stamp
    else:
        category = failure_category or UNKNOWN
        failures += 1
        successes = 0
        last_error = redact(error) if error is not None else None
        last_success_at = previous.get("last_success_at")
        last_failure_at = stamp
        if category in (HTTP_401, EXPIRED_CREDENTIALS, CONFIGURATION_ERROR):
            state = AUTH_FAILED
        elif category in (HTTP_403,):
            state = ACCESS_DENIED
        elif category in (HTTP_429,):
            state = RATE_LIMITED
        elif failures >= failure_threshold:
            state = ISOLATED
        else:
            state = DEGRADED
        if state == ISOLATED:
            isolation_until = (
                _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=isolation_seconds)
            ).isoformat()

    conn, db_type = storage._connect()
    values = (
        provider,
        state,
        failures,
        successes,
        last_success_at,
        last_failure_at,
        category,
        last_error,
        isolation_until,
        stamp,
    )
    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO provider_health (
                        provider, state, consecutive_failures, consecutive_successes,
                        last_success_at, last_failure_at, last_failure_category,
                        last_error, isolation_until, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (provider) DO UPDATE SET
                        state = EXCLUDED.state,
                        consecutive_failures = EXCLUDED.consecutive_failures,
                        consecutive_successes = EXCLUDED.consecutive_successes,
                        last_success_at = EXCLUDED.last_success_at,
                        last_failure_at = EXCLUDED.last_failure_at,
                        last_failure_category = EXCLUDED.last_failure_category,
                        last_error = EXCLUDED.last_error,
                        isolation_until = EXCLUDED.isolation_until,
                        updated_at = EXCLUDED.updated_at
                    """,
                    values,
                )
            conn.commit()
        else:
            conn.execute(
                """
                INSERT INTO provider_health (
                    provider, state, consecutive_failures, consecutive_successes,
                    last_success_at, last_failure_at, last_failure_category,
                    last_error, isolation_until, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (provider) DO UPDATE SET
                    state = excluded.state,
                    consecutive_failures = excluded.consecutive_failures,
                    consecutive_successes = excluded.consecutive_successes,
                    last_success_at = excluded.last_success_at,
                    last_failure_at = excluded.last_failure_at,
                    last_failure_category = excluded.last_failure_category,
                    last_error = excluded.last_error,
                    isolation_until = excluded.isolation_until,
                    updated_at = excluded.updated_at
                """,
                values,
            )
            conn.commit()
    finally:
        conn.close()

    try:
        import provider_registry

        provider_registry.record_health(
            provider,
            ok=ok,
            failure_category=category,
            observed_at=stamp,
            latency_ms=latency_ms,
        )
    except Exception:
        pass

    return {
        "provider": provider,
        "state": state,
        "consecutive_failures": failures,
        "consecutive_successes": successes,
        "last_failure_category": category,
        "isolation_until": isolation_until,
        "updated_at": stamp,
    }


def is_isolated(provider: str, now: Optional[_dt.datetime] = None) -> bool:
    data = get_health(provider)
    until = data.get("isolation_until")
    if not until:
        return False
    try:
        deadline = _dt.datetime.fromisoformat(str(until).replace("Z", "+00:00"))
    except ValueError:
        return False
    return deadline > (now or _dt.datetime.now(_dt.timezone.utc))


def allows_request(provider: str, probe_interval_seconds: int = 600) -> bool:
    """A provider is callable unless isolated or in a permanent auth state.

    Permanent auth/access states are not retried on every cycle, but a bounded
    recovery probe is allowed once ``probe_interval_seconds`` has elapsed so a
    rotated key or lifted block can be picked up without a restart.
    """
    data = get_health(provider)
    if data["state"] == DISABLED:
        return False
    if data["state"] in (AUTH_FAILED, ACCESS_DENIED):
        stamp = data.get("last_failure_at")
        if not stamp:
            return False
        try:
            failed_at = _dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            return False
        elapsed = (_dt.datetime.now(_dt.timezone.utc) - failed_at).total_seconds()
        return elapsed >= max(0, probe_interval_seconds)
    return not is_isolated(provider)


def backoff_seconds(
    attempt: int,
    *,
    base: float = 0.5,
    cap: float = 8.0,
    jitter: float = 0.25,
    retry_after: Optional[float] = None,
    rng: Optional[random.Random] = None,
) -> float:
    """Exponential backoff with additive jitter and bounded retry-after."""
    if retry_after is not None:
        return max(0.0, min(float(retry_after), cap * 4))
    exponent = max(0, attempt)
    delay = min(cap, base * (2 ** exponent))
    jitter_fn = (rng or random).uniform
    return max(0.0, delay * (1.0 + jitter_fn(-jitter, jitter)))


def call_with_retry(
    provider: str,
    func: Callable[[], Any],
    *,
    max_attempts: int = 3,
    base_delay: float = 0.5,
    cap_delay: float = 8.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    record_health: bool = True,
) -> Tuple[Any, Dict[str, Any]]:
    """Call ``func`` with bounded retries and explicit failure classification.

    Returns ``(result, metadata)``. When every attempt fails the final
    exception is re-raised after its health observation is recorded, so callers
    keep their existing error handling. Permanent failures are never retried
    and are never mistaken for an empty dataset.
    """
    meta: Dict[str, Any] = {
        "provider": provider,
        "attempts": [],
        "success": False,
        "failure_category": None,
    }
    last_exc: Optional[BaseException] = None
    for attempt in range(max(1, max_attempts)):
        started = time.monotonic()
        try:
            result = func()
        except BaseException as exc:  # noqa: BLE001 - classification re-raises
            last_exc = exc
            category = classify_exception(exc)
            latency_ms = (time.monotonic() - started) * 1000.0
            meta["attempts"].append(
                {"attempt": attempt + 1, "ok": False, "failure_category": category, "latency_ms": round(latency_ms, 1)}
            )
            meta["failure_category"] = category
            if record_health:
                record(provider, ok=False, failure_category=category, error=exc, latency_ms=latency_ms)
            if category in PERMANENT_FAILURES:
                break
            if attempt + 1 >= max_attempts:
                break
            retry_after = None
            response = getattr(exc, "response", None)
            if response is not None and getattr(response, "headers", None):
                try:
                    retry_after = float(response.headers.get("Retry-After"))
                except (TypeError, ValueError):
                    retry_after = None
            sleep_fn(backoff_seconds(attempt, base=base_delay, cap=cap_delay, retry_after=retry_after))
            continue

        latency_ms = (time.monotonic() - started) * 1000.0
        meta["attempts"].append({"attempt": attempt + 1, "ok": True, "latency_ms": round(latency_ms, 1)})
        meta["success"] = True
        if record_health:
            record(provider, ok=True, latency_ms=latency_ms)
        return result, meta

    assert last_exc is not None
    raise last_exc


def snapshot() -> Dict[str, Any]:
    """Health of every provider that has ever been observed."""
    ensure_schema()
    conn, db_type = storage._connect()
    try:
        if db_type == "postgres":
            with conn.cursor() as cur:
                cur.execute("SELECT provider FROM provider_health ORDER BY provider")
                providers = [row[0] for row in cur.fetchall()]
        else:
            providers = [
                row[0]
                for row in conn.execute("SELECT provider FROM provider_health ORDER BY provider").fetchall()
            ]
    finally:
        conn.close()
    return {
        "generated_at": _now(),
        "providers": {name: get_health(name) for name in providers},
    }
