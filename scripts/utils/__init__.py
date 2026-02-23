"""
Utility modules for generator matching.
"""
from .name_matching import (
    normalize_substation_name,
    fuzzy_match_substation,
    calculate_name_confidence,
)
from .spatial_matching import (
    SpatialMatcher,
    calculate_spatial_confidence,
    DISTANCE_THRESHOLDS,
)

__all__ = [
    'normalize_substation_name',
    'fuzzy_match_substation',
    'calculate_name_confidence',
    'SpatialMatcher',
    'calculate_spatial_confidence',
    'DISTANCE_THRESHOLDS',
]
