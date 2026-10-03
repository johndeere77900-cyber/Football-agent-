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

    # Lowercase
    text = no_accents.lower()

    # Replace punctuation / non-alphanumeric with space
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
    auto_register: bool = True,
) -> str | None:
    """
    Resolve canonical team identity deterministically.

    Order:
    1. Check existing provider mapping in DB (`team_identities`).
    2. Check exact normalized_name + league_id in DB (`team_identities`).
    3. Check configured canonical alias (`config.CANONICAL_TEAM_ALIASES`).
    4. Check safe stripped name within same league_id.
    5. Register new canonical identity if auto_register=True and name is unambiguous.
    """
    if not raw_name or not isinstance(raw_name, str) or not raw_name.strip():
        return None

    provider = str(provider).strip().lower()
    p_team_id_str = str(provider_team_id).strip()
    norm_name = normalize_team_name(raw_name)
    if not norm_name:
        return None

    # Step 1: Existing provider mapping in DB
    existing = storage.get_team_identity_by_provider(sport, provider, p_team_id_str)
    if existing:
        return existing["canonical_id"]

    # Step 2: Check explicit alias in config
    alias_dict = getattr(config, "CANONICAL_TEAM_ALIASES", {})
    alias_key = norm_name
    target_canonical_id = None

    if alias_key in alias_dict:
        canonical_alias = alias_dict[alias_key]
        target_canonical_id = f"{sport}_team_{sanitize_canonical_slug(canonical_alias)}"

    # Step 3: Exact normalized name + league_id lookup in DB
    if not target_canonical_id:
        matches = storage.get_team_identities_by_normalized_name(sport, norm_name, league_id)
        if matches:
            # Verify no conflicting canonical IDs
            candidate_ids = {m["canonical_id"] for m in matches}
            if len(candidate_ids) == 1:
                target_canonical_id = list(candidate_ids)[0]
            else:
                # Ambiguous match across different teams -> fail closed
                return None

    # Step 4: Safe stripped name match within same league_id
    if not target_canonical_id and league_id:
        stripped_name = safe_stripped_team_name(raw_name)
        if stripped_name and stripped_name != norm_name:
            stripped_matches = storage.get_team_identities_by_normalized_name(sport, stripped_name, league_id)
            if stripped_matches:
                candidate_ids = {m["canonical_id"] for m in stripped_matches}
                if len(candidate_ids) == 1:
                    target_canonical_id = list(candidate_ids)[0]

    # Step 5: Auto-register new canonical identity if non-existent
    if not target_canonical_id:
        if not auto_register:
            return None
        slug = sanitize_canonical_slug(raw_name)
        target_canonical_id = f"{sport}_team_{slug}"

    # Persist the identity mapping for future deterministic lookups
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
