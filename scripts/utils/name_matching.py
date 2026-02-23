"""
Name normalization and fuzzy matching utilities for substation matching.

This module provides functions to normalize German substation names
and perform fuzzy matching for generator-to-bus assignment.
"""
import re
from typing import Optional

# Try to import rapidfuzz, fall back to fuzzywuzzy if not available
try:
    from rapidfuzz import fuzz, process
    FUZZY_LIB = 'rapidfuzz'
except ImportError:
    try:
        from fuzzywuzzy import fuzz, process
        FUZZY_LIB = 'fuzzywuzzy'
    except ImportError:
        FUZZY_LIB = None


# Prefixes to remove during normalization (German substation naming conventions)
SUBSTATION_PREFIXES = [
    r'^Umspannwerk\s+',
    r'^UW\s+',
    r'^Umspannanlage\s+',
    r'^Station\s+',
    r'^Schaltanlage\s+',
    r'^Netzanschluss\s+',
    r'^Anschluss\s+',
    r'^Netzverknüpfungspunkt\s+',
    r'^NVP\s+',
    r'^Hauptschaltstation\s+',
    r'^HST\s+',
]

# Suffixes to remove
SUBSTATION_SUFFIXES = [
    r'\s+\d{3}\s*kV.*$',      # "380 kV" etc.
    r'\s+[A-Z]{2,3}\s*$',     # "UW", "SA" etc. at end
    r'\s+\(.*\)\s*$',         # Parenthetical info
    r'\s+Nord$',              # Directional suffixes (keep if part of name)
    r'\s+Süd$',
    r'\s+Ost$',
    r'\s+West$',
    r'\s+I+$',                # Roman numerals at end
    r'\s+\d+$',               # Numbers at end
]

# Common abbreviation expansions
ABBREVIATIONS = {
    'uw': 'umspannwerk',
    'sa': 'schaltanlage',
    'hst': 'hauptschaltstation',
    'nap': 'netzanschlusspunkt',
    'nvp': 'netzverknüpfungspunkt',
    'kw': 'kraftwerk',
    'hkw': 'heizkraftwerk',
    'gkw': 'gaskraftwerk',
    'wkw': 'wasserkraftwerk',
    'str': 'strasse',
    'str.': 'strasse',
}

# Characters to normalize (umlauts etc.)
CHAR_REPLACEMENTS = {
    'ä': 'ae',
    'ö': 'oe',
    'ü': 'ue',
    'ß': 'ss',
}


def normalize_substation_name(name: str, preserve_direction: bool = False) -> str:
    """
    Normalize a substation name for matching.

    Steps:
    1. Lowercase
    2. Remove prefixes (Umspannwerk, UW, etc.)
    3. Remove suffixes (voltage levels, abbreviations)
    4. Expand common abbreviations
    5. Normalize umlauts
    6. Remove special characters
    7. Collapse whitespace

    Args:
        name: Raw substation name
        preserve_direction: If True, keep directional suffixes (Nord, Süd, etc.)

    Returns:
        Normalized name string
    """
    if not name or not isinstance(name, str):
        return ''

    # Lowercase and strip
    result = name.lower().strip()

    # Remove prefixes
    for prefix in SUBSTATION_PREFIXES:
        result = re.sub(prefix, '', result, flags=re.IGNORECASE)

    # Remove suffixes (optionally preserve direction)
    suffixes_to_use = SUBSTATION_SUFFIXES
    if preserve_direction:
        suffixes_to_use = [s for s in SUBSTATION_SUFFIXES
                          if not any(d in s for d in ['Nord', 'Süd', 'Ost', 'West'])]

    for suffix in suffixes_to_use:
        result = re.sub(suffix, '', result, flags=re.IGNORECASE)

    # Normalize umlauts
    for char, replacement in CHAR_REPLACEMENTS.items():
        result = result.replace(char, replacement)

    # Expand abbreviations (word-by-word)
    words = result.split()
    expanded = [ABBREVIATIONS.get(w, w) for w in words]
    result = ' '.join(expanded)

    # Remove special characters except spaces and alphanumerics
    result = re.sub(r'[^\w\s]', '', result)

    # Collapse whitespace
    result = re.sub(r'\s+', ' ', result).strip()

    return result


def fuzzy_match_substation(
    query: str,
    candidates: list[tuple[int, str]],  # [(bus_id, name), ...]
    threshold: float = 0.85,
    limit: int = 3
) -> list[tuple[int, str, float]]:
    """
    Find best fuzzy matches for a substation name.

    Uses rapidfuzz (or fuzzywuzzy) for efficient string matching.

    Args:
        query: Normalized query name
        candidates: List of (bus_id, normalized_name) tuples
        threshold: Minimum similarity score (0-1)
        limit: Maximum matches to return

    Returns:
        List of (bus_id, name, score) tuples, sorted by score descending
    """
    if not query or not candidates:
        return []

    if FUZZY_LIB is None:
        # Fallback to simple exact match if no fuzzy library available
        exact_matches = [(bid, name, 1.0) for bid, name in candidates if name == query]
        return exact_matches[:limit]

    # Build lookup dict
    name_to_buses = {}
    for bus_id, name in candidates:
        if name not in name_to_buses:
            name_to_buses[name] = []
        name_to_buses[name].append(bus_id)

    names = list(name_to_buses.keys())

    if not names:
        return []

    # Use rapidfuzz/fuzzywuzzy for efficient matching
    if FUZZY_LIB == 'rapidfuzz':
        matches = process.extract(
            query,
            names,
            scorer=fuzz.WRatio,  # Weighted ratio handles word order variations
            score_cutoff=threshold * 100,
            limit=limit
        )
        # rapidfuzz returns (name, score, index)
        results = []
        for name, score, _ in matches:
            for bus_id in name_to_buses[name]:
                results.append((bus_id, name, score / 100.0))
    else:
        # fuzzywuzzy
        matches = process.extract(
            query,
            names,
            scorer=fuzz.WRatio,
            limit=limit * 2  # Get more since we filter later
        )
        # fuzzywuzzy returns (name, score)
        results = []
        for name, score in matches:
            if score >= threshold * 100:
                for bus_id in name_to_buses[name]:
                    results.append((bus_id, name, score / 100.0))

    # Sort by score and limit
    results.sort(key=lambda x: x[2], reverse=True)
    return results[:limit]


def calculate_name_confidence(
    match_type: str,
    similarity_score: Optional[float] = None
) -> float:
    """
    Calculate confidence score based on match type.

    Args:
        match_type: Type of name match performed
        similarity_score: Fuzzy match score if applicable (0-1)

    Returns:
        Confidence score (0-1)
    """
    BASE_SCORES = {
        'san_name_exact': 0.95,
        'san_name_exact_voltage': 0.95,
        'sel_name_exact': 0.85,
        'sel_name_exact_voltage': 0.85,
        'san_name_fuzzy': 0.75,
        'san_name_fuzzy_voltage': 0.75,
        'sel_name_fuzzy': 0.70,
        'sel_name_fuzzy_voltage': 0.70,
    }

    base = BASE_SCORES.get(match_type, 0.50)

    # Adjust for fuzzy match quality
    if 'fuzzy' in match_type and similarity_score is not None:
        # Scale confidence by match quality
        # 0.85-1.0 → 0-1 quality factor
        if similarity_score >= 0.85:
            quality_factor = (similarity_score - 0.85) / 0.15
            adjustment = quality_factor * 0.10  # Up to +10%
            base = min(0.95, base + adjustment)
        elif similarity_score < 0.80:
            # Penalize lower quality matches
            penalty = (0.80 - similarity_score) * 0.20
            base = max(0.50, base - penalty)

    return base


def extract_location_components(name: str) -> dict:
    """
    Extract location components from a substation name.

    Useful for identifying city names, regions, etc.

    Args:
        name: Raw or normalized substation name

    Returns:
        Dict with extracted components
    """
    components = {
        'base_name': '',
        'direction': None,
        'number': None,
        'voltage': None,
    }

    if not name:
        return components

    # Extract voltage if present
    voltage_match = re.search(r'(\d{3})\s*kV', name, re.IGNORECASE)
    if voltage_match:
        components['voltage'] = int(voltage_match.group(1))

    # Extract direction
    direction_match = re.search(r'\b(Nord|Süd|Ost|West|North|South|East|West)\b', name, re.IGNORECASE)
    if direction_match:
        components['direction'] = direction_match.group(1).capitalize()

    # Extract trailing number
    number_match = re.search(r'\s+(\d+)\s*$', name)
    if number_match:
        components['number'] = int(number_match.group(1))

    # Get base name (normalized, without voltage/direction/number)
    components['base_name'] = normalize_substation_name(name)

    return components


def match_names_with_components(
    query: str,
    candidates: list[tuple[int, str]],
    require_exact_direction: bool = True
) -> list[tuple[int, str, float]]:
    """
    Match names considering location components.

    This is useful when there are multiple substations with similar names
    but different directional suffixes (e.g., "Berlin Nord" vs "Berlin Süd").

    Args:
        query: Query substation name
        candidates: List of (bus_id, name) tuples
        require_exact_direction: If True, direction must match exactly

    Returns:
        List of (bus_id, name, score) tuples
    """
    query_components = extract_location_components(query)

    results = []
    for bus_id, candidate_name in candidates:
        cand_components = extract_location_components(candidate_name)

        # Check direction match if required
        if require_exact_direction:
            if query_components['direction'] != cand_components['direction']:
                continue

        # Calculate base name similarity
        q_base = query_components['base_name']
        c_base = cand_components['base_name']

        if FUZZY_LIB and q_base and c_base:
            score = fuzz.WRatio(q_base, c_base) / 100.0
            if score >= 0.80:
                results.append((bus_id, candidate_name, score))
        elif q_base == c_base:
            results.append((bus_id, candidate_name, 1.0))

    results.sort(key=lambda x: x[2], reverse=True)
    return results
