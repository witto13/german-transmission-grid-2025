"""
Metrics capture utilities for grid reduction pipeline.

Captures per-voltage and global network statistics for comparison
before and after reduction phases.
"""

from dataclasses import dataclass, asdict
from typing import Dict, Tuple, Optional
from datetime import datetime
import json
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


@dataclass
class VoltageMetrics:
    """Per-voltage-level metrics."""
    voltage: int
    node_count: int
    line_count: int
    total_length_km: float
    total_capacity_mva: float
    degree_distribution: Dict[int, int]  # degree -> count
    degree_1_count: int  # endpoints
    degree_2_count: int  # potential simplification candidates
    degree_3_plus_count: int  # junctions


@dataclass
class GlobalMetrics:
    """Network-wide metrics."""
    scenario: str
    timestamp: str
    total_buses: int
    total_lines: int
    total_transformers: int
    total_length_km: float
    total_capacity_mva: float
    bounding_box: Tuple[float, float, float, float]  # minx, miny, maxx, maxy
    connected_components: int
    voltage_metrics: Dict[int, Dict]  # voltage -> VoltageMetrics as dict
    transformer_counts: Dict[str, int]  # voltage_pair -> count


def capture_voltage_metrics(conn, scn_name: str, voltage: int) -> VoltageMetrics:
    """Capture metrics for a specific voltage level."""

    # Node count
    node_result = conn.execute(text("""
        SELECT COUNT(*) as count
        FROM grid.egon_etrago_bus
        WHERE scn_name = :scn AND v_nom = :voltage
    """), {'scn': scn_name, 'voltage': voltage})
    node_count = node_result.scalar()

    # Line count and total length/capacity
    line_result = conn.execute(text("""
        SELECT
            COUNT(*) as count,
            COALESCE(SUM(length), 0) as total_length,
            COALESCE(SUM(s_nom), 0) as total_capacity
        FROM grid.egon_etrago_line l
        JOIN grid.egon_etrago_bus b ON l.bus0 = b.bus_id AND l.scn_name = b.scn_name
        WHERE l.scn_name = :scn AND b.v_nom = :voltage
    """), {'scn': scn_name, 'voltage': voltage})
    line_row = line_result.fetchone()
    line_count = line_row[0]
    total_length_km = float(line_row[1]) if line_row[1] else 0.0
    total_capacity_mva = float(line_row[2]) if line_row[2] else 0.0

    # Degree distribution
    degree_result = conn.execute(text("""
        WITH bus_degrees AS (
            SELECT b.bus_id, COUNT(l.line_id) as degree
            FROM grid.egon_etrago_bus b
            LEFT JOIN grid.egon_etrago_line l
                ON (l.bus0 = b.bus_id OR l.bus1 = b.bus_id)
                AND l.scn_name = b.scn_name
            WHERE b.scn_name = :scn AND b.v_nom = :voltage
            GROUP BY b.bus_id
        )
        SELECT degree, COUNT(*) as count
        FROM bus_degrees
        GROUP BY degree
        ORDER BY degree
    """), {'scn': scn_name, 'voltage': voltage})

    degree_distribution = {}
    degree_1_count = 0
    degree_2_count = 0
    degree_3_plus_count = 0

    for row in degree_result:
        degree = row[0]
        count = row[1]
        degree_distribution[degree] = count

        if degree == 1:
            degree_1_count = count
        elif degree == 2:
            degree_2_count = count
        elif degree >= 3:
            degree_3_plus_count += count

    return VoltageMetrics(
        voltage=voltage,
        node_count=node_count,
        line_count=line_count,
        total_length_km=total_length_km,
        total_capacity_mva=total_capacity_mva,
        degree_distribution=degree_distribution,
        degree_1_count=degree_1_count,
        degree_2_count=degree_2_count,
        degree_3_plus_count=degree_3_plus_count,
    )


def capture_metrics(engine: Engine, scn_name: str) -> GlobalMetrics:
    """
    Capture all metrics for a scenario.

    Args:
        engine: SQLAlchemy engine
        scn_name: Scenario name (e.g., 'eGon2025')

    Returns:
        GlobalMetrics with all network statistics
    """
    with engine.connect() as conn:
        # Total counts
        totals = conn.execute(text("""
            SELECT
                (SELECT COUNT(*) FROM grid.egon_etrago_bus WHERE scn_name = :scn) as buses,
                (SELECT COUNT(*) FROM grid.egon_etrago_line WHERE scn_name = :scn) as lines,
                (SELECT COUNT(*) FROM grid.egon_etrago_transformer WHERE scn_name = :scn) as transformers
        """), {'scn': scn_name}).fetchone()

        total_buses = totals[0]
        total_lines = totals[1]
        total_transformers = totals[2]

        # Total length and capacity
        line_totals = conn.execute(text("""
            SELECT
                COALESCE(SUM(length), 0) as total_length,
                COALESCE(SUM(s_nom), 0) as total_capacity
            FROM grid.egon_etrago_line
            WHERE scn_name = :scn
        """), {'scn': scn_name}).fetchone()

        total_length_km = float(line_totals[0]) if line_totals[0] else 0.0
        total_capacity_mva = float(line_totals[1]) if line_totals[1] else 0.0

        # Bounding box
        bbox_result = conn.execute(text("""
            SELECT
                MIN(x) as minx, MIN(y) as miny,
                MAX(x) as maxx, MAX(y) as maxy
            FROM grid.egon_etrago_bus
            WHERE scn_name = :scn
        """), {'scn': scn_name}).fetchone()

        bounding_box = (
            float(bbox_result[0]) if bbox_result[0] else 0.0,
            float(bbox_result[1]) if bbox_result[1] else 0.0,
            float(bbox_result[2]) if bbox_result[2] else 0.0,
            float(bbox_result[3]) if bbox_result[3] else 0.0,
        )

        # Connected components (simplified - count distinct v_nom groups)
        # For full connectivity analysis, would need graph traversal
        components_result = conn.execute(text("""
            SELECT COUNT(DISTINCT v_nom)
            FROM grid.egon_etrago_bus
            WHERE scn_name = :scn
        """), {'scn': scn_name}).scalar()
        connected_components = components_result

        # Per-voltage metrics
        voltage_levels = [380, 220, 110]
        voltage_metrics = {}
        for voltage in voltage_levels:
            vm = capture_voltage_metrics(conn, scn_name, voltage)
            voltage_metrics[voltage] = asdict(vm)

        # Transformer counts by voltage pair
        trafo_result = conn.execute(text("""
            SELECT
                CONCAT(GREATEST(b0.v_nom, b1.v_nom), '-', LEAST(b0.v_nom, b1.v_nom)) as voltage_pair,
                COUNT(*) as count
            FROM grid.egon_etrago_transformer t
            JOIN grid.egon_etrago_bus b0 ON t.bus0 = b0.bus_id AND t.scn_name = b0.scn_name
            JOIN grid.egon_etrago_bus b1 ON t.bus1 = b1.bus_id AND t.scn_name = b1.scn_name
            WHERE t.scn_name = :scn
            GROUP BY voltage_pair
            ORDER BY voltage_pair
        """), {'scn': scn_name})

        transformer_counts = {row[0]: row[1] for row in trafo_result}

    return GlobalMetrics(
        scenario=scn_name,
        timestamp=datetime.now().isoformat(),
        total_buses=total_buses,
        total_lines=total_lines,
        total_transformers=total_transformers,
        total_length_km=total_length_km,
        total_capacity_mva=total_capacity_mva,
        bounding_box=bounding_box,
        connected_components=connected_components,
        voltage_metrics=voltage_metrics,
        transformer_counts=transformer_counts,
    )


def compare_metrics(before: GlobalMetrics, after: GlobalMetrics) -> Dict:
    """
    Compare two metric snapshots.

    Returns a dictionary with differences and percentages.
    """
    def pct_change(old, new):
        if old == 0:
            return 0.0
        return ((new - old) / old) * 100

    comparison = {
        'scenarios': {
            'before': before.scenario,
            'after': after.scenario,
        },
        'totals': {
            'buses': {
                'before': before.total_buses,
                'after': after.total_buses,
                'change': after.total_buses - before.total_buses,
                'pct_change': pct_change(before.total_buses, after.total_buses),
            },
            'lines': {
                'before': before.total_lines,
                'after': after.total_lines,
                'change': after.total_lines - before.total_lines,
                'pct_change': pct_change(before.total_lines, after.total_lines),
            },
            'transformers': {
                'before': before.total_transformers,
                'after': after.total_transformers,
                'change': after.total_transformers - before.total_transformers,
                'pct_change': pct_change(before.total_transformers, after.total_transformers),
            },
            'total_length_km': {
                'before': before.total_length_km,
                'after': after.total_length_km,
                'change': after.total_length_km - before.total_length_km,
                'pct_change': pct_change(before.total_length_km, after.total_length_km),
            },
        },
        'by_voltage': {},
    }

    # Compare per-voltage metrics
    for voltage in [380, 220, 110]:
        before_v = before.voltage_metrics.get(voltage, {})
        after_v = after.voltage_metrics.get(voltage, {})

        comparison['by_voltage'][voltage] = {
            'nodes': {
                'before': before_v.get('node_count', 0),
                'after': after_v.get('node_count', 0),
                'pct_change': pct_change(
                    before_v.get('node_count', 0),
                    after_v.get('node_count', 0)
                ),
            },
            'lines': {
                'before': before_v.get('line_count', 0),
                'after': after_v.get('line_count', 0),
                'pct_change': pct_change(
                    before_v.get('line_count', 0),
                    after_v.get('line_count', 0)
                ),
            },
        }

    return comparison


def save_metrics(metrics: GlobalMetrics, path: str) -> None:
    """Save metrics to JSON file."""
    data = asdict(metrics)
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


def load_metrics(path: str) -> GlobalMetrics:
    """Load metrics from JSON file."""
    with open(path, 'r') as f:
        data = json.load(f)

    return GlobalMetrics(
        scenario=data['scenario'],
        timestamp=data['timestamp'],
        total_buses=data['total_buses'],
        total_lines=data['total_lines'],
        total_transformers=data['total_transformers'],
        total_length_km=data['total_length_km'],
        total_capacity_mva=data['total_capacity_mva'],
        bounding_box=tuple(data['bounding_box']),
        connected_components=data['connected_components'],
        voltage_metrics=data['voltage_metrics'],
        transformer_counts=data['transformer_counts'],
    )
