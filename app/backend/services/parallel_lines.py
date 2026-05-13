"""Tag each line with its parallel-group size and index within the group.

Two lines are 'parallel' if they share the same (bus0, bus1) endpoints
(unordered) and the same v_nom.
"""

from collections import defaultdict
from typing import Iterable


def annotate(lines: Iterable[dict]) -> list[dict]:
    """Mutate-and-return list with 'parallel_count' and 'parallel_index' keys.

    Each line dict is expected to have at least 'bus0', 'bus1', 'v_nom'.
    """
    groups: dict[tuple, list] = defaultdict(list)
    for line in lines:
        b0, b1 = line["bus0"], line["bus1"]
        key = (min(b0, b1), max(b0, b1), line.get("v_nom"))
        groups[key].append(line)

    out = []
    for key, members in groups.items():
        for idx, line in enumerate(members):
            line["parallel_count"] = len(members)
            line["parallel_index"] = idx
            out.append(line)
    return out
