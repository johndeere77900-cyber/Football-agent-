"""
Canonical Team Identity Resolution Layer.

Provides deterministic team identity matching across providers (api_football, football_data_org, soccerdata, etc.)
without assuming provider numeric IDs are interchangeable or using unsafe fuzzy matching.

Resolution Priority:
1. Existing explicit provider mapping in persistent storage (`team_identities`).
2. Exact normalized team name (+ optional competition context `league_id`).
3. Explicit configured canonical alias match (`config.CANONICAL_TEAM_ALIASES`).
4. National team suffix normalization (e.g., 'Nigeria National Team' -> 'Nigeria').
5. Safe normalized name match (strip FC/CF/BSC noise words within same league_id).
6. Fail closed (return None) when ambiguous or unmapped.
"""

import re
import unicodedata
import config
import storage


def normalize_team_name(name: str) -> str:
    """
    Normalize team name for deterministic comparison.

    Performs:
    - Unicode normalization (NFD / strip accents)
    - Lowercase conversion
    - Punctuation removal
    - Whitespace collapse
    """
    if not isinstance(name, str) or not name.strip():
        return ""

    # NFD decomposition to separate base letters and diacritics
    nfkd = unicodedata.normalize("NFKD", name)
    no_accents = "".join([c for c in nfkd if not unicodedata.combining(c)])

    # Lowercase and replace underscores/punctuation with space
    text = no_accents.lower().replace("_", " ")

    # Replace remaining punctuation / non-alphanumeric with space
    text = re.sub(r"[^\w\s]", " ", text)

    # Collapse multiple whitespace
    tokens = text.split()
    return " ".join(tokens)


def sanitize_canonical_slug(name: str) -> str:
    """Generate a clean slug identifier from team name."""
    norm = normalize_team_name(name)
    if not norm:
        return "unknown"
    return re.sub(r"\s+", "_", norm)


def safe_stripped_team_name(name: str) -> str:
    """
    Strip common noise tokens (fc, cf, bsc, afc, sv, sc) from normalized team name.
    Only used within strict competition context (same league_id).
    """
    norm = normalize_team_name(name)
    if not norm:
        return ""

    tokens = norm.split()
    noise_tokens = {"fc", "cf", "bsc", "afc", "sv", "sc", "fk", "sk", "cd", "ud", "rcd", "rc", "as", "ss", "us"}
    filtered = [t for t in tokens if t not in noise_tokens]
    if not filtered:
        return norm
    return " ".join(filtered)


def strip_national_team_words(name: str) -> str:
    """
    Strip national team suffix words (national team, football team, national, team)
    to reconcile variants like 'Nigeria National Team' -> 'Nigeria'.
    """
    norm = normalize_team_name(name)
    if not norm:
        return ""

    suffixes = [" national team", " football team", " national", " team"]
    for suff in suffixes:
        if norm.endswith(suff) and len(norm) > len(suff):
            norm = norm[:-len(suff)].strip()
            break
    return norm


def resolve_canonical_team_id(
    raw_name: str,
    provider: str,
    provider_team_id: str | int,
    league_id: int = None,
    sport: str = "football",
    auto_register: bool = False,
) -> str | None:
    """
    Resolve canonical team identity deterministically. FAIL CLOSED if unresolved.

    Order:
    1. Check existing verified provider mapping in DB (`team_identities`).
    2. Check exact normalized_name lookup in DB (`team_identities`).
    3. Check competition-aware alias in `config.CANONICAL_TEAM_ALIASES`.
    4. Check national team variant matching ('Nigeria National Team' -> 'Nigeria').
    5. Check safe stripped name within same league_id context.
    6. If auto_register is True, generate a new canonical identity.
       Otherwise, if unresolved, FAIL CLOSED and return None.
    """
    if not raw_name or not isinstance(raw_name, str) or not raw_name.strip():
        return None

    provider = str(provider).strip().lower()
    p_team_id_str = None
    if provider_team_id is not None and str(provider_team_id).strip().lower() not in ("", "none", "nan"):
        p_team_id_str = str(provider_team_id).strip()

    norm_name = normalize_team_name(raw_name)
    if not norm_name:
        return None

    # Step 1: Existing verified provider mapping in DB (only if provider team ID is present)
    if p_team_id_str:
        existing = storage.get_team_identity_by_provider(sport, provider, p_team_id_str)
        if existing:
            return existing["canonical_id"]

    # Step 2: Exact normalized_name lookup in DB
    target_canonical_id = None
    matches = storage.get_team_identities_by_normalized_name(sport, norm_name, league_id)
    if matches:
        candidate_ids = {m["canonical_id"] for m in matches}
        if len(candidate_ids) == 1:
            target_canonical_id = list(candidate_ids)[0]

    # Step 3: Competition-aware alias check in config
    if not target_canonical_id:
        alias_dict = getattr(config, "CANONICAL_TEAM_ALIASES", {})
        canonical_alias = None
        if league_id and (norm_name, league_id) in alias_dict:
            canonical_alias = alias_dict[(norm_name, league_id)]
        elif norm_name in alias_dict:
            canonical_alias = alias_dict[norm_name]

        if canonical_alias:
            target_canonical_id = f"{sport}_team_{sanitize_canonical_slug(canonical_alias)}"

    # Step 4: National team suffix stripping check (e.g. 'Nigeria National Team' -> 'Nigeria')
    if not target_canonical_id:
        national_stripped = strip_national_team_words(raw_name)
        if national_stripped and national_stripped != norm_name:
            nat_matches = storage.get_team_identities_by_normalized_name(sport, national_stripped, None)
            if nat_matches:
                candidate_ids = {m["canonical_id"] for m in nat_matches}
                if len(candidate_ids) == 1:
                    target_canonical_id = list(candidate_ids)[0]
            elif national_stripped in getattr(config, "CANONICAL_TEAM_ALIASES", {}):
                alias = config.CANONICAL_TEAM_ALIASES[national_stripped]
                target_canonical_id = f"{sport}_team_{sanitize_canonical_slug(alias)}"

    # Step 5: Safe stripped name match within same league_id
    if not target_canonical_id and league_id:
        stripped_name = safe_stripped_team_name(raw_name)
        if stripped_name and stripped_name != norm_name:
            stripped_matches = storage.get_team_identities_by_normalized_name(sport, stripped_name, league_id)
            if stripped_matches:
                candidate_ids = {m["canonical_id"] for m in stripped_matches}
                if len(candidate_ids) == 1:
                    target_canonical_id = list(candidate_ids)[0]

    # Step 6: Auto-register if explicitly requested
    if not target_canonical_id and auto_register:
        clean_base = strip_national_team_words(raw_name) or raw_name
        slug = sanitize_canonical_slug(clean_base)
        if league_id:
            target_canonical_id = f"{sport}_team_{league_id}_{slug}"
        else:
            target_canonical_id = f"{sport}_team_{slug}"

    # FAIL CLOSED: If no trusted canonical identity is resolved, return None
    if not target_canonical_id:
        return None

    # Persist the newly resolved mapping ONLY if real provider team ID exists
    if p_team_id_str:
        storage.save_team_identity(
            sport=sport,
            canonical_id=target_canonical_id,
            provider=provider,
            provider_team_id=p_team_id_str,
            normalized_name=norm_name,
            display_name=raw_name.strip(),
            league_id=league_id,
        )

    return target_canonical_id


def bootstrap_historical_team_identity(
    raw_name: str,
    provider: str,
    provider_team_id: str | int,
    league_id: int = None,
    sport: str = "football",
) -> str | None:
    """
    Controlled historical canonical team identity bootstrap mechanism.

    Used ONLY during trusted historical ingestion.
    Supports international and domestic team identity bootstrapping.
    """
    if not raw_name or not isinstance(raw_name, str) or not raw_name.strip():
        return None

    provider = str(provider).strip().lower()
    p_team_id_str = None
    if provider_team_id is not None and str(provider_team_id).strip().lower() not in ("", "none", "nan"):
        p_team_id_str = str(provider_team_id).strip()

    norm_name = normalize_team_name(raw_name)
    if not norm_name:
        return None

    # First check if resolution succeeds via existing verified mapping or alias
    resolved = resolve_canonical_team_id(
        raw_name=raw_name,
        provider=provider,
        provider_team_id=p_team_id_str,
        league_id=league_id,
        sport=sport,
        auto_register=False,
    )
    if resolved:
        return resolved

    # Otherwise, generate controlled canonical identity using base team slug
    clean_base = strip_national_team_words(raw_name) or raw_name
    slug = sanitize_canonical_slug(clean_base)
    if league_id:
        canonical_id = f"{sport}_team_{league_id}_{slug}"
    else:
        canonical_id = f"{sport}_team_{slug}"

    # Save provider mapping ONLY if a real provider team ID exists
    if p_team_id_str:
        storage.save_team_identity(
            sport=sport,
            canonical_id=canonical_id,
            provider=provider,
            provider_team_id=p_team_id_str,
            normalized_name=norm_name,
            display_name=raw_name.strip(),
            league_id=league_id,
        )

    return canonical_id
