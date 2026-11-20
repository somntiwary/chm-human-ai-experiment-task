"""Select bounded, original public evidence without rewarding chat volume."""

from collections.abc import Mapping, Sequence
from typing import TypeVar


_Message = TypeVar("_Message", bound=Mapping)
_SLOTS = {"A", "B", "C"}


def select_vote_evidence(
    transcript: Sequence[_Message], round_number: int, budget: int = 3000,
) -> list[_Message]:
    """Give each round/player an equal reservation, then share unused space.

    Use complete original messages, alternating first/latest within each group.
    Redistribution favors groups represented by fewer bytes, with original
    transcript order breaking ties; no candidate label sets selection priority.
    Return selected rows in their original chronological order. This is still
    an excerpt: a complete message may exceed the available capacity, and equal
    reservations do not guarantee equal representation or unbiased judgment.
    """
    if round_number not in (1, 2) or budget < 0:
        raise ValueError("Voting requires round 1 or 2 and a nonnegative budget.")
    rows = [
        row for row in transcript
        if row["round_number"] in range(1, round_number + 1)
        and row["sender_slot"] in _SLOTS
    ]
    groups: dict[tuple[int, str], list[int]] = {}
    costs = []
    for index, row in enumerate(rows):
        costs.append(len(row["text"].encode("utf-8")) + 48)
        groups.setdefault((row["round_number"], row["sender_slot"]), []).append(index)

    # Endpoints preserve both an initial contribution and a later response when
    # capacity permits, rather than keeping only the most recent chatter.
    priorities = {}
    for key, indices in groups.items():
        order = []
        first, last = 0, len(indices) - 1
        while first <= last:
            order.append(indices[first])
            if first != last:
                order.append(indices[last])
            first, last = first + 1, last - 1
        priorities[key] = order

    selected: set[int] = set()
    used = dict.fromkeys(groups, 0)
    reservation = budget // (3 * round_number)
    for key, order in priorities.items():
        for index in order:
            if used[key] + costs[index] <= reservation:
                selected.add(index)
                used[key] += costs[index]

    remaining = budget - sum(used.values())
    while remaining:
        # Insertion order is the first real contribution, not A/B/C ordering.
        candidates = sorted(groups, key=lambda key: (used[key], groups[key][0]))
        for key in candidates:
            index = next((
                index for index in priorities[key]
                if index not in selected and costs[index] <= remaining
            ), None)
            if index is not None:
                selected.add(index)
                used[key] += costs[index]
                remaining -= costs[index]
                break
        else:
            break
    return [row for index, row in enumerate(rows) if index in selected]
