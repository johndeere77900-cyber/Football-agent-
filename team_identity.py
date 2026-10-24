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
    - Ampersand expansion ('&' -> 'and')
    - Punctuation removal
    - General noise word / 'FC' / 'CF' / 'AFC' suffix stripping
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

    # Strip noise tokens like 'fc', 'cf', 'afc', 'club'
    tokens = text.split()
    noise_tokens = {"fc", "cf", "afc", "sc", "club"}
    filtered = [t for t in tokens if t not in noise_tokens]
    if not filtered:
        filtered = tokens
    return " ".join(filtered)


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


def _get_sport_alias_map(sport: str) -> dict:
    """Select the sport-aware canonical team alias map from config."""
    s_clean = str(sport).strip().lower() if sport else "football"
    if s_clean == "basketball":
        return getattr(config, "BASKETBALL_CANONICAL_TEAM_ALIASES", {})
    return getattr(config, "FOOTBALL_CANONICAL_TEAM_ALIASES", getattr(config, "CANONICAL_TEAM_ALIASES", {}))


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
    3. Check competition-aware alias in sport-aware alias map.
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

    raw_str = str(raw_name).strip() if (raw_name is not None and not isinstance(raw_name, bool)) else ""
    norm_name = normalize_team_name(raw_str) if raw_str else ""

    # Step 1: Check existing verified provider mapping in DB (only if provider team ID is present)
    if p_team_id_str:
        existing = storage.get_team_identity_by_provider(sport, provider, p_team_id_str)
        if existing and isinstance(existing, dict) and existing.get("canonical_id"):
            stored_norm = existing.get("normalized_name", "")
            stored_canonical = existing.get("canonical_id")
            stored_league = existing.get("league_id")

            if norm_name and norm_name != p_team_id_str:
                is_name_match = (
                    norm_name == stored_norm
                    or safe_stripped_team_name(raw_str) == safe_stripped_team_name(stored_norm)
                    or strip_national_team_words(raw_str) == strip_national_team_words(stored_norm)
                )

                if not is_name_match:
                    alias_dict = _get_sport_alias_map(sport)
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

    if not norm_name or norm_name == p_team_id_str:
        # If raw_name was empty or numeric equal to provider_team_id and no stored mapping exists, check if auto_register requested
        if auto_register and p_team_id_str:
            canonical_id = f"{sport}_team_{league_id}_{p_team_id_str}" if league_id else f"{sport}_team_{p_team_id_str}"
            storage.save_team_identity(
                sport=sport,
                canonical_id=canonical_id,
                provider=provider,
                provider_team_id=p_team_id_str,
                normalized_name=p_team_id_str,
                display_name=f"Team {p_team_id_str}",
                league_id=league_id,
            )
            return canonical_id
        return None

    # Step 2: Exact normalized_name lookup in DB
    target_canonical_id = None
    clean_raw_name = raw_name.replace(f"{sport}_team_", "").replace("football_team_", "").replace("basketball_team_", "") if isinstance(raw_name, str) else ""
    clean_norm_name = normalize_team_name(clean_raw_name) if clean_raw_name else ""

    matches = storage.get_team_identities_by_normalized_name(sport, norm_name, league_id)
    if not matches and clean_norm_name and clean_norm_name != norm_name:
        matches = storage.get_team_identities_by_normalized_name(sport, clean_norm_name, league_id)

    if matches:
        candidate_ids = {m["canonical_id"] for m in matches}
        if len(candidate_ids) == 1:
            target_canonical_id = list(candidate_ids)[0]

    # Step 3: Competition-aware alias check in config
    if not target_canonical_id:
        alias_dict = _get_sport_alias_map(sport)
        canonical_alias = None
        for alias_key, target_val in alias_dict.items():
            if isinstance(alias_key, tuple) and len(alias_key) == 2:
                key_name, key_league = alias_key
                k_norm = normalize_team_name(str(key_name))
                if (k_norm == norm_name or (clean_norm_name and k_norm == clean_norm_name)) and (league_id is None or key_league == league_id):
                    canonical_alias = target_val
                    break
            elif isinstance(alias_key, str):
                k_norm = normalize_team_name(alias_key)
                if k_norm == norm_name or (clean_norm_name and k_norm == clean_norm_name):
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
            else:
                alias_dict = _get_sport_alias_map(sport)
                if national_stripped in alias_dict:
                    alias = alias_dict[national_stripped]
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

    # FAIL CLOSED: If no trusted canonical identity is resolved via steps 1-5, return None.
    # Unknown or ambiguous team names MUST NOT automatically generate synthetic canonical IDs.
    if not target_canonical_id:
        import logging
        logging.getLogger(__name__).warning(
            "Unresolved canonical team identity for raw_name=%r, provider=%r, provider_team_id=%r, league_id=%r",
            raw_name, provider, provider_team_id, league_id
        )
        return None

    # Save provider mapping into DB ONLY if auto_register is True, target_canonical_id was resolved, and provider ID exists
    if p_team_id_str and target_canonical_id and auto_register:
        storage.save_team_identity(
            sport=sport,
            canonical_id=target_canonical_id,
            provider=provider,
            provider_team_id=p_team_id_str,
            normalized_name=norm_name,
            display_name=raw_str or f"Team {p_team_id_str}",
            league_id=league_id,
        )

    return target_canonical_id


def register_canonical_team_identity(
    canonical_id: str,
    display_name: str,
    provider: str,
    provider_team_id: str | int,
    league_id: int = None,
    sport: str = "football",
) -> str:
    """
    Explicitly register a new canonical team identity with verified provider mapping.
    Used for controlled competition initialization and explicit seeding.
    """
    provider = str(provider).strip().lower()
    p_team_id_str = str(provider_team_id).strip()
    norm_name = normalize_team_name(display_name)

    storage.save_team_identity(
        sport=sport,
        canonical_id=canonical_id,
        provider=provider,
        provider_team_id=p_team_id_str,
        normalized_name=norm_name,
        display_name=display_name,
        league_id=league_id,
    )
    return canonical_id


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
    First resolves against existing DB mappings, exact DB normalized names, and CANONICAL_TEAM_ALIASES.
    If an existing canonical ID matches, reuses that canonical ID across providers without creating duplicate IDs.
    Otherwise, generates a controlled canonical ID and saves the provider mapping to DB.
    """
    if not raw_name or not isinstance(raw_name, str) or not raw_name.strip():
        return None

    sport = str(sport).strip().lower() if sport else "football"
    provider = str(provider).strip().lower() if provider else ""
    if provider in INVALID_IDENTITY_PROVIDERS or not provider:
        return None

    p_team_id_str = None
    if provider_team_id is not None and str(provider_team_id).strip().lower() not in ("", "none", "nan"):
        p_team_id_str = str(provider_team_id).strip()

    raw_str = str(raw_name).strip() if (raw_name is not None and not isinstance(raw_name, bool)) else ""
    norm_name = normalize_team_name(raw_str) if raw_str else ""
    if not norm_name and not p_team_id_str:
        return None

    # Step 1: Preserve existing identity resolution first
    resolved = resolve_canonical_team_id(
        raw_name=raw_name,
        provider=provider,
        provider_team_id=p_team_id_str,
        league_id=league_id,
        sport=sport,
        auto_register=False,
    )

    if resolved:
        if p_team_id_str:
            storage.save_team_identity(
                sport=sport,
                canonical_id=resolved,
                provider=provider,
                provider_team_id=p_team_id_str,
                normalized_name=norm_name,
                display_name=raw_str or f"Team {p_team_id_str}",
                league_id=league_id,
            )
        return resolved

    # Step 2: When existing resolution fails (unknown team), create and persist a deterministic canonical identity
    if not norm_name:
        return None

    # Check for name matches strictly within target league context
    matches = storage.get_team_identities_by_normalized_name(sport, norm_name, league_id)

    if matches:
        candidate_ids = {m["canonical_id"] for m in matches if isinstance(m, dict) and m.get("canonical_id")}
        if len(candidate_ids) > 1:
            # Ambiguous team name mapping to multiple existing canonical team IDs -> FAIL CLOSED
            return None
        elif len(candidate_ids) == 1:
            target_canonical = list(candidate_ids)[0]
            effective_p_id = p_team_id_str if p_team_id_str else (f"{league_id}_{norm_name}" if league_id else norm_name)
            storage.save_team_identity(
                sport=sport,
                canonical_id=target_canonical,
                provider=provider,
                provider_team_id=effective_p_id,
                normalized_name=norm_name,
                display_name=raw_str,
                league_id=league_id,
            )
            return target_canonical

    slug = sanitize_canonical_slug(raw_str)
    if not slug or slug == "unknown":
        return None

    target_canonical = f"{sport}_team_{slug}"

    # Check if target_canonical collides with a team in another league or with a different name in storage
    existing_by_cid = storage.get_team_identities_by_canonical_id(sport, target_canonical)
    if existing_by_cid:
        for ex in existing_by_cid:
            ex_league = ex.get("league_id")
            ex_norm = ex.get("normalized_name", "")
            # Disambiguate if target_canonical is already registered for another league or different team name
            if (league_id is not None and ex_league != league_id) or (
                ex_norm and ex_norm != norm_name and safe_stripped_team_name(ex_norm) != safe_stripped_team_name(norm_name)
            ):
                if league_id:
                    target_canonical = f"{sport}_team_{league_id}_{slug}"
                    existing_by_cid_2 = storage.get_team_identities_by_canonical_id(sport, target_canonical)
                    if existing_by_cid_2:
                        for ex2 in existing_by_cid_2:
                            ex2_norm = ex2.get("normalized_name", "")
                            if ex2_norm and ex2_norm != norm_name and safe_stripped_team_name(ex2_norm) != safe_stripped_team_name(norm_name):
                                return None
                else:
                    return None
                break

    effective_p_id = p_team_id_str if p_team_id_str else (f"{league_id}_{norm_name}" if league_id else norm_name)
    storage.save_team_identity(
        sport=sport,
        canonical_id=target_canonical,
        provider=provider,
        provider_team_id=effective_p_id,
        normalized_name=norm_name,
        display_name=raw_str or f"Team {effective_p_id}",
        league_id=league_id,
    )

    return target_canonical
