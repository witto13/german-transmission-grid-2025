"""
Small hand-crafted networks for unit testing topology and reduction logic.

Each function returns a dict with:
    - 'edges': list of (bus0, bus1) tuples
    - 'protected': set of protected bus IDs
    - 'lines': list of line dicts (for electrical param tests)
    - 'description': human-readable description
"""


def simple_chain():
    """A-B-C-D linear chain. B and C are eliminable.

    A(1) --- B(2) --- C(3) --- D(4)
    Protected: {1, 4}
    Eliminable degree-2: {2, 3}
    Expected chain: [1, 2, 3, 4]
    """
    return {
        'edges': [(1, 2), (2, 3), (3, 4)],
        'protected': {1, 4},
        'lines': [
            {'line_id': 1, 'bus0': 1, 'bus1': 2, 'r': 1.0, 'x': 3.0, 's_nom': 200.0, 'length': 10.0},
            {'line_id': 2, 'bus0': 2, 'bus1': 3, 'r': 2.0, 'x': 6.0, 's_nom': 150.0, 'length': 15.0},
            {'line_id': 3, 'bus0': 3, 'bus1': 4, 'r': 1.5, 'x': 4.5, 's_nom': 180.0, 'length': 12.0},
        ],
        'description': 'Simple 4-bus linear chain, B and C eliminable',
    }


def parallel_chain():
    """A=2=B=3=C=2=D -- each segment has parallel lines. B/C eliminable.

    A(1) ==2== B(2) ==3== C(3) ==2== D(4)
    Protected: {1, 4}
    Eliminable degree-2: {2, 3} (on compressed graph)
    """
    return {
        'edges': [
            (1, 2), (1, 2),  # 2 parallel
            (2, 3), (2, 3), (2, 3),  # 3 parallel
            (3, 4), (3, 4),  # 2 parallel
        ],
        'protected': {1, 4},
        'lines': [
            # Segment A-B: 2 parallel lines
            {'line_id': 1, 'bus0': 1, 'bus1': 2, 'r': 2.0, 'x': 6.0, 's_nom': 100.0, 'length': 10.0},
            {'line_id': 2, 'bus0': 1, 'bus1': 2, 'r': 2.0, 'x': 6.0, 's_nom': 100.0, 'length': 10.0},
            # Segment B-C: 3 parallel lines
            {'line_id': 3, 'bus0': 2, 'bus1': 3, 'r': 3.0, 'x': 9.0, 's_nom': 80.0, 'length': 20.0},
            {'line_id': 4, 'bus0': 2, 'bus1': 3, 'r': 3.0, 'x': 9.0, 's_nom': 80.0, 'length': 20.0},
            {'line_id': 5, 'bus0': 2, 'bus1': 3, 'r': 3.0, 'x': 9.0, 's_nom': 80.0, 'length': 20.0},
            # Segment C-D: 2 parallel lines
            {'line_id': 6, 'bus0': 3, 'bus1': 4, 'r': 1.0, 'x': 3.0, 's_nom': 150.0, 'length': 5.0},
            {'line_id': 7, 'bus0': 3, 'bus1': 4, 'r': 1.0, 'x': 3.0, 's_nom': 150.0, 'length': 5.0},
        ],
        'description': 'Chain with parallel lines at each segment',
    }


def protected_degree2():
    """Degree-2 bus that is a substation -- must NOT be eliminated.

    A(1) --- B(2) --- C(3)
    Protected: {1, 2, 3}  (bus 2 is a substation)
    Eliminable degree-2: {} (empty -- B is protected)
    """
    return {
        'edges': [(1, 2), (2, 3)],
        'protected': {1, 2, 3},
        'lines': [
            {'line_id': 1, 'bus0': 1, 'bus1': 2, 'r': 1.0, 'x': 3.0, 's_nom': 200.0, 'length': 10.0},
            {'line_id': 2, 'bus0': 2, 'bus1': 3, 'r': 1.5, 'x': 4.5, 's_nom': 180.0, 'length': 12.0},
        ],
        'description': 'All buses protected, no elimination possible',
    }


def branching_node():
    """Hub-and-spoke: center bus has degree 3 (not eliminable).

    B(2)
     |
    A(1)---C(3)
     |
    D(4)

    Protected: {2, 3, 4}
    Bus 1 has degree 3 -> not a degree-2 candidate
    Eliminable degree-2: {}
    """
    return {
        'edges': [(1, 2), (1, 3), (1, 4)],
        'protected': {2, 3, 4},
        'lines': [
            {'line_id': 1, 'bus0': 1, 'bus1': 2, 'r': 1.0, 'x': 3.0, 's_nom': 200.0, 'length': 5.0},
            {'line_id': 2, 'bus0': 1, 'bus1': 3, 'r': 1.0, 'x': 3.0, 's_nom': 200.0, 'length': 5.0},
            {'line_id': 3, 'bus0': 1, 'bus1': 4, 'r': 1.0, 'x': 3.0, 's_nom': 200.0, 'length': 5.0},
        ],
        'description': 'Center bus has degree 3, not eliminable',
    }


def dead_end():
    """Dead-end bus (degree 1) -- not a degree-2 candidate.

    A(1) --- B(2) --- C(3)
                       |
                      D(4) (dead end, degree 1)
    Protected: {1, 2}
    Bus 4 has degree 1 -> not a degree-2 candidate
    Bus 3 has degree 2 but connects to dead end
    Eliminable degree-2: {3} (degree-2, not protected)
    Chain: [2, 3, 4] -- but 4 is degree-1 endpoint
    """
    return {
        'edges': [(1, 2), (2, 3), (3, 4)],
        'protected': {1, 2},
        'lines': [
            {'line_id': 1, 'bus0': 1, 'bus1': 2, 'r': 1.0, 'x': 3.0, 's_nom': 200.0, 'length': 10.0},
            {'line_id': 2, 'bus0': 2, 'bus1': 3, 'r': 2.0, 'x': 6.0, 's_nom': 150.0, 'length': 15.0},
            {'line_id': 3, 'bus0': 3, 'bus1': 4, 'r': 1.5, 'x': 4.5, 's_nom': 180.0, 'length': 12.0},
        ],
        'description': 'Dead-end bus (degree 1) with eliminable intermediate',
    }


def multi_component():
    """Two disconnected subgraphs.

    Component 1: A(1) --- B(2) --- C(3)
    Component 2: D(10) --- E(11) --- F(12)
    Protected: {1, 3, 10, 12}
    Eliminable degree-2: {2, 11}
    Chains: [1, 2, 3], [10, 11, 12]
    """
    return {
        'edges': [(1, 2), (2, 3), (10, 11), (11, 12)],
        'protected': {1, 3, 10, 12},
        'lines': [
            {'line_id': 1, 'bus0': 1, 'bus1': 2, 'r': 1.0, 'x': 3.0, 's_nom': 200.0, 'length': 10.0},
            {'line_id': 2, 'bus0': 2, 'bus1': 3, 'r': 2.0, 'x': 6.0, 's_nom': 150.0, 'length': 15.0},
            {'line_id': 3, 'bus0': 10, 'bus1': 11, 'r': 1.5, 'x': 4.5, 's_nom': 180.0, 'length': 12.0},
            {'line_id': 4, 'bus0': 11, 'bus1': 12, 'r': 0.5, 'x': 1.5, 's_nom': 250.0, 'length': 8.0},
        ],
        'description': 'Two disconnected components',
    }


def diamond():
    r"""Diamond topology: no degree-2 buses.

        B(2)
       / \
    A(1)   D(4)
       \ /
        C(3)

    All buses have degree 2, but if A and D are protected,
    B and C have degree 2 but are in parallel paths (not a chain).
    After parallel compression: A-B-D and A-C-D become single edges
    A has degree 2 neighbors {B, C}, but on compressed graph A has
    degree 2 neighbors {B, C}... Actually on compressed graph with
    parallel compression of A->B,A->C edges (they go to different nodes),
    A still has 2 neighbors. But B and C each have 2 neighbors too.

    Protected: {1, 4}
    Eliminable degree-2: {2, 3}
    But chains: [1, 2, 4] and [1, 3, 4] -- two separate chains
    """
    return {
        'edges': [(1, 2), (2, 4), (1, 3), (3, 4)],
        'protected': {1, 4},
        'lines': [
            {'line_id': 1, 'bus0': 1, 'bus1': 2, 'r': 1.0, 'x': 3.0, 's_nom': 200.0, 'length': 10.0},
            {'line_id': 2, 'bus0': 2, 'bus1': 4, 'r': 1.5, 'x': 4.5, 's_nom': 180.0, 'length': 12.0},
            {'line_id': 3, 'bus0': 1, 'bus1': 3, 'r': 2.0, 'x': 6.0, 's_nom': 150.0, 'length': 15.0},
            {'line_id': 4, 'bus0': 3, 'bus1': 4, 'r': 1.0, 'x': 3.0, 's_nom': 200.0, 'length': 10.0},
        ],
        'description': 'Diamond with two parallel paths, both eliminable',
    }
