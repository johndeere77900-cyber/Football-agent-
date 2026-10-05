"""
Canonical Team Identity Resolution Layer.

Provides deterministic team identity matching across providers (api_football, football_data_org, etc.)
without assuming provider numeric IDs are interchangeable or using unsafe fuzzy matching.

Resolution Priority:
1. Existing explicit provider mapping in persistent storage (`team_identities`).
2. Exact normalized team name + competition context (`league_id`).
3. Explicit configured canonical alias match (`config.CANONICAL_TEAM_ALIASES`).
4. Safe normalized name match (strip FC/CF/BSC noise words within same league_id).
5. Fail closed (return None) when ambiguous or unmapped.
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
    2. Check exact normalized_name + same league_id context in DB (`team_identities`).
    3. Check competition-aware alias in `config.CANONICAL_TEAM_ALIASES` matching (norm_name, league_id) or norm_name.
    4. Check safe stripped name within same league_id context.
    5. If auto_register is True, generate a new canonical identity.
       Otherwise, if unresolved, FAIL CLOSED and return None.
    """
    if not raw_name or not isinstance(raw_name, str) or not raw_name.strip():
        return None

    provider = str(provider).strip().lower()
    p_team_id_str = str(provider_team_id).strip()
    norm_name = normalize_team_name(raw_name)
    if not norm_name:
        return None

    # Step 1: Existing verified provider mapping in DB
    existing = storage.get_team_identity_by_provider(sport, provider, p_team_id_str)
    if existing:
        return existing["canonical_id"]

    # Step 2: Exact normalized_name + league_id context lookup in DB
    target_canonical_id = None
    if league_id:
        matches = storage.get_team_identities_by_normalized_name(sport, norm_name, league_id)
        if matches:
            candidate_ids = {m["canonical_id"] for m in matches}
            if len(candidate_ids) == 1:
                target_canonical_id = list(candidate_ids)[0]
            else:
                return None

    # Step 3: Competition-aware alias check in config
    if not target_canonical_id:
        alias_dict = getattr(config, "CANONICAL_TEAM_ALIASES", {})
        # Check (norm_name, league_id) tuple first if league_id is provided
        canonical_alias = None
        if league_id and (norm_name, league_id) in alias_dict:
            canonical_alias = alias_dict[(norm_name, league_id)]
        elif norm_name in alias_dict:
            canonical_alias = alias_dict[norm_name]

        if canonical_alias:
            target_canonical_id = f"{sport}_team_{sanitize_canonical_slug(canonical_alias)}"

    # Step 4: Safe stripped name match within same league_id
    if not target_canonical_id and league_id:
        stripped_name = safe_stripped_team_name(raw_name)
        if stripped_name and stripped_name != norm_name:
            stripped_matches = storage.get_team_identities_by_normalized_name(sport, stripped_name, league_id)
            if stripped_matches:
                candidate_ids = {m["canonical_id"] for m in stripped_matches}
                if len(candidate_ids) == 1:
                    target_canonical_id = list(candidate_ids)[0]

    # FAIL CLOSED: If no trusted canonical identity is resolved, return None
    if not target_canonical_id:
        return None

    # Persist the newly resolved mapping for this provider team ID
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
    league_id: int,
    sport: str = "football",
) -> str | None:
    """
    Controlled historical canonical team identity bootstrap mechanism.

    Used ONLY during trusted historical ingestion.
    Requirements:
    - sport is known
    - league/competition is known
    - raw_name and normalized_name are valid
    - competition context (league_id) is known
    - generates a unique canonical identity for the team within that competition context
      if no existing mapping or conflicting mapping exists.
    """
    if not raw_name or not isinstance(raw_name, str) or not raw_name.strip():
        return None
    if not league_id or not isinstance(league_id, int) or league_id <= 0:
        return None

    provider = str(provider).strip().lower()
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

    # Otherwise, generate controlled canonical identity using competition-scoped slug
    slug = sanitize_canonical_slug(raw_name)
    canonical_id = f"{sport}_team_{league_id}_{slug}"

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
