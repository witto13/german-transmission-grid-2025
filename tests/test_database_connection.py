"""
Test database connection and basic read operations.
"""

import pytest
from scripts.reduction.db.reader import GridReader
from scripts.reduction.db.backup import ScenarioBackup
from scripts.reduction.config import ORIGINAL_SCENARIO


def test_database_connection():
    """Test that database connection works."""
    reader = GridReader()

    # Check if original scenario exists
    exists = reader.scenario_exists(ORIGINAL_SCENARIO)
    assert exists, f"Original scenario '{ORIGINAL_SCENARIO}' should exist in database"

    reader.close()


def test_read_buses():
    """Test reading buses from database."""
    reader = GridReader()

    buses = reader.read_buses(ORIGINAL_SCENARIO)

    # Should have ~14,494 buses
    assert len(buses) > 10000, "Should have > 10k buses"
    assert len(buses) < 20000, "Should have < 20k buses"

    # Check required columns
    required_cols = ['bus_id', 'v_nom', 'x', 'y', 'scn_name', 'country']
    for col in required_cols:
        assert col in buses.columns, f"Missing column: {col}"

    # Check voltage levels
    voltage_levels = set(buses['v_nom'].unique())
    assert 110 in voltage_levels, "Should have 110kV buses"
    assert 220 in voltage_levels, "Should have 220kV buses"
    assert 380 in voltage_levels, "Should have 380kV buses"

    reader.close()


def test_read_lines():
    """Test reading lines from database."""
    reader = GridReader()

    lines = reader.read_lines(ORIGINAL_SCENARIO)

    # Should have ~26,489 lines
    assert len(lines) > 20000, "Should have > 20k lines"
    assert len(lines) < 30000, "Should have < 30k lines"

    # Check required columns
    required_cols = ['line_id', 'bus0', 'bus1', 'length', 's_nom', 'x', 'r', 'scn_name']
    for col in required_cols:
        assert col in lines.columns, f"Missing column: {col}"

    reader.close()


def test_read_transformers():
    """Test reading transformers from database."""
    reader = GridReader()

    transformers = reader.read_transformers(ORIGINAL_SCENARIO)

    # Should have ~535 transformers
    assert len(transformers) > 400, "Should have > 400 transformers"
    assert len(transformers) < 700, "Should have < 700 transformers"

    # Check required columns
    required_cols = ['trafo_id', 'bus0', 'bus1', 's_nom', 'x', 'r', 'scn_name']
    for col in required_cols:
        assert col in transformers.columns, f"Missing column: {col}"

    reader.close()


def test_count_components():
    """Test component counting."""
    reader = GridReader()

    counts = reader.count_components(ORIGINAL_SCENARIO)

    # Check expected components
    assert 'bus' in counts
    assert 'line' in counts
    assert 'transformer' in counts

    # Validate counts
    assert counts['bus'] > 10000, "Should have > 10k buses"
    assert counts['line'] > 20000, "Should have > 20k lines"
    assert counts['transformer'] > 400, "Should have > 400 transformers"

    reader.close()


def test_read_osm_mapping():
    """Test reading OSM substation mapping."""
    reader = GridReader()

    osm_mapping = reader.read_osm_substation_mapping()

    # Should have ~5,329 buses with OSM mapping
    assert len(osm_mapping) > 4000, "Should have > 4k OSM-mapped buses"
    assert len(osm_mapping) < 7000, "Should have < 7k OSM-mapped buses"

    # Check required columns
    required_cols = ['bus_id', 'osm_substation_id', 'osm_name']
    for col in required_cols:
        assert col in osm_mapping.columns, f"Missing column: {col}"

    reader.close()


def test_scenario_backup():
    """Test scenario backup functionality."""
    backup = ScenarioBackup()

    # Check that original scenario exists
    assert backup.scenario_exists(ORIGINAL_SCENARIO), f"Original scenario '{ORIGINAL_SCENARIO}' should exist"

    # List all scenarios
    scenarios = backup.list_scenarios()
    assert ORIGINAL_SCENARIO in scenarios, f"Original scenario should be in list: {scenarios}"

    backup.close()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
