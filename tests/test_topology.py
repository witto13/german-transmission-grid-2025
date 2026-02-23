"""
Unit tests for topology graph engine.

Tests degree computation, degree-2 bus detection, chain finding,
and connected component analysis.
"""

import pytest
from scripts.reduction.core.topology import TopologyGraph
from tests.fixtures.sample_networks import (
    simple_chain,
    parallel_chain,
    protected_degree2,
    branching_node,
    dead_end,
    multi_component,
    diamond,
)


class TestTopologyGraphBasics:
    """Test basic graph construction and properties."""

    def test_add_edge(self):
        g = TopologyGraph()
        g.add_edge(1, 2)
        assert g.topological_degree(1) == 1
        assert g.topological_degree(2) == 1

    def test_add_duplicate_edge(self):
        """Adding the same edge twice doesn't increase degree."""
        g = TopologyGraph()
        g.add_edge(1, 2)
        g.add_edge(1, 2)
        assert g.topological_degree(1) == 1
        assert g.topological_degree(2) == 1

    def test_self_loop_ignored(self):
        g = TopologyGraph()
        g.add_edge(1, 1)
        assert g.topological_degree(1) == 0

    def test_unknown_node_degree_zero(self):
        g = TopologyGraph()
        assert g.topological_degree(999) == 0

    def test_from_edges(self):
        g = TopologyGraph.from_edges([(1, 2), (2, 3), (3, 1)])
        assert g.topological_degree(1) == 2
        assert g.topological_degree(2) == 2
        assert g.topological_degree(3) == 2

    def test_neighbors(self):
        g = TopologyGraph.from_edges([(1, 2), (1, 3), (1, 4)])
        assert g.neighbors(1) == {2, 3, 4}
        assert g.neighbors(2) == {1}

    def test_nodes_property(self):
        g = TopologyGraph.from_edges([(1, 2), (3, 4)])
        assert g.nodes == {1, 2, 3, 4}

    def test_num_nodes(self):
        g = TopologyGraph.from_edges([(1, 2), (3, 4)])
        assert g.num_nodes == 4

    def test_add_node_isolated(self):
        g = TopologyGraph()
        g.add_node(5)
        assert 5 in g.nodes
        assert g.topological_degree(5) == 0


class TestDegreeComputation:
    """Test degree computation on sample networks."""

    def test_simple_chain_degrees(self):
        net = simple_chain()
        g = TopologyGraph.from_edges(net['edges'])
        assert g.topological_degree(1) == 1  # endpoint
        assert g.topological_degree(2) == 2  # pass-through
        assert g.topological_degree(3) == 2  # pass-through
        assert g.topological_degree(4) == 1  # endpoint

    def test_parallel_chain_degrees(self):
        """Parallel edges don't increase topological degree."""
        net = parallel_chain()
        g = TopologyGraph.from_edges(net['edges'])
        assert g.topological_degree(1) == 1  # only connects to 2
        assert g.topological_degree(2) == 2  # connects to 1 and 3
        assert g.topological_degree(3) == 2  # connects to 2 and 4
        assert g.topological_degree(4) == 1  # only connects to 3

    def test_branching_node_degrees(self):
        net = branching_node()
        g = TopologyGraph.from_edges(net['edges'])
        assert g.topological_degree(1) == 3  # hub
        assert g.topological_degree(2) == 1
        assert g.topological_degree(3) == 1
        assert g.topological_degree(4) == 1

    def test_diamond_degrees(self):
        net = diamond()
        g = TopologyGraph.from_edges(net['edges'])
        assert g.topological_degree(1) == 2
        assert g.topological_degree(2) == 2
        assert g.topological_degree(3) == 2
        assert g.topological_degree(4) == 2


class TestFindDegree2Buses:
    """Test finding unprotected degree-2 buses."""

    def test_simple_chain(self):
        net = simple_chain()
        g = TopologyGraph.from_edges(net['edges'])
        result = g.find_degree2_buses(net['protected'])
        assert result == {2, 3}

    def test_parallel_chain(self):
        net = parallel_chain()
        g = TopologyGraph.from_edges(net['edges'])
        result = g.find_degree2_buses(net['protected'])
        assert result == {2, 3}

    def test_protected_degree2(self):
        net = protected_degree2()
        g = TopologyGraph.from_edges(net['edges'])
        result = g.find_degree2_buses(net['protected'])
        assert result == set()

    def test_branching_node_no_degree2(self):
        net = branching_node()
        g = TopologyGraph.from_edges(net['edges'])
        result = g.find_degree2_buses(net['protected'])
        # Bus 1 has degree 3, buses 2,3,4 have degree 1
        assert result == set()

    def test_dead_end(self):
        net = dead_end()
        g = TopologyGraph.from_edges(net['edges'])
        result = g.find_degree2_buses(net['protected'])
        assert result == {3}

    def test_multi_component(self):
        net = multi_component()
        g = TopologyGraph.from_edges(net['edges'])
        result = g.find_degree2_buses(net['protected'])
        assert result == {2, 11}

    def test_diamond(self):
        net = diamond()
        g = TopologyGraph.from_edges(net['edges'])
        result = g.find_degree2_buses(net['protected'])
        assert result == {2, 3}

    def test_no_protected(self):
        """Without protection, all degree-2 nodes are eliminable."""
        g = TopologyGraph.from_edges([(1, 2), (2, 3)])
        result = g.find_degree2_buses()
        assert result == {2}


class TestFindChains:
    """Test chain detection."""

    def test_simple_chain(self):
        net = simple_chain()
        g = TopologyGraph.from_edges(net['edges'])
        eliminable = g.find_degree2_buses(net['protected'])
        chains = g.find_chains(eliminable)
        assert len(chains) == 1
        chain = chains[0]
        # Chain should be [1, 2, 3, 4] (endpoints are non-eliminable)
        assert chain[0] in {1, 4}
        assert chain[-1] in {1, 4}
        assert chain[0] != chain[-1]
        assert set(chain) == {1, 2, 3, 4}

    def test_parallel_chain(self):
        net = parallel_chain()
        g = TopologyGraph.from_edges(net['edges'])
        eliminable = g.find_degree2_buses(net['protected'])
        chains = g.find_chains(eliminable)
        assert len(chains) == 1
        chain = chains[0]
        assert chain[0] in {1, 4}
        assert chain[-1] in {1, 4}
        assert set(chain) == {1, 2, 3, 4}

    def test_no_eliminable_no_chains(self):
        net = protected_degree2()
        g = TopologyGraph.from_edges(net['edges'])
        eliminable = g.find_degree2_buses(net['protected'])
        chains = g.find_chains(eliminable)
        assert chains == []

    def test_multi_component_chains(self):
        net = multi_component()
        g = TopologyGraph.from_edges(net['edges'])
        eliminable = g.find_degree2_buses(net['protected'])
        chains = g.find_chains(eliminable)
        assert len(chains) == 2
        chain_sets = [set(c) for c in chains]
        assert {1, 2, 3} in chain_sets
        assert {10, 11, 12} in chain_sets

    def test_diamond_chains(self):
        net = diamond()
        g = TopologyGraph.from_edges(net['edges'])
        eliminable = g.find_degree2_buses(net['protected'])
        chains = g.find_chains(eliminable)
        assert len(chains) == 2
        # Two chains: [1, 2, 4] and [1, 3, 4]
        chain_sets = [set(c) for c in chains]
        assert {1, 2, 4} in chain_sets
        assert {1, 3, 4} in chain_sets

    def test_chain_endpoints_are_non_eliminable(self):
        """Chain endpoints must not be in the eliminable set."""
        net = simple_chain()
        g = TopologyGraph.from_edges(net['edges'])
        eliminable = g.find_degree2_buses(net['protected'])
        chains = g.find_chains(eliminable)
        for chain in chains:
            assert chain[0] not in eliminable
            assert chain[-1] not in eliminable

    def test_dead_end_chain(self):
        """Chain with a degree-1 endpoint."""
        net = dead_end()
        g = TopologyGraph.from_edges(net['edges'])
        eliminable = g.find_degree2_buses(net['protected'])
        chains = g.find_chains(eliminable)
        assert len(chains) == 1
        chain = chains[0]
        assert set(chain) == {2, 3, 4}
        # Endpoints: 2 (protected, not eliminable) and 4 (degree-1, not eliminable)
        assert chain[0] in {2, 4}
        assert chain[-1] in {2, 4}


class TestConnectedComponents:
    """Test connected component detection."""

    def test_single_component(self):
        net = simple_chain()
        g = TopologyGraph.from_edges(net['edges'])
        components = g.connected_components()
        assert len(components) == 1
        assert components[0] == {1, 2, 3, 4}

    def test_two_components(self):
        net = multi_component()
        g = TopologyGraph.from_edges(net['edges'])
        components = g.connected_components()
        assert len(components) == 2
        comp_sets = [c for c in components]
        assert {1, 2, 3} in comp_sets
        assert {10, 11, 12} in comp_sets

    def test_isolated_node(self):
        g = TopologyGraph()
        g.add_node(1)
        g.add_edge(2, 3)
        components = g.connected_components()
        assert len(components) == 2

    def test_diamond_single_component(self):
        net = diamond()
        g = TopologyGraph.from_edges(net['edges'])
        components = g.connected_components()
        assert len(components) == 1


class TestBFSReachability:
    """Test BFS reachability between source nodes."""

    def test_simple_chain_reachability(self):
        net = simple_chain()
        g = TopologyGraph.from_edges(net['edges'])
        sources = {1, 4}
        reach = g.bfs_reachability(sources)
        assert 4 in reach[1]
        assert 1 in reach[4]

    def test_multi_component_reachability(self):
        net = multi_component()
        g = TopologyGraph.from_edges(net['edges'])
        sources = {1, 3, 10, 12}
        reach = g.bfs_reachability(sources)
        # Component 1
        assert 3 in reach[1]
        assert 1 in reach[3]
        # Component 2
        assert 12 in reach[10]
        assert 10 in reach[12]
        # Cross-component unreachable
        assert 10 not in reach[1]
        assert 1 not in reach[10]

    def test_unknown_source(self):
        g = TopologyGraph.from_edges([(1, 2)])
        reach = g.bfs_reachability({999})
        assert reach[999] == set()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
