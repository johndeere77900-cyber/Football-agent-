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

    # Lowercase, expand ampersands to 'and', replace underscores with space
    text = no_accents.lower().replace("_", " ").replace("&", " and ")

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


INVALID_IDENTITY_PROVIDERS = {"mixed", "reconciled", "unknown"}


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
       Validates canonical ID, sport, provider namespace, normalized incoming team name, and league context.
    2. Check exact normalized_name lookup in DB (`team_identities`).
    3. Check competition-aware alias in `config.CANONICAL_TEAM_ALIASES`.
    4. Check national team variant matching ('Nigeria National Team' -> 'Nigeria').
    5. Check safe stripped name within same league_id context.
    6. If auto_register is True, generate a new canonical identity.
       Otherwise, if unresolved, FAIL CLOSED and return None.
    """
    provider = str(provider).strip().lower() if provider else ""
    if provider in INVALID_IDENTITY_PROVIDERS or not provider:
        return None

    p_team_id_str = None
    if provider_team_id is not None and str(provider_team_id).strip().lower() not in ("", "none", "nan"):
        p_team_id_str = str(provider_team_id).strip()

    norm_name = normalize_team_name(raw_name) if (raw_name and isinstance(raw_name, str)) else ""

    # Step 1: Check existing verified provider mapping in DB (only if provider team ID is present)
    if p_team_id_str:
        existing = storage.get_team_identity_by_provider(sport, provider, p_team_id_str)
        if existing and isinstance(existing, dict) and existing.get("canonical_id"):
            stored_norm = existing.get("normalized_name", "")
            stored_canonical = existing.get("canonical_id")
            stored_league = existing.get("league_id")

            if norm_name:
                is_name_match = (
                    norm_name == stored_norm
                    or safe_stripped_team_name(raw_name) == safe_stripped_team_name(stored_norm)
                    or strip_national_team_words(raw_name) == strip_national_team_words(stored_norm)
                )

                if not is_name_match:
                    alias_dict = getattr(config, "CANONICAL_TEAM_ALIASES", {})
                    for alias_key, target_val in alias_dict.items():
                        target_cid = f"{sport}_team_{sanitize_canonical_slug(target_val)}"
                        if target_cid == stored_canonical:
                            if isinstance(alias_key, tuple) and len(alias_key) == 2:
                                k_name, k_league = alias_key
                                if normalize_team_name(str(k_name)) == norm_name and (league_id is None or k_league == league_id):
                                    is_name_match = True
                                    break
                            elif isinstance(alias_key, str):
                                if normalize_team_name(alias_key) == norm_name:
                                    is_name_match = True
                                    break

                context_conflict = (
                    league_id is not None
                    and stored_league is not None
                    and league_id != stored_league
                    and not is_name_match
                )

                if is_name_match and not context_conflict:
                    return stored_canonical
                elif not is_name_match:
                    # Name conflict against stored provider team ID -> FAIL CLOSED
                    return None
            else:
                return stored_canonical

    if not norm_name:
        return None

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
        for alias_key, target_val in alias_dict.items():
            if isinstance(alias_key, tuple) and len(alias_key) == 2:
                key_name, key_league = alias_key
                if normalize_team_name(str(key_name)) == norm_name and (league_id is None or key_league == league_id):
                    canonical_alias = target_val
                    break
            elif isinstance(alias_key, str):
                if normalize_team_name(alias_key) == norm_name:
                    canonical_alias = target_val
                    break

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

    # Persist newly resolved mapping ONLY if auto_register is True and real provider team ID exists
    if p_team_id_str and auto_register:
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
    if provider in INVALID_IDENTITY_PROVIDERS:
        return None

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
