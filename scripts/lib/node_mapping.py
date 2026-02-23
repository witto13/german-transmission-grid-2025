"""
NodeMapping class for tracking bus transformations through the reduction pipeline.

Provides:
- Forward mapping: old_id -> new_id
- Reverse mapping: new_id -> [old_ids]
- Composition: mapping1.compose(mapping2)
- Serialization to CSV/JSON
"""

from collections import defaultdict
from typing import Dict, Set, Iterable, List, Optional, Any
import pandas as pd
import json


class NodeMapping:
    """
    Tracks bus ID transformations through clustering and simplification phases.

    Supports:
    - Forward mapping: old_id -> new_id
    - Reverse mapping: new_id -> [old_ids]
    - Composition: mapping1.compose(mapping2)
    - Serialization to CSV/JSON

    Example:
        mapping = NodeMapping()
        mapping.add_mapping(101, 100, reason='spatial_cluster', phase='03')
        mapping.add_mapping(102, 100, reason='spatial_cluster', phase='03')

        # Now buses 101 and 102 are merged into bus 100
        assert mapping.map(101) == 100
        assert mapping.map(102) == 100
        assert mapping.get_merged_into(100) == {100, 101, 102}
    """

    def __init__(self):
        self._forward: Dict[int, int] = {}  # old_id -> new_id
        self._reverse: Dict[int, Set[int]] = defaultdict(set)  # new_id -> {old_ids}
        self._metadata: Dict[int, Dict[str, Any]] = {}  # old_id -> {reason, phase, ...}

    def add_mapping(self, old_id: int, new_id: int, reason: str = None, **metadata) -> None:
        """
        Add a single mapping.

        Args:
            old_id: Original bus ID
            new_id: Target bus ID after transformation
            reason: Why this mapping was created (e.g., 'spatial_cluster', 'degree2_elim')
            **metadata: Additional metadata (phase, voltage, etc.)
        """
        self._forward[old_id] = new_id
        self._reverse[new_id].add(old_id)

        if reason or metadata:
            self._metadata[old_id] = {
                'reason': reason,
                'new_id': new_id,
                **metadata
            }

    def map(self, bus_id: int) -> int:
        """
        Map old ID to new ID.

        Returns the same ID if no mapping exists.
        """
        return self._forward.get(bus_id, bus_id)

    def map_many(self, bus_ids: Iterable[int]) -> List[int]:
        """Map multiple IDs."""
        return [self.map(bid) for bid in bus_ids]

    def get_merged_into(self, new_id: int) -> Set[int]:
        """
        Get all old IDs that were merged into new_id.

        Returns a set containing at least the new_id itself.
        """
        result = self._reverse.get(new_id, set())
        if not result:
            return {new_id}
        return result

    def compose(self, other: 'NodeMapping') -> 'NodeMapping':
        """
        Compose two mappings: self followed by other.

        If self maps A->B and other maps B->C, result maps A->C.

        Args:
            other: Second mapping to apply after this one

        Returns:
            New NodeMapping with composed transformations
        """
        result = NodeMapping()

        # Map all our entries through the second mapping
        for old_id, intermediate_id in self._forward.items():
            final_id = other.map(intermediate_id)
            reason = self._metadata.get(old_id, {}).get('reason', 'composed')
            result.add_mapping(old_id, final_id, reason=f"{reason}+composed")

        # Add entries from other that weren't in self
        for old_id, new_id in other._forward.items():
            if old_id not in result._forward:
                reason = other._metadata.get(old_id, {}).get('reason', 'from_other')
                result.add_mapping(old_id, new_id, reason=reason)

        return result

    @property
    def removed_nodes(self) -> Set[int]:
        """Nodes that were mapped to a different node (i.e., removed)."""
        return {k for k, v in self._forward.items() if k != v}

    @property
    def kept_nodes(self) -> Set[int]:
        """Unique target nodes (centroids)."""
        return set(self._forward.values())

    @property
    def all_original_nodes(self) -> Set[int]:
        """All original node IDs that have mappings."""
        return set(self._forward.keys())

    def __len__(self) -> int:
        """Number of mappings."""
        return len(self._forward)

    def __contains__(self, bus_id: int) -> bool:
        """Check if bus_id has a mapping."""
        return bus_id in self._forward

    def get_metadata(self, bus_id: int) -> Optional[Dict[str, Any]]:
        """Get metadata for a specific bus mapping."""
        return self._metadata.get(bus_id)

    def to_dataframe(self) -> pd.DataFrame:
        """
        Export to DataFrame for analysis.

        Returns DataFrame with columns: old_bus, new_bus, reason, ...metadata
        """
        records = []
        for old_id, new_id in self._forward.items():
            record = {'old_bus': old_id, 'new_bus': new_id}
            if old_id in self._metadata:
                record.update(self._metadata[old_id])
            records.append(record)
        return pd.DataFrame(records)

    def save_csv(self, path: str) -> None:
        """Save to CSV file."""
        self.to_dataframe().to_csv(path, index=False)

    def save_json(self, path: str) -> None:
        """Save to JSON file."""
        data = {
            'mappings': [
                {'old_bus': k, 'new_bus': v}
                for k, v in self._forward.items()
            ],
            'metadata': {
                str(k): v for k, v in self._metadata.items()
            },
            'summary': {
                'total_mappings': len(self._forward),
                'removed_nodes': len(self.removed_nodes),
                'kept_nodes': len(self.kept_nodes),
            }
        }
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)

    @classmethod
    def load_csv(cls, path: str) -> 'NodeMapping':
        """Load from CSV file."""
        df = pd.read_csv(path)
        mapping = cls()
        for _, row in df.iterrows():
            reason = row.get('reason') if 'reason' in row else None
            mapping.add_mapping(
                int(row['old_bus']),
                int(row['new_bus']),
                reason=reason
            )
        return mapping

    @classmethod
    def identity(cls, bus_ids: Iterable[int]) -> 'NodeMapping':
        """
        Create identity mapping (no changes).

        Every bus maps to itself.
        """
        mapping = cls()
        for bid in bus_ids:
            mapping.add_mapping(bid, bid, reason='identity')
        return mapping

    def summary(self) -> str:
        """Return human-readable summary."""
        return (
            f"NodeMapping(\n"
            f"  total_mappings={len(self._forward)},\n"
            f"  removed_nodes={len(self.removed_nodes)},\n"
            f"  kept_nodes={len(self.kept_nodes)}\n"
            f")"
        )

    def __repr__(self) -> str:
        return self.summary()
