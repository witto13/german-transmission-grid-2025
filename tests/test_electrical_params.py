"""
Unit tests for electrical parameter aggregation formulas.

Tests parallel/series impedance, capacity aggregation, and centroid computation.
"""

import pytest
import numpy as np
from scripts.reduction.core.electrical_params import (
    parallel_impedance,
    series_impedance,
    parallel_reactance,
    parallel_resistance,
    series_reactance,
    series_resistance,
    sum_capacity,
    bottleneck_capacity,
    weighted_average_length,
    aggregate_parallel_lines,
    aggregate_series_lines,
    calculate_impedance_per_km,
    compute_centroid
)


class TestParallelImpedance:
    """Test parallel impedance calculations."""

    def test_single_impedance(self):
        """Single impedance returns itself."""
        assert parallel_impedance([5.0]) == 5.0

    def test_two_equal_impedances(self):
        """Two equal impedances: Z_eq = Z/2."""
        assert parallel_impedance([10.0, 10.0]) == pytest.approx(5.0)

    def test_two_different_impedances(self):
        """Two different impedances: 1/Z_eq = 1/Z1 + 1/Z2."""
        # Z1 = 3, Z2 = 6 → Z_eq = 2
        assert parallel_impedance([3.0, 6.0]) == pytest.approx(2.0)

    def test_three_impedances(self):
        """Three impedances in parallel."""
        # Z1 = 10, Z2 = 10, Z3 = 10 → Z_eq = 10/3
        assert parallel_impedance([10.0, 10.0, 10.0]) == pytest.approx(10.0 / 3.0)

    def test_many_parallel(self):
        """Many parallel impedances reduce total impedance."""
        impedances = [100.0] * 10
        assert parallel_impedance(impedances) == pytest.approx(10.0)

    def test_zero_impedance_raises(self):
        """Zero impedance raises ValueError."""
        with pytest.raises(ValueError, match="Non-positive"):
            parallel_impedance([5.0, 0.0])

    def test_negative_impedance_raises(self):
        """Negative impedance raises ValueError."""
        with pytest.raises(ValueError, match="Non-positive"):
            parallel_impedance([5.0, -2.0])

    def test_empty_list_raises(self):
        """Empty list raises ValueError."""
        with pytest.raises(ValueError, match="Empty"):
            parallel_impedance([])


class TestSeriesImpedance:
    """Test series impedance calculations."""

    def test_single_impedance(self):
        """Single impedance returns itself."""
        assert series_impedance([5.0]) == 5.0

    def test_two_impedances(self):
        """Two impedances in series add."""
        assert series_impedance([3.0, 7.0]) == 10.0

    def test_three_impedances(self):
        """Three impedances in series add."""
        assert series_impedance([1.0, 2.0, 3.0]) == 6.0

    def test_negative_impedance_raises(self):
        """Negative impedance raises ValueError."""
        with pytest.raises(ValueError, match="Negative"):
            series_impedance([5.0, -2.0])

    def test_empty_list_raises(self):
        """Empty list raises ValueError."""
        with pytest.raises(ValueError, match="Empty"):
            series_impedance([])

    def test_zero_allowed_in_series(self):
        """Zero impedance is allowed in series (e.g., ideal connection)."""
        assert series_impedance([5.0, 0.0, 3.0]) == 8.0


class TestCapacityAggregation:
    """Test capacity aggregation functions."""

    def test_sum_capacity_parallel(self):
        """Parallel lines: capacities add."""
        assert sum_capacity([100.0, 150.0, 200.0]) == 450.0

    def test_sum_capacity_negative_raises(self):
        """Negative capacity raises ValueError."""
        with pytest.raises(ValueError, match="Negative"):
            sum_capacity([100.0, -50.0])

    def test_sum_capacity_empty_raises(self):
        """Empty list raises ValueError."""
        with pytest.raises(ValueError, match="Empty"):
            sum_capacity([])

    def test_bottleneck_capacity_series(self):
        """Series lines: capacity is minimum (bottleneck)."""
        assert bottleneck_capacity([100.0, 150.0, 80.0, 200.0]) == 80.0

    def test_bottleneck_capacity_negative_raises(self):
        """Negative capacity raises ValueError."""
        with pytest.raises(ValueError, match="Negative"):
            bottleneck_capacity([100.0, -50.0])

    def test_bottleneck_capacity_empty_raises(self):
        """Empty list raises ValueError."""
        with pytest.raises(ValueError, match="Empty"):
            bottleneck_capacity([])


class TestWeightedAverageLength:
    """Test weighted average length calculation."""

    def test_equal_weights(self):
        """Equal weights give simple average."""
        lengths = [10.0, 20.0, 30.0]
        weights = [1.0, 1.0, 1.0]
        assert weighted_average_length(lengths, weights) == pytest.approx(20.0)

    def test_different_weights(self):
        """Different weights affect average."""
        lengths = [10.0, 20.0]
        weights = [3.0, 1.0]
        # (10*3 + 20*1) / (3+1) = 50/4 = 12.5
        assert weighted_average_length(lengths, weights) == pytest.approx(12.5)

    def test_single_value(self):
        """Single value returns itself."""
        assert weighted_average_length([15.0], [5.0]) == 15.0

    def test_length_mismatch_raises(self):
        """Mismatched list lengths raise ValueError."""
        with pytest.raises(ValueError, match="same size"):
            weighted_average_length([1.0, 2.0], [1.0, 2.0, 3.0])

    def test_zero_weight_sum_raises(self):
        """Zero total weight raises ValueError."""
        with pytest.raises(ValueError, match="zero"):
            weighted_average_length([10.0, 20.0], [0.0, 0.0])

    def test_negative_length_raises(self):
        """Negative length raises ValueError."""
        with pytest.raises(ValueError, match="Negative length"):
            weighted_average_length([-5.0, 10.0], [1.0, 1.0])

    def test_negative_weight_raises(self):
        """Negative weight raises ValueError."""
        with pytest.raises(ValueError, match="Negative weight"):
            weighted_average_length([10.0, 20.0], [1.0, -1.0])


class TestAggregateParallelLines:
    """Test parallel line aggregation."""

    def test_two_parallel_lines(self):
        """Aggregate two parallel lines."""
        lines = [
            {'r': 1.0, 'x': 5.0, 's_nom': 100.0, 'length': 10.0},
            {'r': 1.0, 'x': 5.0, 's_nom': 100.0, 'length': 10.0}
        ]

        result = aggregate_parallel_lines(lines)

        # Parallel: R_eq = R/2, X_eq = X/2
        assert result['r'] == pytest.approx(0.5)
        assert result['x'] == pytest.approx(2.5)
        # Capacity sums
        assert result['s_nom'] == pytest.approx(200.0)
        # Equal weights give simple average
        assert result['length'] == pytest.approx(10.0)
        assert result['num_parallel'] == 2

    def test_three_parallel_different_capacities(self):
        """Aggregate three parallel lines with different capacities."""
        lines = [
            {'r': 3.0, 'x': 9.0, 's_nom': 50.0, 'length': 10.0},
            {'r': 6.0, 'x': 18.0, 's_nom': 100.0, 'length': 12.0},
            {'r': 9.0, 'x': 27.0, 's_nom': 150.0, 'length': 15.0}
        ]

        result = aggregate_parallel_lines(lines)

        # Parallel impedance: 1/Z_eq = 1/3 + 1/6 + 1/9 = 6/18 + 3/18 + 2/18 = 11/18
        # Z_eq = 18/11 ≈ 1.636
        assert result['r'] == pytest.approx(18.0 / 11.0)
        assert result['x'] == pytest.approx(54.0 / 11.0)

        # Capacity sums
        assert result['s_nom'] == pytest.approx(300.0)

        # Weighted average length
        # (10*50 + 12*100 + 15*150) / (50+100+150) = 3950/300 ≈ 13.17
        assert result['length'] == pytest.approx(13.166667, rel=1e-4)

        assert result['num_parallel'] == 3

    def test_empty_list_raises(self):
        """Empty list raises ValueError."""
        with pytest.raises(ValueError, match="Empty"):
            aggregate_parallel_lines([])


class TestAggregateSeriesLines:
    """Test series line aggregation (chain contraction)."""

    def test_two_series_lines(self):
        """Aggregate two series lines."""
        lines = [
            {'r': 1.0, 'x': 5.0, 's_nom': 100.0, 'length': 10.0},
            {'r': 2.0, 'x': 10.0, 's_nom': 150.0, 'length': 15.0}
        ]

        result = aggregate_series_lines(lines)

        # Series: R_eq = R1 + R2, X_eq = X1 + X2
        assert result['r'] == pytest.approx(3.0)
        assert result['x'] == pytest.approx(15.0)

        # Capacity is bottleneck (minimum)
        assert result['s_nom'] == pytest.approx(100.0)

        # Length sums
        assert result['length'] == pytest.approx(25.0)
        assert result['num_series'] == 2

    def test_three_series_lines(self):
        """Aggregate three series lines."""
        lines = [
            {'r': 1.0, 'x': 3.0, 's_nom': 200.0, 'length': 5.0},
            {'r': 0.5, 'x': 2.0, 's_nom': 100.0, 'length': 3.0},
            {'r': 1.5, 'x': 5.0, 's_nom': 150.0, 'length': 7.0}
        ]

        result = aggregate_series_lines(lines)

        # Series: impedances add
        assert result['r'] == pytest.approx(3.0)
        assert result['x'] == pytest.approx(10.0)

        # Bottleneck capacity
        assert result['s_nom'] == pytest.approx(100.0)

        # Total length
        assert result['length'] == pytest.approx(15.0)
        assert result['num_series'] == 3

    def test_empty_list_raises(self):
        """Empty list raises ValueError."""
        with pytest.raises(ValueError, match="Empty"):
            aggregate_series_lines([])


class TestCalculateImpedancePerKm:
    """Test per-km impedance calculation."""

    def test_impedance_per_km(self):
        """Calculate per-km impedance."""
        result = calculate_impedance_per_km(r=3.0, x=9.0, length=10.0)

        assert result['r_per_km'] == pytest.approx(0.3)
        assert result['x_per_km'] == pytest.approx(0.9)

    def test_zero_length_raises(self):
        """Zero length raises ValueError."""
        with pytest.raises(ValueError, match="Invalid length"):
            calculate_impedance_per_km(r=3.0, x=9.0, length=0.0)

    def test_negative_length_raises(self):
        """Negative length raises ValueError."""
        with pytest.raises(ValueError, match="Invalid length"):
            calculate_impedance_per_km(r=3.0, x=9.0, length=-5.0)


class TestComputeCentroid:
    """Test centroid computation."""

    def test_simple_centroid(self):
        """Compute simple centroid (unweighted)."""
        coords = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
        centroid = compute_centroid(coords)

        assert centroid[0] == pytest.approx(5.0)
        assert centroid[1] == pytest.approx(5.0)

    def test_weighted_centroid(self):
        """Compute weighted centroid."""
        coords = [(0.0, 0.0), (10.0, 0.0)]
        weights = [3.0, 1.0]  # First point has 3× weight

        centroid = compute_centroid(coords, weights)

        # (0*3 + 10*1) / 4 = 2.5
        assert centroid[0] == pytest.approx(2.5)
        assert centroid[1] == pytest.approx(0.0)

    def test_single_coordinate(self):
        """Single coordinate returns itself."""
        coords = [(5.0, 7.0)]
        centroid = compute_centroid(coords)

        assert centroid[0] == pytest.approx(5.0)
        assert centroid[1] == pytest.approx(7.0)

    def test_empty_coordinates_raises(self):
        """Empty coordinates raise ValueError."""
        with pytest.raises(ValueError, match="Empty"):
            compute_centroid([])

    def test_weight_length_mismatch_raises(self):
        """Mismatched weight length raises ValueError."""
        coords = [(0.0, 0.0), (10.0, 10.0)]
        weights = [1.0, 2.0, 3.0]

        with pytest.raises(ValueError, match="!= coordinates length"):
            compute_centroid(coords, weights)

    def test_zero_total_weight_raises(self):
        """Zero total weight raises ValueError."""
        coords = [(0.0, 0.0), (10.0, 10.0)]
        weights = [0.0, 0.0]

        with pytest.raises(ValueError, match="zero"):
            compute_centroid(coords, weights)


class TestConvenienceWrappers:
    """Test that convenience wrappers call correct underlying functions."""

    def test_parallel_reactance(self):
        """parallel_reactance wraps parallel_impedance."""
        assert parallel_reactance([3.0, 6.0]) == pytest.approx(2.0)

    def test_parallel_resistance(self):
        """parallel_resistance wraps parallel_impedance."""
        assert parallel_resistance([4.0, 4.0]) == pytest.approx(2.0)

    def test_series_reactance(self):
        """series_reactance wraps series_impedance."""
        assert series_reactance([3.0, 7.0]) == 10.0

    def test_series_resistance(self):
        """series_resistance wraps series_impedance."""
        assert series_resistance([1.0, 2.0, 3.0]) == 6.0


class TestRealWorldScenarios:
    """Test real-world scenarios with realistic values."""

    def test_typical_380kv_parallel_lines(self):
        """Typical 380kV double-circuit line."""
        lines = [
            {'r': 0.02, 'x': 0.30, 's_nom': 1800.0, 'length': 50.0},
            {'r': 0.02, 'x': 0.30, 's_nom': 1800.0, 'length': 50.0}
        ]

        result = aggregate_parallel_lines(lines)

        # Equivalent impedance halved
        assert result['r'] == pytest.approx(0.01)
        assert result['x'] == pytest.approx(0.15)
        # Total capacity doubled
        assert result['s_nom'] == pytest.approx(3600.0)

    def test_typical_110kv_chain(self):
        """Typical 110kV chain contraction."""
        lines = [
            {'r': 0.05, 'x': 0.40, 's_nom': 200.0, 'length': 5.0},
            {'r': 0.08, 'x': 0.64, 's_nom': 180.0, 'length': 8.0},
            {'r': 0.03, 'x': 0.24, 's_nom': 220.0, 'length': 3.0}
        ]

        result = aggregate_series_lines(lines)

        # Total impedance
        assert result['r'] == pytest.approx(0.16)
        assert result['x'] == pytest.approx(1.28)
        # Bottleneck capacity (180 MVA)
        assert result['s_nom'] == pytest.approx(180.0)
        # Total length
        assert result['length'] == pytest.approx(16.0)

    def test_transformer_parallel_aggregation(self):
        """Multiple transformers in parallel (substation consolidation)."""
        transformers = [
            {'r': 0.01, 'x': 0.10, 's_nom': 300.0, 'length': 0.0},
            {'r': 0.01, 'x': 0.10, 's_nom': 300.0, 'length': 0.0},
            {'r': 0.01, 'x': 0.10, 's_nom': 400.0, 'length': 0.0}
        ]

        result = aggregate_parallel_lines(transformers)

        # Parallel impedance of three transformers
        # 1/Z = 1/0.1 + 1/0.1 + 1/0.1 = 30 → Z = 1/30 ≈ 0.0333
        assert result['x'] == pytest.approx(0.1 / 3.0)
        # Total capacity
        assert result['s_nom'] == pytest.approx(1000.0)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
