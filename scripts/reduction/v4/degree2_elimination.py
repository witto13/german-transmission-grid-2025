"""
Degree-2 bus elimination for v4 network reduction.

Wraps TopologyGraph to find degree-2 chains and compute merged replacement
lines using series-combination formulas.
"""

import os
import sys
from collections import defaultdict
from typing import Dict, List, Set, Any, Optional

import pandas as pd

# Add project root for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))
from scripts.reduction.core.topology import TopologyGraph
from scripts.reduction.core.electrical_params import (
    aggregate_series_lines,
    aggregate_parallel_lines,
)


class Degree2Eliminator:
    """Find and merge degree-2 bus chains in a network.

    Builds a TopologyGraph from a lines DataFrame, identifies eliminable
    degree-2 buses (not in protected set), finds maximal chains, and
    computes replacement lines using series impedance formulas.  Parallel
    lines on a single segment are compressed first.

    Args:
        lines_df: DataFrame with at least columns
            [line_id, bus0, bus1, r, x, b, g, s_nom, length].
        protected: Set of bus IDs that must not be eliminated.
        all_buses: Set of all bus IDs in the network.
    """

    def __init__(
        self,
        lines_df: pd.DataFrame,
        protected: Set[int],
        all_buses: Set[int],
    ):
        self._lines = lines_df.copy()
        self._protected = protected
        self._all_buses = all_buses

        # Build topology graph (parallel edges collapsed to single adjacency)
        self._graph = TopologyGraph()
        for bid in all_buses:
            self._graph.add_node(bid)
        for _, row in lines_df.iterrows():
            self._graph.add_edge(int(row['bus0']), int(row['bus1']))

        # Index lines by (min, max) endpoint pair for parallel detection
        self._pair_lines: Dict[tuple, List[dict]] = defaultdict(list)
        for _, row in lines_df.iterrows():
            key = (min(int(row['bus0']), int(row['bus1'])),
                   max(int(row['bus0']), int(row['bus1'])))
            self._pair_lines[key].append(row.to_dict())

        self._analysis: Optional[dict] = None
        self._chains: List[List[int]] = []

    def analyze(self) -> dict:
        """Identify eliminable buses and chains.

        Returns:
            dict with keys:
                eliminable_buses: set of bus IDs
                eliminable_count: int
                chain_count: int
                chains: list of chains (endpoint-inclusive)
        """
        eliminable = self._graph.find_degree2_buses(protected=self._protected)
        self._chains = self._graph.find_chains(eliminable)

        self._analysis = {
            'eliminable_buses': eliminable,
            'eliminable_count': len(eliminable),
            'chain_count': len(self._chains),
            'chains': self._chains,
        }
        return self._analysis

    def compute_merged_lines(self, next_line_id: int = 1) -> List[Dict[str, Any]]:
        """Compute merged replacement lines for all chains.

        For each chain [A, m1, ..., mN, B]:
          1. For each segment, compress parallel lines via
             ``aggregate_parallel_lines``.
          2. Series-combine the compressed segments via
             ``aggregate_series_lines``.

        Args:
            next_line_id: Starting line_id for new merged lines.

        Returns:
            List of dicts, each representing a merged line with keys
            line_id, bus0, bus1, r, x, b, g, s_nom, length, and metadata
            about the original chain.
        """
        if self._analysis is None:
            self.analyze()

        merged = []
        lid = next_line_id

        for chain in self._chains:
            if len(chain) < 3:
                continue

            endpoint_a = chain[0]
            endpoint_b = chain[-1]

            # Build segment list: consecutive pairs in the chain
            segments = []
            for i in range(len(chain) - 1):
                b0, b1 = chain[i], chain[i + 1]
                key = (min(b0, b1), max(b0, b1))
                seg_lines = self._pair_lines.get(key, [])

                if not seg_lines:
                    continue

                if len(seg_lines) == 1:
                    seg = seg_lines[0]
                    segments.append({
                        'r': float(seg['r']),
                        'x': float(seg['x']),
                        'b': float(seg.get('b', 0) or 0),
                        'g': float(seg.get('g', 0) or 0),
                        's_nom': float(seg['s_nom']),
                        'length': float(seg['length']),
                    })
                else:
                    # Parallel compression first
                    par_input = [{
                        'r': float(l['r']),
                        'x': float(l['x']),
                        'b': float(l.get('b', 0) or 0),
                        'g': float(l.get('g', 0) or 0),
                        's_nom': float(l['s_nom']),
                        'length': float(l['length']),
                    } for l in seg_lines]
                    compressed = aggregate_parallel_lines(par_input)
                    segments.append(compressed)

            if not segments:
                continue

            if len(segments) == 1:
                combined = segments[0]
            else:
                combined = aggregate_series_lines(segments)

            merged.append({
                'line_id': lid,
                'bus0': endpoint_a,
                'bus1': endpoint_b,
                'r': combined['r'],
                'x': combined['x'],
                'b': combined.get('b', 0.0),
                'g': combined.get('g', 0.0),
                's_nom': combined['s_nom'],
                'length': combined['length'],
                'chain': chain,
            })
            lid += 1

        return merged

    def get_lines_to_delete(self) -> Set[int]:
        """Return line_ids of all lines touching eliminated buses.

        These lines are replaced by the merged lines from
        ``compute_merged_lines``.
        """
        if self._analysis is None:
            self.analyze()

        elim_buses = self._analysis['eliminable_buses']
        to_delete = set()
        for _, row in self._lines.iterrows():
            b0, b1 = int(row['bus0']), int(row['bus1'])
            if b0 in elim_buses or b1 in elim_buses:
                to_delete.add(int(row['line_id']))
        return to_delete
