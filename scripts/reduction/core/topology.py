"""
Topology graph engine for network reduction.

Provides adjacency-based graph analysis: degree computation, degree-2 bus
identification, chain detection, and connected component analysis.
"""

from collections import defaultdict, deque
from typing import Dict, List, Set, Optional, Tuple


class TopologyGraph:
    """Undirected graph for network topology analysis.

    Stores adjacency as sets of distinct neighbors per node.
    Parallel edges between the same pair are collapsed to a single edge.
    """

    def __init__(self):
        self._adj: Dict[int, Set[int]] = defaultdict(set)

    def add_edge(self, bus_a: int, bus_b: int) -> None:
        """Add an undirected edge between bus_a and bus_b.

        Self-loops (bus_a == bus_b) are ignored.
        """
        if bus_a == bus_b:
            return
        self._adj[bus_a].add(bus_b)
        self._adj[bus_b].add(bus_a)

    def add_node(self, bus_id: int) -> None:
        """Ensure a node exists in the graph (even if isolated)."""
        if bus_id not in self._adj:
            self._adj[bus_id] = set()

    @property
    def nodes(self) -> Set[int]:
        """Return all nodes in the graph."""
        return set(self._adj.keys())

    @property
    def num_nodes(self) -> int:
        return len(self._adj)

    def topological_degree(self, bus_id: int) -> int:
        """Return the number of distinct neighbors of bus_id.

        Returns 0 for unknown nodes.
        """
        return len(self._adj.get(bus_id, set()))

    def neighbors(self, bus_id: int) -> Set[int]:
        """Return set of neighbors for bus_id."""
        return set(self._adj.get(bus_id, set()))

    def find_degree2_buses(self, protected: Optional[Set[int]] = None) -> Set[int]:
        """Find all buses with topological degree exactly 2 that are not protected.

        Args:
            protected: Set of bus IDs that must not be eliminated.

        Returns:
            Set of eliminable degree-2 bus IDs.
        """
        if protected is None:
            protected = set()
        result = set()
        for bus_id, neighbors in self._adj.items():
            if len(neighbors) == 2 and bus_id not in protected:
                result.add(bus_id)
        return result

    def find_chains(self, eliminable: Set[int]) -> List[List[int]]:
        """Find maximal chains of consecutive eliminable degree-2 buses.

        Each chain is a list [endpoint_A, mid1, ..., mid_N, endpoint_B]
        where mid_i are all eliminable and endpoints are not.

        Args:
            eliminable: Set of bus IDs eligible for elimination.

        Returns:
            List of chains. Each chain has at least 3 elements
            (2 endpoints + at least 1 eliminable bus).
        """
        visited = set()
        chains = []

        for bus_id in eliminable:
            if bus_id in visited:
                continue

            # Walk in both directions from this eliminable bus
            # to find the full chain
            nbrs = list(self._adj[bus_id])
            if len(nbrs) != 2:
                continue

            # Walk left
            left_chain = self._walk_direction(bus_id, nbrs[0], eliminable, visited)
            # Walk right
            right_chain = self._walk_direction(bus_id, nbrs[1], eliminable, visited)

            # Build full chain: left_endpoint ... bus_id ... right_endpoint
            # left_chain is [bus_id, ..., left_endpoint] reversed
            # right_chain is [bus_id, ..., right_endpoint]
            chain = list(reversed(left_chain)) + [bus_id] + right_chain
            # Remove duplicates of bus_id at boundaries
            # left_chain starts with bus_id neighbor, right_chain starts with bus_id neighbor

            # Actually let me redo this more carefully
            # _walk_direction returns the path from start going toward direction
            # excluding the start itself, ending at the non-eliminable endpoint
            left_path = self._walk_to_endpoint(bus_id, nbrs[0], eliminable, visited)
            right_path = self._walk_to_endpoint(bus_id, nbrs[1], eliminable, visited)

            chain = list(reversed(left_path)) + [bus_id] + right_path

            # Mark all eliminable buses in this chain as visited
            for b in chain:
                if b in eliminable:
                    visited.add(b)

            chains.append(chain)

        return chains

    def _walk_to_endpoint(
        self, start: int, direction: int, eliminable: Set[int], visited: Set[int]
    ) -> List[int]:
        """Walk from start toward direction until hitting a non-eliminable bus.

        Returns path from direction onward (not including start).
        The last element is the non-eliminable endpoint.
        """
        path = []
        prev = start
        current = direction

        while current in eliminable and current not in visited:
            path.append(current)
            # Find the next neighbor that isn't prev
            nbrs = self._adj[current]
            next_buses = [n for n in nbrs if n != prev]
            if len(next_buses) != 1:
                # Should not happen for a degree-2 bus, but be safe
                break
            prev = current
            current = next_buses[0]

        # current is non-eliminable endpoint (or already visited)
        if current not in eliminable:
            path.append(current)
        elif current in visited:
            # Connects to already-processed chain; current is eliminable
            # but this shouldn't happen in normal graphs since chains
            # must end at non-eliminable endpoints
            path.append(current)

        return path

    def _walk_direction(self, start, direction, eliminable, visited):
        """Deprecated - use _walk_to_endpoint instead."""
        return self._walk_to_endpoint(start, direction, eliminable, visited)

    def connected_components(self) -> List[Set[int]]:
        """Find all connected components using BFS.

        Returns:
            List of sets, each set containing bus IDs in one component.
        """
        visited = set()
        components = []

        for node in self._adj:
            if node in visited:
                continue
            component = set()
            queue = deque([node])
            while queue:
                current = queue.popleft()
                if current in visited:
                    continue
                visited.add(current)
                component.add(current)
                for neighbor in self._adj[current]:
                    if neighbor not in visited:
                        queue.append(neighbor)
            components.append(component)

        return components

    def bfs_reachability(self, sources: Set[int]) -> Dict[int, Set[int]]:
        """For each source node, find which other source nodes are reachable.

        Args:
            sources: Set of bus IDs to check reachability between.

        Returns:
            Dict mapping each source to set of reachable sources.
        """
        reachability = {}

        for source in sources:
            if source not in self._adj:
                reachability[source] = set()
                continue

            # BFS from source
            visited = set()
            queue = deque([source])
            reachable_sources = set()

            while queue:
                current = queue.popleft()
                if current in visited:
                    continue
                visited.add(current)

                if current in sources and current != source:
                    reachable_sources.add(current)

                for neighbor in self._adj[current]:
                    if neighbor not in visited:
                        queue.append(neighbor)

            reachability[source] = reachable_sources

        return reachability

    @classmethod
    def from_edges(cls, edges: List[Tuple[int, int]]) -> 'TopologyGraph':
        """Build a TopologyGraph from a list of (bus0, bus1) edges."""
        graph = cls()
        for a, b in edges:
            graph.add_edge(a, b)
        return graph
