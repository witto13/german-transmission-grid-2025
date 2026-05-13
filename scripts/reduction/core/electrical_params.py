"""
Pure-math functions for electrical parameter aggregation.

Handles parallel and series impedance combinations, capacity aggregation,
and geometric computations for network reduction.
"""

from typing import List, Tuple, Optional, Dict, Any


def parallel_impedance(values: List[float]) -> float:
    """Compute equivalent parallel impedance: 1/Z_eq = sum(1/Zi).

    Args:
        values: List of impedance values (all must be positive).

    Returns:
        Equivalent parallel impedance.

    Raises:
        ValueError: If list is empty or contains non-positive values.
    """
    if not values:
        raise ValueError("Empty impedance list")
    values = [float(v) for v in values]
    for v in values:
        if v <= 0:
            raise ValueError(f"Non-positive impedance: {v}")
    if len(values) == 1:
        return values[0]
    reciprocal_sum = sum(1.0 / v for v in values)
    return 1.0 / reciprocal_sum


def series_impedance(values: List[float]) -> float:
    """Compute equivalent series impedance: Z_eq = sum(Zi).

    Args:
        values: List of impedance values (must be non-negative).

    Returns:
        Sum of impedance values.

    Raises:
        ValueError: If list is empty or contains negative values.
    """
    if not values:
        raise ValueError("Empty impedance list")
    values = [float(v) for v in values]
    for v in values:
        if v < 0:
            raise ValueError(f"Negative impedance: {v}")
    return sum(values)


# Convenience wrappers
def parallel_reactance(values: List[float]) -> float:
    """Parallel reactance combination (wraps parallel_impedance)."""
    return parallel_impedance(values)


def parallel_resistance(values: List[float]) -> float:
    """Parallel resistance combination (wraps parallel_impedance)."""
    return parallel_impedance(values)


def series_reactance(values: List[float]) -> float:
    """Series reactance combination (wraps series_impedance)."""
    return series_impedance(values)


def series_resistance(values: List[float]) -> float:
    """Series resistance combination (wraps series_impedance)."""
    return series_impedance(values)


def sum_capacity(values: List[float]) -> float:
    """Sum capacities for parallel lines: S_eq = sum(Si).

    Args:
        values: List of capacity values (must be non-negative).

    Returns:
        Total capacity.

    Raises:
        ValueError: If list is empty or contains negative values.
    """
    if not values:
        raise ValueError("Empty capacity list")
    values = [float(v) for v in values]
    for v in values:
        if v < 0:
            raise ValueError(f"Negative capacity: {v}")
    return sum(values)


def bottleneck_capacity(values: List[float]) -> float:
    """Bottleneck capacity for series lines: S_eq = min(Si).

    Args:
        values: List of capacity values (must be non-negative).

    Returns:
        Minimum capacity (bottleneck).

    Raises:
        ValueError: If list is empty or contains negative values.
    """
    if not values:
        raise ValueError("Empty capacity list")
    values = [float(v) for v in values]
    for v in values:
        if v < 0:
            raise ValueError(f"Negative capacity: {v}")
    return min(values)


def weighted_average_length(lengths: List[float], weights: List[float]) -> float:
    """Compute weighted average length.

    Args:
        lengths: List of length values.
        weights: List of weight values (e.g., capacities).

    Returns:
        Weighted average length.

    Raises:
        ValueError: If lists have different sizes, contain negative values,
                     or total weight is zero.
    """
    if len(lengths) != len(weights):
        raise ValueError(
            f"Lengths and weights must have same size "
            f"({len(lengths)} != {len(weights)})"
        )
    lengths = [float(l) for l in lengths]
    weights = [float(w) for w in weights]
    for l in lengths:
        if l < 0:
            raise ValueError(f"Negative length: {l}")
    for w in weights:
        if w < 0:
            raise ValueError(f"Negative weight: {w}")
    total_weight = sum(weights)
    if total_weight == 0:
        raise ValueError("Total weight is zero")
    return sum(l * w for l, w in zip(lengths, weights)) / total_weight


def aggregate_parallel_lines(lines: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate N parallel lines into one equivalent line.

    Parallel combination:
        - r, x: parallel impedance (1/Z_eq = sum(1/Zi))
        - b: sum (parallel susceptance adds)
        - g: sum (parallel conductance adds)
        - s_nom: sum (capacities add)
        - length: weighted average by s_nom
        - num_parallel: count of input lines

    Args:
        lines: List of dicts with keys 'r', 'x', 's_nom', 'length',
               and optionally 'b', 'g'.

    Returns:
        Dict with aggregated parameters.

    Raises:
        ValueError: If list is empty.
    """
    if not lines:
        raise ValueError("Empty line list")

    r_values = [float(l['r']) for l in lines]
    x_values = [float(l['x']) for l in lines]
    s_values = [float(l['s_nom']) for l in lines]
    l_values = [float(l['length']) for l in lines]

    result = {
        'r': parallel_impedance(r_values),
        'x': parallel_impedance(x_values),
        's_nom': sum_capacity(s_values),
        'length': weighted_average_length(l_values, s_values),
        'num_parallel': len(lines),
    }

    # Handle susceptance (b) if present: parallel adds
    if any('b' in l for l in lines):
        b_values = [float(l.get('b', 0.0) or 0.0) for l in lines]
        result['b'] = sum(b_values)

    # Handle conductance (g) if present: parallel adds
    if any('g' in l for l in lines):
        g_values = [float(l.get('g', 0.0) or 0.0) for l in lines]
        result['g'] = sum(g_values)

    return result


def aggregate_series_lines(lines: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate N series lines into one equivalent line.

    Series combination:
        - r, x: series impedance (Z_eq = sum(Zi))
        - b: reciprocal series (1/b_eq = sum(1/bi)) if all non-zero, else 0
        - g: sum (typically all zero)
        - s_nom: bottleneck (minimum)
        - length: sum
        - num_series: count of input lines

    Args:
        lines: List of dicts with keys 'r', 'x', 's_nom', 'length',
               and optionally 'b', 'g'.

    Returns:
        Dict with aggregated parameters.

    Raises:
        ValueError: If list is empty.
    """
    if not lines:
        raise ValueError("Empty line list")

    r_values = [float(l['r']) for l in lines]
    x_values = [float(l['x']) for l in lines]
    s_values = [float(l['s_nom']) for l in lines]
    l_values = [float(l['length']) for l in lines]

    result = {
        'r': series_impedance(r_values),
        'x': series_impedance(x_values),
        's_nom': bottleneck_capacity(s_values),
        'length': sum(l_values),
        'num_series': len(lines),
    }

    # Handle susceptance (b): reciprocal series if all non-zero
    if any('b' in l for l in lines):
        b_values = [float(l.get('b', 0.0) or 0.0) for l in lines]
        if all(b != 0 for b in b_values):
            result['b'] = 1.0 / sum(1.0 / b for b in b_values)
        else:
            result['b'] = 0.0

    # Handle conductance (g): sum (typically 0)
    if any('g' in l for l in lines):
        g_values = [float(l.get('g', 0.0) or 0.0) for l in lines]
        result['g'] = sum(g_values)

    return result


def calculate_impedance_per_km(r: float, x: float, length: float) -> Dict[str, float]:
    """Calculate per-km impedance values.

    Args:
        r: Total resistance.
        x: Total reactance.
        length: Line length in km.

    Returns:
        Dict with 'r_per_km' and 'x_per_km'.

    Raises:
        ValueError: If length is zero or negative.
    """
    if length <= 0:
        raise ValueError(f"Invalid length: {length}")
    return {
        'r_per_km': r / length,
        'x_per_km': x / length,
    }


def compute_centroid(
    coords: List[Tuple[float, float]],
    weights: Optional[List[float]] = None,
) -> Tuple[float, float]:
    """Compute (optionally weighted) centroid of coordinates.

    Args:
        coords: List of (x, y) tuples.
        weights: Optional list of weights. If None, equal weights are used.

    Returns:
        (x, y) centroid.

    Raises:
        ValueError: If coords is empty, weights length mismatches, or
                     total weight is zero.
    """
    if not coords:
        raise ValueError("Empty coordinate list")
    if weights is not None:
        if len(weights) != len(coords):
            raise ValueError(
                f"Weights length ({len(weights)}) != coordinates length ({len(coords)})"
            )
        total_w = sum(weights)
        if total_w == 0:
            raise ValueError("Total weight is zero")
        cx = sum(c[0] * w for c, w in zip(coords, weights)) / total_w
        cy = sum(c[1] * w for c, w in zip(coords, weights)) / total_w
    else:
        n = len(coords)
        cx = sum(c[0] for c in coords) / n
        cy = sum(c[1] for c in coords) / n
    return (cx, cy)
