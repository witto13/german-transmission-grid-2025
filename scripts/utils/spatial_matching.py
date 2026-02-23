"""
Spatial matching utilities using KD-tree for efficient nearest-neighbor lookup.

This module provides efficient spatial matching of generators to buses
based on geographic coordinates, with voltage-level filtering.
"""
import numpy as np
from typing import Optional
import pandas as pd

try:
    from scipy.spatial import cKDTree
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False


# Distance thresholds by voltage level (km)
# Higher voltage substations are more sparse, so allow larger search radius
DISTANCE_THRESHOLDS = {
    380: 5.0,   # EHV - large transmission substations
    220: 3.0,   # HV - regional substations
    110: 2.0,   # HV - distribution interface
}

# Coordinate conversion factors (approximate for Germany ~52° latitude)
KM_PER_DEG_LAT = 111.0
KM_PER_DEG_LON = 71.5  # cos(52°) * 111


class SpatialMatcher:
    """
    Efficient spatial matching using KD-trees, with voltage-level filtering.

    Builds separate KD-trees for each voltage level to enable voltage-constrained
    nearest-neighbor queries.
    """

    def __init__(self, buses_df: pd.DataFrame):
        """
        Initialize with bus data.

        Args:
            buses_df: DataFrame with columns [bus_id, lon (or x), lat (or y), v_nom]
        """
        self.buses = buses_df.copy()

        # Normalize column names
        if 'x' in self.buses.columns and 'lon' not in self.buses.columns:
            self.buses['lon'] = self.buses['x']
        if 'y' in self.buses.columns and 'lat' not in self.buses.columns:
            self.buses['lat'] = self.buses['y']

        # Filter out invalid coordinates
        valid_mask = (
            self.buses['lon'].notna() &
            self.buses['lat'].notna() &
            (self.buses['lon'] >= -180) & (self.buses['lon'] <= 180) &
            (self.buses['lat'] >= -90) & (self.buses['lat'] <= 90)
        )
        self.buses = self.buses[valid_mask].copy()

        self.trees = {}  # KD-tree per voltage level
        self.indices = {}  # Bus indices per voltage level
        self.bus_data = {}  # Bus data per voltage level

        if not HAS_SCIPY:
            print("Warning: scipy not available, using brute-force spatial matching")
            return

        # Build KD-trees for each voltage level
        for voltage in [110, 220, 380]:
            mask = self.buses['v_nom'] == voltage
            voltage_buses = self.buses[mask].reset_index(drop=True)

            if len(voltage_buses) > 0:
                # Convert to approximate km coordinates for better distance calculations
                coords = np.column_stack([
                    voltage_buses['lon'].values * KM_PER_DEG_LON,
                    voltage_buses['lat'].values * KM_PER_DEG_LAT
                ])
                self.trees[voltage] = cKDTree(coords)
                self.indices[voltage] = np.arange(len(voltage_buses))
                self.bus_data[voltage] = voltage_buses

    def find_nearest(
        self,
        lon: float,
        lat: float,
        voltage: int,
        max_distance_km: Optional[float] = None
    ) -> Optional[tuple[int, float]]:
        """
        Find nearest bus at specified voltage level.

        Args:
            lon: Longitude of query point
            lat: Latitude of query point
            voltage: Target voltage level (110, 220, 380)
            max_distance_km: Maximum search radius (uses default if None)

        Returns:
            Tuple of (bus_id, distance_km) or None if no match within threshold
        """
        if pd.isna(lon) or pd.isna(lat):
            return None

        if voltage not in self.trees:
            return None

        if max_distance_km is None:
            max_distance_km = DISTANCE_THRESHOLDS.get(voltage, 2.0)

        # Convert query point to km coordinates
        query = np.array([lon * KM_PER_DEG_LON, lat * KM_PER_DEG_LAT])

        # Query KD-tree
        distance, idx = self.trees[voltage].query(query, k=1)

        if distance <= max_distance_km:
            bus_row = self.bus_data[voltage].iloc[idx]
            bus_id = int(bus_row['bus_id'])
            return (bus_id, float(distance))

        return None

    def find_nearest_k(
        self,
        lon: float,
        lat: float,
        voltage: int,
        k: int = 5,
        max_distance_km: Optional[float] = None
    ) -> list[tuple[int, float]]:
        """
        Find k nearest buses at specified voltage level.

        Args:
            lon: Longitude of query point
            lat: Latitude of query point
            voltage: Target voltage level
            k: Number of neighbors to return
            max_distance_km: Maximum search radius

        Returns:
            List of (bus_id, distance_km) tuples, sorted by distance
        """
        if pd.isna(lon) or pd.isna(lat):
            return []

        if voltage not in self.trees:
            return []

        if max_distance_km is None:
            max_distance_km = DISTANCE_THRESHOLDS.get(voltage, 2.0) * 2

        # Convert query point to km coordinates
        query = np.array([lon * KM_PER_DEG_LON, lat * KM_PER_DEG_LAT])

        # Query KD-tree for k neighbors
        n_buses = len(self.bus_data[voltage])
        k_actual = min(k, n_buses)

        distances, indices = self.trees[voltage].query(query, k=k_actual)

        # Handle single result case (returns scalar instead of array)
        if k_actual == 1:
            distances = [distances]
            indices = [indices]

        results = []
        for dist, idx in zip(distances, indices):
            if dist <= max_distance_km:
                bus_row = self.bus_data[voltage].iloc[idx]
                bus_id = int(bus_row['bus_id'])
                results.append((bus_id, float(dist)))

        return results

    def find_nearest_any_voltage(
        self,
        lon: float,
        lat: float,
        max_distance_km: float = 5.0,
        preferred_voltage: Optional[int] = None
    ) -> Optional[tuple[int, float, int]]:
        """
        Find nearest bus at any HV/EHV voltage level.

        Args:
            lon: Longitude of query point
            lat: Latitude of query point
            max_distance_km: Maximum search radius
            preferred_voltage: If specified, prefer this voltage if within threshold

        Returns:
            Tuple of (bus_id, distance_km, voltage) or None
        """
        if pd.isna(lon) or pd.isna(lat):
            return None

        best = None

        # If preferred voltage specified, check it first
        if preferred_voltage and preferred_voltage in self.trees:
            result = self.find_nearest(lon, lat, preferred_voltage, max_distance_km)
            if result:
                bus_id, distance = result
                best = (bus_id, distance, preferred_voltage)
                # If very close, return immediately
                if distance < 1.0:
                    return best

        # Check all voltage levels (prefer higher voltages for ties)
        for voltage in [380, 220, 110]:
            if voltage == preferred_voltage:
                continue  # Already checked

            result = self.find_nearest(lon, lat, voltage, max_distance_km)
            if result:
                bus_id, distance = result
                if best is None or distance < best[1]:
                    best = (bus_id, distance, voltage)

        return best

    def get_bus_count(self, voltage: Optional[int] = None) -> int:
        """Get number of buses in the spatial index."""
        if voltage:
            return len(self.bus_data.get(voltage, []))
        return sum(len(data) for data in self.bus_data.values())


def calculate_spatial_confidence(
    distance_km: float,
    voltage: int,
    method: str = 'spatial_voltage'
) -> float:
    """
    Calculate confidence score for spatial match.

    Confidence decreases with distance and depends on the match method.

    Args:
        distance_km: Distance to matched bus
        voltage: Voltage level of match
        method: Type of spatial match

    Returns:
        Confidence score (0-1)
    """
    BASE_SCORES = {
        'spatial_voltage': 0.60,
        'spatial_direct': 0.60,
        'spatial_sel_centroid': 0.55,
        'spatial_plz_centroid': 0.35,
        'spatial_municipality': 0.30,
        'spatial_any_voltage': 0.50,
    }

    base = BASE_SCORES.get(method, 0.40)
    threshold = DISTANCE_THRESHOLDS.get(voltage, 2.0)

    # Apply distance-based adjustments
    if distance_km <= threshold * 0.5:
        # Very close - bonus
        bonus = min(0.15, (1 - distance_km / threshold) * 0.15)
        return min(0.90, base + bonus)
    elif distance_km <= threshold:
        # Within threshold - base score
        return base
    elif distance_km <= threshold * 2:
        # Beyond threshold but reasonable - penalty
        excess_ratio = (distance_km - threshold) / threshold
        penalty = excess_ratio * 0.15
        return max(0.25, base - penalty)
    else:
        # Very far - large penalty
        return max(0.15, base - 0.25)


def haversine_distance(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """
    Calculate the great-circle distance between two points on Earth.

    Args:
        lon1, lat1: First point coordinates (degrees)
        lon2, lat2: Second point coordinates (degrees)

    Returns:
        Distance in kilometers
    """
    # Convert to radians
    lon1, lat1, lon2, lat2 = map(np.radians, [lon1, lat1, lon2, lat2])

    # Haversine formula
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = np.sin(dlat/2)**2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon/2)**2
    c = 2 * np.arcsin(np.sqrt(a))

    # Earth's radius in km
    r = 6371

    return c * r


def batch_nearest_lookup(
    query_points: pd.DataFrame,
    buses_df: pd.DataFrame,
    voltage_col: str = 'voltage_kv',
    query_lon_col: str = 'lon',
    query_lat_col: str = 'lat',
    max_distance_km: Optional[float] = None
) -> pd.DataFrame:
    """
    Perform batch nearest-neighbor lookup for multiple query points.

    Args:
        query_points: DataFrame with lon, lat, and voltage columns
        buses_df: Bus reference DataFrame
        voltage_col: Name of voltage column in query_points
        query_lon_col: Name of longitude column in query_points
        query_lat_col: Name of latitude column in query_points
        max_distance_km: Maximum search radius (None uses voltage-specific defaults)

    Returns:
        DataFrame with matched bus_id and distance_km added
    """
    matcher = SpatialMatcher(buses_df)

    results = []
    for idx, row in query_points.iterrows():
        lon = row.get(query_lon_col)
        lat = row.get(query_lat_col)
        voltage = row.get(voltage_col)

        if pd.isna(lon) or pd.isna(lat) or pd.isna(voltage):
            results.append({
                'query_idx': idx,
                'matched_bus_id': None,
                'match_distance_km': None,
            })
            continue

        result = matcher.find_nearest(lon, lat, int(voltage), max_distance_km)

        if result:
            bus_id, distance = result
            results.append({
                'query_idx': idx,
                'matched_bus_id': bus_id,
                'match_distance_km': distance,
            })
        else:
            results.append({
                'query_idx': idx,
                'matched_bus_id': None,
                'match_distance_km': None,
            })

    return pd.DataFrame(results).set_index('query_idx')
