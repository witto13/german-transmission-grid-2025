#!/usr/bin/env python3
"""
Tests for scripts/simplify_substations.py

Unit tests (no DB required):
  pytest tests/test_simplify_substations.py -v -k "not integration"

Integration tests (require running DB with eGon2025 scenario):
  pytest tests/test_simplify_substations.py -v -k integration
"""

import os
import sys
import subprocess

import numpy as np
import pandas as pd
import pytest

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from scripts.simplify_substations import (
    UnionFind,
    spatial_cluster,
    split_by_connectivity,
    build_mapping,
    count_connected_components,
    KM_PER_DEG_LAT,
    KM_PER_DEG_LON,
)


# ---------------------------------------------------------------------------
# Helper functions for creating test DataFrames
# ---------------------------------------------------------------------------

def _make_buses(data):
    """Create buses DataFrame from list of (bus_id, v_nom, x, y) tuples."""
    return pd.DataFrame(data, columns=['bus_id', 'v_nom', 'x', 'y'])


def _make_lines(edges, start_id=1):
    """Create lines DataFrame from list of (bus0, bus1) tuples."""
    return pd.DataFrame([
        {'line_id': start_id + i, 'bus0': b0, 'bus1': b1}
        for i, (b0, b1) in enumerate(edges)
    ])


def _make_trafos(edges, start_id=1):
    """Create transformers DataFrame from list of (bus0, bus1) tuples."""
    return pd.DataFrame([
        {'trafo_id': start_id + i, 'bus0': b0, 'bus1': b1}
        for i, (b0, b1) in enumerate(edges)
    ])


# Approximate meters -> degrees at 52°N latitude
def _m_to_deg_x(meters):
    """Convert meters to approximate degrees longitude at 52°N."""
    return meters / (KM_PER_DEG_LON * 1000)


def _m_to_deg_y(meters):
    """Convert meters to approximate degrees latitude."""
    return meters / (KM_PER_DEG_LAT * 1000)


# ---------------------------------------------------------------------------
# Unit Tests
# ---------------------------------------------------------------------------

class TestSpatialClustering:
    """Tests for spatial_cluster()."""

    def test_clustering_same_voltage(self):
        """4 buses at 380kV within 500m all merge to 1 cluster."""
        # Place 4 buses within 500m of each other
        base_x, base_y = 10.0, 52.0
        buses = _make_buses([
            (1, 380, base_x, base_y),
            (2, 380, base_x + _m_to_deg_x(100), base_y),
            (3, 380, base_x + _m_to_deg_x(200), base_y),
            (4, 380, base_x + _m_to_deg_x(300), base_y),
        ])
        radii = {110: 200, 220: 1000, 380: 1000}
        cluster_map = spatial_cluster(buses, radii)

        # All 4 should have the same root
        roots = {cluster_map[i] for i in [1, 2, 3, 4]}
        assert len(roots) == 1, f"Expected 1 cluster, got {len(roots)}: {roots}"

    def test_clustering_different_voltage(self):
        """380kV and 110kV buses 100m apart do NOT merge."""
        base_x, base_y = 10.0, 52.0
        buses = _make_buses([
            (1, 380, base_x, base_y),
            (2, 110, base_x + _m_to_deg_x(100), base_y),
        ])
        radii = {110: 200, 220: 1000, 380: 1000}
        cluster_map = spatial_cluster(buses, radii)

        assert cluster_map[1] != cluster_map[2], \
            "Different voltage buses should not be in the same cluster"

    def test_distant_buses_not_clustered(self):
        """380kV buses 5km apart do NOT merge with 1km radius."""
        base_x, base_y = 10.0, 52.0
        buses = _make_buses([
            (1, 380, base_x, base_y),
            (2, 380, base_x + _m_to_deg_x(5000), base_y),
        ])
        radii = {110: 200, 220: 1000, 380: 1000}
        cluster_map = spatial_cluster(buses, radii)

        assert cluster_map[1] != cluster_map[2], \
            "Distant buses should not be clustered"


class TestConnectivitySplit:
    """Tests for split_by_connectivity()."""

    def test_connectivity_split(self):
        """Spatial cluster of 4 buses where 2+2 are disconnected -> 2 sub-clusters."""
        # All 4 in same spatial cluster
        cluster_map = {1: 1, 2: 1, 3: 1, 4: 1}
        # Lines only connect (1,2) and (3,4) — two disconnected pairs
        lines = _make_lines([(1, 2), (3, 4)])

        merge_groups = split_by_connectivity(cluster_map, lines)

        assert len(merge_groups) == 2, f"Expected 2 sub-clusters, got {len(merge_groups)}"
        groups_as_sets = [frozenset(g) for g in merge_groups]
        assert frozenset({1, 2}) in groups_as_sets
        assert frozenset({3, 4}) in groups_as_sets

    def test_fully_connected_cluster(self):
        """All buses in spatial cluster connected -> 1 merge group."""
        cluster_map = {1: 1, 2: 1, 3: 1}
        lines = _make_lines([(1, 2), (2, 3)])

        merge_groups = split_by_connectivity(cluster_map, lines)

        assert len(merge_groups) == 1
        assert merge_groups[0] == {1, 2, 3}

    def test_singleton_not_in_merge_groups(self):
        """Singleton spatial clusters produce no merge groups."""
        cluster_map = {1: 1, 2: 2}  # Each bus is its own cluster
        lines = _make_lines([(1, 2)])

        merge_groups = split_by_connectivity(cluster_map, lines)

        assert len(merge_groups) == 0

    def test_no_lines_within_cluster(self):
        """Spatial cluster with no internal lines -> no merge (all isolated)."""
        cluster_map = {1: 1, 2: 1, 3: 1}
        # Lines only connect to buses outside cluster
        lines = _make_lines([(1, 99), (2, 98)])

        merge_groups = split_by_connectivity(cluster_map, lines)

        # No sub-cluster should have >1 member
        assert len(merge_groups) == 0


class TestBuildMapping:
    """Tests for build_mapping() and representative selection."""

    def test_representative_selection(self):
        """Highest-degree bus in cluster is picked as representative."""
        merge_groups = [{10, 20, 30}]
        # Bus 20 has degree 4 (appears in 4 line endpoints)
        lines = _make_lines([(10, 20), (20, 30), (20, 40), (20, 50)])

        mapping = build_mapping(merge_groups, lines)

        # Bus 20 should be representative (highest degree)
        assert mapping.map(10) == 20
        assert mapping.map(30) == 20
        assert mapping.map(20) == 20  # identity (not in removed_nodes)

    def test_tiebreak_lowest_bus_id(self):
        """When degree is tied, lowest bus_id wins."""
        merge_groups = [{100, 200}]
        # Both have degree 1 from within-group line, plus 1 external each
        lines = _make_lines([(100, 200), (100, 300), (200, 400)])

        mapping = build_mapping(merge_groups, lines)

        # Both have degree 2; bus 200 wins because tiebreak is -bus_id
        # Actually: max(key=lambda b: (degree, -b)) so -100 > -200 => 100 wins
        assert mapping.map(200) == 100

    def test_empty_merge_groups(self):
        """No merge groups -> empty mapping."""
        lines = _make_lines([(1, 2)])
        mapping = build_mapping([], lines)
        assert len(mapping.removed_nodes) == 0


class TestSelfLoopDetection:
    """Tests for self-loop creation during remapping."""

    def test_self_loop_detection(self):
        """Line with both endpoints in same cluster becomes self-loop."""
        # Build a scenario where remapping creates a self-loop
        merge_groups = [{1, 2}]
        lines = _make_lines([(1, 2), (1, 3)])

        mapping = build_mapping(merge_groups, lines)

        # Line (1,2) — after remapping both map to same bus -> self-loop
        b0_new = mapping.map(1)
        b1_new = mapping.map(2)
        assert b0_new == b1_new, "Expected self-loop after remapping"

        # Line (1,3) — only one endpoint changes -> not a self-loop
        b0_ext = mapping.map(1)
        b1_ext = mapping.map(3)
        assert b0_ext != b1_ext, "External line should not become self-loop"

    def test_transformer_no_self_loop(self):
        """Transformer connecting 110kV->380kV cannot become self-loop
        (different voltage level clusters never merge)."""
        base_x, base_y = 10.0, 52.0
        buses = _make_buses([
            (1, 110, base_x, base_y),
            (2, 110, base_x + _m_to_deg_x(50), base_y),
            (3, 380, base_x + _m_to_deg_x(20), base_y),
        ])
        radii = {110: 200, 220: 1000, 380: 1000}
        cluster_map = spatial_cluster(buses, radii)

        # 110kV buses 1,2 may cluster; 380kV bus 3 is separate
        assert cluster_map[1] == cluster_map[2], "110kV buses should cluster"
        assert cluster_map[1] != cluster_map[3], "Different voltages should not cluster"

        # Transformer (2, 3) would not become self-loop
        # because 2 and 3 are in different clusters


class TestParallelPreservation:
    """Test that parallel lines between different clusters survive."""

    def test_parallel_preservation(self):
        """Two lines from different clusters to same destination survive as parallels."""
        # Cluster A: buses 1,2 (merge -> 1)
        # Cluster B: bus 3 (no merge)
        # Lines: (1,3) and (2,3) -> after merge: (1,3) and (1,3) = parallels
        merge_groups = [{1, 2}]
        lines = _make_lines([(1, 2), (1, 3), (2, 3)])

        mapping = build_mapping(merge_groups, lines)

        # Both (1,3) and (2,3) map to (rep,3)
        rep = mapping.map(1)
        assert mapping.map(2) == rep
        # Line (1,3) -> (rep, 3), Line (2,3) -> (rep, 3)
        # Both survive (as parallel lines), self-loop (1,2)->(rep,rep) is deleted separately


class TestWaypointPreservation:
    """Test that isolated degree-2 buses not near any cluster are untouched."""

    def test_waypoint_preservation(self):
        """Isolated degree-2 bus far from any cluster is untouched."""
        base_x, base_y = 10.0, 52.0
        buses = _make_buses([
            (1, 380, base_x, base_y),
            (2, 380, base_x + _m_to_deg_x(50000), base_y),  # 50km away
            (3, 380, base_x + _m_to_deg_x(100000), base_y),  # 100km away
        ])
        radii = {110: 200, 220: 1000, 380: 1000}
        lines = _make_lines([(1, 2), (2, 3)])

        cluster_map = spatial_cluster(buses, radii)
        merge_groups = split_by_connectivity(cluster_map, lines)

        # No bus should be merged (all far apart)
        assert len(merge_groups) == 0
        mapping = build_mapping(merge_groups, lines)
        assert len(mapping.removed_nodes) == 0


class TestChainMerging:
    """Test transitive merging in chains."""

    def test_chain_not_over_merged(self):
        """Buses A-B-C-D in a line, each 150m apart (A-D = 450m), all merge at 200m."""
        base_x, base_y = 10.0, 52.0
        step = _m_to_deg_x(150)  # 150m
        buses = _make_buses([
            (1, 380, base_x, base_y),
            (2, 380, base_x + step, base_y),
            (3, 380, base_x + 2 * step, base_y),
            (4, 380, base_x + 3 * step, base_y),
        ])
        # Use 200m radius — A-B, B-C, C-D are all within 200m
        # But A-D is ~450m, beyond 200m.  Union-Find transitivity merges all.
        radii = {110: 200, 220: 1000, 380: 200}
        cluster_map = spatial_cluster(buses, radii)

        roots = {cluster_map[i] for i in [1, 2, 3, 4]}
        assert len(roots) == 1, \
            f"Transitive merge should produce 1 cluster, got {len(roots)}"


class TestComponentCountPreservation:
    """Test that simplification preserves connected component count."""

    def test_component_count_preserved(self):
        """2-component graph stays 2-component after simplification."""
        base_x, base_y = 10.0, 52.0
        # Component 1: buses 1,2,3 (1,2 close -> cluster)
        # Component 2: buses 4,5,6 (4,5 close -> cluster), far from component 1
        buses = _make_buses([
            (1, 380, base_x, base_y),
            (2, 380, base_x + _m_to_deg_x(100), base_y),
            (3, 380, base_x + _m_to_deg_x(50000), base_y),
            (4, 380, base_x + _m_to_deg_x(200000), base_y),
            (5, 380, base_x + _m_to_deg_x(200100), base_y),
            (6, 380, base_x + _m_to_deg_x(250000), base_y),
        ])
        lines = _make_lines([
            (1, 2), (2, 3),  # Component 1
            (4, 5), (5, 6),  # Component 2
        ])
        trafos = _make_trafos([])

        radii = {110: 200, 220: 1000, 380: 1000}

        # Pre: 2 components
        buses_set = set(buses['bus_id'].values)
        pre = count_connected_components(buses_set, lines, trafos)
        assert pre == 2

        # Cluster + merge
        cluster_map = spatial_cluster(buses, radii)
        merge_groups = split_by_connectivity(cluster_map, lines)
        mapping = build_mapping(merge_groups, lines)

        # Apply mapping to lines (simulate remapping)
        remapped_lines = lines.copy()
        remapped_lines['bus0'] = remapped_lines['bus0'].map(lambda b: mapping.map(b))
        remapped_lines['bus1'] = remapped_lines['bus1'].map(lambda b: mapping.map(b))

        # Remove self-loops
        remapped_lines = remapped_lines[remapped_lines['bus0'] != remapped_lines['bus1']]

        # Post: still 2 components
        remaining_buses = set(remapped_lines['bus0']) | set(remapped_lines['bus1'])
        post = count_connected_components(remaining_buses, remapped_lines, trafos)
        assert post == 2, f"Expected 2 components, got {post}"


# ---------------------------------------------------------------------------
# Integration Tests (require running DB)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestIntegration:
    """Integration tests requiring a running PostgreSQL database."""

    @pytest.fixture(autouse=True)
    def setup_engine(self):
        """Set up database connection."""
        from sqlalchemy import create_engine, text
        self.engine = create_engine(
            'postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data')
        # Verify connection
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            pytest.skip("Database not available")

    def test_full_pipeline_dry_run(self):
        """Run script with --dry-run, verify no DB changes."""
        from sqlalchemy import text

        # Get pre-counts
        with self.engine.connect() as conn:
            pre_buses = conn.execute(text(
                "SELECT count(*) FROM grid.egon_etrago_bus WHERE scn_name = 'eGon2025'"
            )).scalar()

        # Run dry-run
        result = subprocess.run(
            [sys.executable, 'scripts/simplify_substations.py', '--dry-run'],
            capture_output=True, text=True, cwd='/root/egon_2025_project',
            timeout=120,
        )
        assert result.returncode == 0, f"Script failed: {result.stderr}"

        # Verify no changes
        with self.engine.connect() as conn:
            post_buses = conn.execute(text(
                "SELECT count(*) FROM grid.egon_etrago_bus WHERE scn_name = 'eGon2025'"
            )).scalar()

        assert pre_buses == post_buses, \
            f"Dry run modified bus count: {pre_buses} -> {post_buses}"

    def test_scenario_copy(self):
        """Verify eGon2025v2 copy has same row counts as eGon2025."""
        from scripts.simplify_substations import copy_scenario
        from sqlalchemy import text

        target = 'eGon2025v2_test_copy'
        try:
            counts = copy_scenario(self.engine, 'eGon2025', target)

            with self.engine.connect() as conn:
                src_buses = conn.execute(text(
                    "SELECT count(*) FROM grid.egon_etrago_bus "
                    "WHERE scn_name = 'eGon2025'"
                )).scalar()
                tgt_buses = conn.execute(text(
                    "SELECT count(*) FROM grid.egon_etrago_bus "
                    "WHERE scn_name = :scn"
                ), {'scn': target}).scalar()

            assert counts['buses'] == src_buses
            assert tgt_buses == src_buses
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

    def test_post_simplification_topology(self):
        """After simplification, verify topology invariants."""
        from scripts.simplify_substations import (
            copy_scenario, load_data, count_connected_components,
            apply_remapping, delete_self_loops, delete_orphaned_buses,
        )
        from sqlalchemy import text

        target = 'eGon2025v2_test_topo'
        try:
            # Copy and simplify
            copy_scenario(self.engine, 'eGon2025', target)
            buses, lines, trafos = load_data(self.engine, target)
            buses_set = set(buses['bus_id'].values)
            pre_components = count_connected_components(buses_set, lines, trafos)

            radii = {110: 200, 220: 1000, 380: 1000}
            cluster_map = spatial_cluster(buses, radii)
            merge_groups = split_by_connectivity(cluster_map, lines)
            mapping = build_mapping(merge_groups, lines)

            apply_remapping(self.engine, target, mapping)
            delete_self_loops(self.engine, target)
            delete_orphaned_buses(self.engine, target)

            # Reload and validate
            post_buses, post_lines, post_trafos = load_data(self.engine, target)
            post_buses_set = set(post_buses['bus_id'].values)
            post_components = count_connected_components(
                post_buses_set, post_lines, post_trafos)

            # Same connected component count
            assert post_components == pre_components, \
                f"Components changed: {pre_components} -> {post_components}"

            # No self-loops
            assert len(post_lines[post_lines['bus0'] == post_lines['bus1']]) == 0
            assert len(post_trafos[post_trafos['bus0'] == post_trafos['bus1']]) == 0

            # Voltage consistency: lines connect same-voltage buses
            bus_vnom = dict(zip(post_buses['bus_id'], post_buses['v_nom']))
            for _, row in post_lines.iterrows():
                v0 = bus_vnom.get(int(row['bus0']))
                v1 = bus_vnom.get(int(row['bus1']))
                if v0 is not None and v1 is not None:
                    assert v0 == v1, \
                        f"Line {row['line_id']}: voltage mismatch {v0} != {v1}"

            # Voltage consistency: transformers connect different-voltage buses
            for _, row in post_trafos.iterrows():
                v0 = bus_vnom.get(int(row['bus0']))
                v1 = bus_vnom.get(int(row['bus1']))
                if v0 is not None and v1 is not None:
                    assert v0 != v1, \
                        f"Trafo {row['trafo_id']}: same voltage {v0} == {v1}"

            # No orphaned buses
            referenced = set()
            for _, row in post_lines.iterrows():
                referenced.add(int(row['bus0']))
                referenced.add(int(row['bus1']))
            for _, row in post_trafos.iterrows():
                referenced.add(int(row['bus0']))
                referenced.add(int(row['bus1']))
            # Check generators/loads too
            for tbl_name in ['generator', 'load', 'storage', 'store']:
                df = pd.read_sql(text(
                    f"SELECT bus FROM grid.egon_etrago_{tbl_name} "
                    f"WHERE scn_name = :scn"
                ), self.engine, params={'scn': target})
                referenced.update(df['bus'].astype(int).values)
            links = pd.read_sql(text(
                "SELECT bus0, bus1 FROM grid.egon_etrago_link WHERE scn_name = :scn"
            ), self.engine, params={'scn': target})
            if len(links) > 0:
                referenced.update(links['bus0'].astype(int).values)
                referenced.update(links['bus1'].astype(int).values)

            orphans = post_buses_set - referenced
            assert len(orphans) == 0, f"{len(orphans)} orphaned buses remain"

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
