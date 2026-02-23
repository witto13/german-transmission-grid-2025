#!/usr/bin/env python3
"""
Tests for scripts/simplify_degree2.py

Unit tests (no DB required):
  pytest tests/test_simplify_degree2.py -v -k "not integration"

Integration tests (require running DB with eGon2025v2 scenario):
  pytest tests/test_simplify_degree2.py -v -k integration
"""

import os
import sys
import subprocess

import numpy as np
import pandas as pd
import pytest

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from scripts.simplify_degree2 import (
    build_protected_set,
    _find_cross_voltage_neighbors,
    KM_PER_DEG_LAT,
    KM_PER_DEG_LON,
)
from scripts.reduction.v4.degree2_elimination import Degree2Eliminator
from scripts.reduction.core.electrical_params import (
    aggregate_series_lines,
    aggregate_parallel_lines,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_buses(data):
    """Create buses DataFrame from list of (bus_id, v_nom, x, y) tuples."""
    return pd.DataFrame(data, columns=['bus_id', 'v_nom', 'x', 'y'])


def _make_lines(data, start_id=1):
    """Create lines DataFrame with electrical params.

    data: list of (bus0, bus1) or (bus0, bus1, r, x, s_nom, length) tuples.
    """
    records = []
    for i, row in enumerate(data):
        if len(row) == 2:
            b0, b1 = row
            r, x, s_nom, length = 0.01, 0.1, 1000.0, 10.0
        else:
            b0, b1, r, x, s_nom, length = row
        records.append({
            'line_id': start_id + i,
            'bus0': b0, 'bus1': b1,
            'r': r, 'x': x, 'b': 0.0, 'g': 0.0,
            's_nom': s_nom, 'length': length,
            'v_nom': 380, 'cables': 3, 'carrier': 'AC',
            's_nom_extendable': False, 's_nom_min': 0, 's_nom_max': 0,
            's_max_pu': 1.0, 'build_year': 0, 'lifetime': 40.0,
            'capital_cost': 0, 'terrain_factor': 1.0,
            'num_parallel': 1.0, 'v_ang_min': None, 'v_ang_max': None,
            'type': '',
        })
    return pd.DataFrame(records)


def _make_trafos(edges, start_id=1):
    """Create transformers DataFrame from list of (bus0, bus1) tuples."""
    return pd.DataFrame([
        {'trafo_id': start_id + i, 'bus0': b0, 'bus1': b1}
        for i, (b0, b1) in enumerate(edges)
    ])


def _m_to_deg_x(meters):
    return meters / (KM_PER_DEG_LON * 1000)


def _m_to_deg_y(meters):
    return meters / (KM_PER_DEG_LAT * 1000)


def _empty_df(columns):
    return pd.DataFrame(columns=columns)


def _build_data(buses, lines, trafos=None, links=None, generators=None,
                loads=None, storages=None, stores=None, osm_subs=None,
                ehv_subs=None):
    """Build a data dict like load_data() returns."""
    return {
        'buses': buses,
        'lines': lines,
        'trafos': trafos if trafos is not None else _empty_df(['trafo_id', 'bus0', 'bus1']),
        'links': links if links is not None else _empty_df(['link_id', 'bus0', 'bus1']),
        'generators': generators if generators is not None else _empty_df(['generator_id', 'bus']),
        'loads': loads if loads is not None else _empty_df(['load_id', 'bus']),
        'storages': storages if storages is not None else _empty_df(['storage_id', 'bus']),
        'stores': stores if stores is not None else _empty_df(['store_id', 'bus']),
        'osm_subs': osm_subs if osm_subs is not None else _empty_df(['bus_i', 'osm_substation_id']),
        'ehv_subs': ehv_subs if ehv_subs is not None else _empty_df(['bus_id']),
    }


# ---------------------------------------------------------------------------
# Unit Tests: Protected Set
# ---------------------------------------------------------------------------

class TestProtectedSet:
    """Tests for build_protected_set()."""

    def test_110kv_always_protected(self):
        """All 110 kV buses are protected regardless of degree."""
        buses = _make_buses([
            (1, 110, 10.0, 52.0),
            (2, 380, 10.1, 52.0),
            (3, 380, 10.2, 52.0),
        ])
        lines = _make_lines([(1, 2), (2, 3)])
        data = _build_data(buses, lines)
        protected = build_protected_set(data)
        assert 1 in protected

    def test_transformer_bus_protected(self):
        """Buses connected to transformers are protected."""
        buses = _make_buses([
            (1, 380, 10.0, 52.0),
            (2, 380, 10.5, 52.0),
            (3, 220, 10.0, 52.01),
        ])
        lines = _make_lines([(1, 2)])
        trafos = _make_trafos([(1, 3)])
        data = _build_data(buses, lines, trafos=trafos)
        protected = build_protected_set(data)
        assert 1 in protected
        assert 3 in protected

    def test_osm_substation_protected(self):
        """Buses with OSM substation IDs are protected."""
        buses = _make_buses([
            (1, 380, 10.0, 52.0),
            (2, 380, 10.5, 52.0),
            (3, 380, 11.0, 52.0),
        ])
        lines = _make_lines([(1, 2), (2, 3)])
        osm_subs = pd.DataFrame({'bus_i': [2], 'osm_substation_id': [12345]})
        data = _build_data(buses, lines, osm_subs=osm_subs)
        protected = build_protected_set(data)
        assert 2 in protected

    def test_ehv_substation_protected(self):
        """Buses in egon_ehv_substation are protected."""
        buses = _make_buses([
            (1, 380, 10.0, 52.0),
            (2, 380, 10.5, 52.0),
            (3, 380, 11.0, 52.0),
        ])
        lines = _make_lines([(1, 2), (2, 3)])
        ehv_subs = pd.DataFrame({'bus_id': [2]})
        data = _build_data(buses, lines, ehv_subs=ehv_subs)
        protected = build_protected_set(data)
        assert 2 in protected

    def test_generator_bus_protected(self):
        """Buses with generators are protected."""
        buses = _make_buses([
            (1, 380, 10.0, 52.0),
            (2, 380, 10.5, 52.0),
            (3, 380, 11.0, 52.0),
        ])
        lines = _make_lines([(1, 2), (2, 3)])
        generators = pd.DataFrame({'generator_id': [100], 'bus': [2]})
        data = _build_data(buses, lines, generators=generators)
        protected = build_protected_set(data)
        assert 2 in protected

    def test_load_bus_protected(self):
        """Buses with loads are protected."""
        buses = _make_buses([
            (1, 380, 10.0, 52.0),
            (2, 380, 10.5, 52.0),
            (3, 380, 11.0, 52.0),
        ])
        lines = _make_lines([(1, 2), (2, 3)])
        loads = pd.DataFrame({'load_id': [100], 'bus': [2]})
        data = _build_data(buses, lines, loads=loads)
        protected = build_protected_set(data)
        assert 2 in protected

    def test_link_bus_protected(self):
        """Buses with links are protected."""
        buses = _make_buses([
            (1, 380, 10.0, 52.0),
            (2, 380, 10.5, 52.0),
            (3, 380, 11.0, 52.0),
        ])
        lines = _make_lines([(1, 2), (2, 3)])
        links = pd.DataFrame({'link_id': [100], 'bus0': [2], 'bus1': [99]})
        data = _build_data(buses, lines, links=links)
        protected = build_protected_set(data)
        assert 2 in protected

    def test_cross_voltage_nearby_protected(self):
        """Bus near a different-voltage bus is protected."""
        base_x, base_y = 10.0, 52.0
        buses = _make_buses([
            (1, 380, base_x, base_y),
            (2, 380, base_x + _m_to_deg_x(50000), base_y),
            (3, 220, base_x + _m_to_deg_x(50200), base_y),  # 200m from bus 2
            (4, 380, base_x + _m_to_deg_x(100000), base_y),
        ])
        lines = _make_lines([(1, 2), (2, 4)])
        data = _build_data(buses, lines)
        protected = build_protected_set(data)
        # Bus 2 is near bus 3 (different voltage, <1km) -> protected
        assert 2 in protected

    def test_unprotected_waypoint(self):
        """A pure waypoint with no special attributes is NOT protected."""
        base_x, base_y = 10.0, 52.0
        buses = _make_buses([
            (1, 380, base_x, base_y),
            (2, 380, base_x + _m_to_deg_x(50000), base_y),  # 50km from 1
            (3, 380, base_x + _m_to_deg_x(100000), base_y),  # 50km from 2
        ])
        lines = _make_lines([(1, 2), (2, 3)])
        data = _build_data(buses, lines)
        protected = build_protected_set(data)
        # Bus 2 is 380kV, degree-2, no trafo, no OSM, no EHV, no cross-voltage
        assert 2 not in protected


# ---------------------------------------------------------------------------
# Unit Tests: Degree-2 Elimination Logic
# ---------------------------------------------------------------------------

class TestDegree2Elimination:
    """Tests for the Degree2Eliminator with the protected set."""

    def test_simple_chain_elimination(self):
        """A--B--C chain with B eliminable -> 1 merged line A-C."""
        lines = _make_lines([
            (1, 2, 0.01, 0.1, 1000, 10),
            (2, 3, 0.02, 0.2, 900, 20),
        ])
        protected = {1, 3}
        all_buses = {1, 2, 3}

        elim = Degree2Eliminator(lines, protected, all_buses)
        analysis = elim.analyze()

        assert analysis['eliminable_count'] == 1
        assert 2 in analysis['eliminable_buses']
        assert analysis['chain_count'] == 1

        merged = elim.compute_merged_lines(next_line_id=100)
        assert len(merged) == 1

        ml = merged[0]
        assert ml['bus0'] in (1, 3) and ml['bus1'] in (1, 3)
        assert ml['bus0'] != ml['bus1']
        # Series: r = 0.01 + 0.02 = 0.03
        assert abs(ml['r'] - 0.03) < 1e-9
        # Series: x = 0.1 + 0.2 = 0.3
        assert abs(ml['x'] - 0.3) < 1e-9
        # Series: s_nom = min(1000, 900) = 900
        assert abs(ml['s_nom'] - 900) < 1e-9
        # Series: length = 10 + 20 = 30
        assert abs(ml['length'] - 30) < 1e-9

    def test_multi_bus_chain(self):
        """A--B--C--D chain with B,C eliminable -> 1 merged line A-D."""
        lines = _make_lines([
            (1, 2, 0.01, 0.1, 1000, 10),
            (2, 3, 0.02, 0.2, 800, 20),
            (3, 4, 0.03, 0.3, 900, 30),
        ])
        protected = {1, 4}
        all_buses = {1, 2, 3, 4}

        elim = Degree2Eliminator(lines, protected, all_buses)
        analysis = elim.analyze()

        assert analysis['eliminable_count'] == 2
        assert {2, 3} == analysis['eliminable_buses']

        merged = elim.compute_merged_lines(next_line_id=100)
        assert len(merged) == 1

        ml = merged[0]
        # r = 0.01+0.02+0.03 = 0.06
        assert abs(ml['r'] - 0.06) < 1e-9
        # x = 0.1+0.2+0.3 = 0.6
        assert abs(ml['x'] - 0.6) < 1e-9
        # s_nom = min(1000,800,900) = 800
        assert abs(ml['s_nom'] - 800) < 1e-9
        # length = 10+20+30 = 60
        assert abs(ml['length'] - 60) < 1e-9

    def test_protected_bus_not_eliminated(self):
        """Bus 2 is protected -> not eliminated even though degree-2."""
        lines = _make_lines([
            (1, 2, 0.01, 0.1, 1000, 10),
            (2, 3, 0.02, 0.2, 900, 20),
        ])
        protected = {1, 2, 3}  # All protected
        all_buses = {1, 2, 3}

        elim = Degree2Eliminator(lines, protected, all_buses)
        analysis = elim.analyze()

        assert analysis['eliminable_count'] == 0
        assert analysis['chain_count'] == 0

    def test_branch_bus_not_eliminated(self):
        """Bus 2 has degree-3 -> not eliminated."""
        lines = _make_lines([
            (1, 2, 0.01, 0.1, 1000, 10),
            (2, 3, 0.02, 0.2, 900, 20),
            (2, 4, 0.03, 0.3, 800, 30),
        ])
        protected = {1, 3, 4}
        all_buses = {1, 2, 3, 4}

        elim = Degree2Eliminator(lines, protected, all_buses)
        analysis = elim.analyze()

        # Bus 2 has degree 3, not eliminable
        assert 2 not in analysis['eliminable_buses']

    def test_parallel_lines_at_segment(self):
        """Parallel lines at a segment are compressed before series merge."""
        # A--B has 2 parallel lines, B--C has 1 line
        # B is degree-2 in topology (after parallel compression)
        lines = _make_lines([
            (1, 2, 0.04, 0.4, 500, 10),   # parallel 1
            (1, 2, 0.04, 0.4, 500, 10),   # parallel 2
            (2, 3, 0.02, 0.2, 900, 20),
        ], start_id=1)
        # Fix: second line needs different line_id
        lines.loc[1, 'line_id'] = 2
        lines.loc[2, 'line_id'] = 3

        protected = {1, 3}
        all_buses = {1, 2, 3}

        elim = Degree2Eliminator(lines, protected, all_buses)
        analysis = elim.analyze()

        assert 2 in analysis['eliminable_buses']

        merged = elim.compute_merged_lines(next_line_id=100)
        assert len(merged) == 1

        ml = merged[0]
        # Parallel: r_par = 0.04/2 = 0.02, then series: 0.02 + 0.02 = 0.04
        assert abs(ml['r'] - 0.04) < 1e-9
        # Parallel: x_par = 0.4/2 = 0.2, then series: 0.2 + 0.2 = 0.4
        assert abs(ml['x'] - 0.4) < 1e-9
        # Parallel: s_nom_par = 500+500 = 1000, series: min(1000,900) = 900
        assert abs(ml['s_nom'] - 900) < 1e-9

    def test_lines_to_delete(self):
        """get_lines_to_delete returns all lines touching eliminated buses."""
        lines = _make_lines([
            (1, 2, 0.01, 0.1, 1000, 10),
            (2, 3, 0.02, 0.2, 900, 20),
            (3, 4, 0.03, 0.3, 800, 30),  # not touching eliminable bus
        ])
        protected = {1, 3, 4}
        all_buses = {1, 2, 3, 4}

        elim = Degree2Eliminator(lines, protected, all_buses)
        elim.analyze()
        to_delete = elim.get_lines_to_delete()

        # Lines 1 (1-2) and 2 (2-3) touch bus 2
        assert 1 in to_delete
        assert 2 in to_delete
        # Line 3 (3-4) does NOT touch any eliminated bus
        assert 3 not in to_delete

    def test_two_independent_chains(self):
        """Two separate chains produce two merged lines."""
        # Chain 1: 1--2--3
        # Chain 2: 4--5--6
        lines = _make_lines([
            (1, 2, 0.01, 0.1, 1000, 10),
            (2, 3, 0.02, 0.2, 900, 20),
            (4, 5, 0.03, 0.3, 800, 30),
            (5, 6, 0.04, 0.4, 700, 40),
        ])
        protected = {1, 3, 4, 6}
        all_buses = {1, 2, 3, 4, 5, 6}

        elim = Degree2Eliminator(lines, protected, all_buses)
        analysis = elim.analyze()

        assert analysis['eliminable_count'] == 2
        assert analysis['chain_count'] == 2

        merged = elim.compute_merged_lines(next_line_id=100)
        assert len(merged) == 2


# ---------------------------------------------------------------------------
# Unit Tests: Series Impedance Math
# ---------------------------------------------------------------------------

class TestSeriesImpedance:
    """Verify series impedance formulas from electrical_params."""

    def test_series_r_x_sum(self):
        """Series lines: r and x are summed."""
        lines = [
            {'r': 0.01, 'x': 0.1, 's_nom': 1000, 'length': 10},
            {'r': 0.02, 'x': 0.2, 's_nom': 900, 'length': 20},
        ]
        result = aggregate_series_lines(lines)
        assert abs(result['r'] - 0.03) < 1e-9
        assert abs(result['x'] - 0.3) < 1e-9

    def test_series_s_nom_bottleneck(self):
        """Series lines: s_nom is the bottleneck (minimum)."""
        lines = [
            {'r': 0.01, 'x': 0.1, 's_nom': 1000, 'length': 10},
            {'r': 0.02, 'x': 0.2, 's_nom': 800, 'length': 20},
        ]
        result = aggregate_series_lines(lines)
        assert abs(result['s_nom'] - 800) < 1e-9

    def test_series_length_sum(self):
        """Series lines: lengths are summed."""
        lines = [
            {'r': 0.01, 'x': 0.1, 's_nom': 1000, 'length': 10},
            {'r': 0.02, 'x': 0.2, 's_nom': 900, 'length': 20},
        ]
        result = aggregate_series_lines(lines)
        assert abs(result['length'] - 30) < 1e-9


# ---------------------------------------------------------------------------
# Unit Tests: Cross-Voltage Neighbor Detection
# ---------------------------------------------------------------------------

class TestCrossVoltageNeighbors:
    """Tests for _find_cross_voltage_neighbors()."""

    def test_nearby_different_voltage(self):
        """380kV bus 200m from 220kV bus -> both protected."""
        base_x, base_y = 10.0, 52.0
        buses = _make_buses([
            (1, 380, base_x, base_y),
            (2, 220, base_x + _m_to_deg_x(200), base_y),
        ])
        protected = set()
        count = _find_cross_voltage_neighbors(buses, protected)
        assert 1 in protected
        assert 2 in protected
        assert count == 2

    def test_distant_different_voltage(self):
        """380kV bus 5km from 220kV bus -> neither protected."""
        base_x, base_y = 10.0, 52.0
        buses = _make_buses([
            (1, 380, base_x, base_y),
            (2, 220, base_x + _m_to_deg_x(5000), base_y),
        ])
        protected = set()
        count = _find_cross_voltage_neighbors(buses, protected)
        assert 1 not in protected
        assert 2 not in protected
        assert count == 0

    def test_same_voltage_nearby(self):
        """Two 380kV buses nearby -> NOT protected by this check."""
        base_x, base_y = 10.0, 52.0
        buses = _make_buses([
            (1, 380, base_x, base_y),
            (2, 380, base_x + _m_to_deg_x(200), base_y),
        ])
        protected = set()
        count = _find_cross_voltage_neighbors(buses, protected)
        assert count == 0


# ---------------------------------------------------------------------------
# Integration Tests (require running DB)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestIntegration:
    """Integration tests requiring a running PostgreSQL database."""

    @pytest.fixture(autouse=True)
    def setup_engine(self):
        from sqlalchemy import create_engine, text
        self.engine = create_engine(
            'postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data')
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            pytest.skip("Database not available")

    def test_dry_run_no_changes(self):
        """Dry-run does not modify the database."""
        from sqlalchemy import text

        # Count buses in source
        with self.engine.connect() as conn:
            pre_buses = conn.execute(text(
                "SELECT count(*) FROM grid.egon_etrago_bus "
                "WHERE scn_name = 'eGon2025v2'"
            )).scalar()

        if pre_buses == 0:
            pytest.skip("eGon2025v2 scenario not available")

        result = subprocess.run(
            [sys.executable, 'scripts/simplify_degree2.py', '--dry-run'],
            capture_output=True, text=True, cwd='/root/egon_2025_project',
            timeout=120,
        )
        assert result.returncode == 0, f"Script failed: {result.stderr}"

        with self.engine.connect() as conn:
            post_buses = conn.execute(text(
                "SELECT count(*) FROM grid.egon_etrago_bus "
                "WHERE scn_name = 'eGon2025v2'"
            )).scalar()

        assert pre_buses == post_buses, \
            f"Dry run modified bus count: {pre_buses} -> {post_buses}"

    def test_apply_and_validate(self):
        """Apply elimination and verify topology invariants."""
        from scripts.simplify_degree2 import run_pipeline
        from sqlalchemy import text

        target = 'eGon2025v3_test'
        try:
            # Check source exists
            with self.engine.connect() as conn:
                src_buses = conn.execute(text(
                    "SELECT count(*) FROM grid.egon_etrago_bus "
                    "WHERE scn_name = 'eGon2025v2'"
                )).scalar()
            if src_buses == 0:
                pytest.skip("eGon2025v2 scenario not available")

            result = run_pipeline(
                source='eGon2025v2',
                target=target,
                dry_run=False,
            )

            if result is None:
                pytest.skip("No eliminable buses found")

            validation = result['validation']
            assert validation['passed'], \
                f"Validation failed: {validation['issues']}"

        finally:
            # Cleanup
            with self.engine.begin() as conn:
                for table in [
                    'grid.egon_etrago_bus', 'grid.egon_etrago_line',
                    'grid.egon_etrago_transformer', 'grid.egon_etrago_generator',
                    'grid.egon_etrago_load', 'grid.egon_etrago_storage',
                    'grid.egon_etrago_store', 'grid.egon_etrago_link',
                ]:
                    conn.execute(text(
                        f"DELETE FROM {table} WHERE scn_name = :scn"
                    ), {'scn': target})
