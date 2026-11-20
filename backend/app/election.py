import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TypedDict


class Ballot(TypedDict):
    round_number: int
    voter_slot: str
    candidate_slot: str


@dataclass(frozen=True)
class ElectionResult:
    winner: str
    rule: str
    totals: dict[str, int]


def elect(candidates: Iterable[str], ballots: Iterable[Ballot]) -> ElectionResult:
    """Tally received votes and narrow the field one rule at a time."""
    remaining = tuple(sorted(set(candidates)))
    if not remaining:
        raise ValueError("The election needs at least one candidate.")

    totals = dict.fromkeys(remaining, 0)
    round_two = dict.fromkeys(remaining, 0)
    from_others = dict.fromkeys(remaining, 0)
    for ballot in ballots:
        candidate = ballot["candidate_slot"]
        totals[candidate] += 1
        if ballot["round_number"] == 2:
            round_two[candidate] += 1
        if ballot["voter_slot"] != candidate:
            from_others[candidate] += 1

    for rule, counts in (
        ("total_votes", totals),
        ("round_two_votes", round_two),
        ("votes_from_others", from_others),
    ):
        highest = max(counts[candidate] for candidate in remaining)
        remaining = tuple(candidate for candidate in remaining if counts[candidate] == highest)
        if len(remaining) == 1:
            return ElectionResult(remaining[0], rule, totals)

    return ElectionResult(secrets.choice(remaining), "random_draw", totals)
