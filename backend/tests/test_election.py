import unittest
from unittest.mock import patch

from app.election import Ballot, ElectionResult, elect


SLOTS = ("A", "B", "C")


def ballots(round_one: str, round_two: str) -> list[Ballot]:
    return [
        {"round_number": round_number, "voter_slot": voter, "candidate_slot": candidate}
        for round_number, votes in enumerate((round_one, round_two), start=1)
        for voter, candidate in zip(SLOTS, votes, strict=True)
    ]


class ElectionTests(unittest.TestCase):
    def test_total_wins_even_when_another_player_has_more_round_two_votes(self):
        with patch("app.election.secrets.choice") as draw:
            result = elect(SLOTS, ballots("AAA", "BCB"))

        self.assertEqual(result, ElectionResult("A", "total_votes", {"A": 3, "B": 2, "C": 1}))
        draw.assert_not_called()

    def test_round_two_self_vote_breaks_the_total_tie(self):
        with patch("app.election.secrets.choice") as draw:
            result = elect(SLOTS, ballots("ABB", "AAB"))

        self.assertEqual(result, ElectionResult("A", "round_two_votes", {"A": 3, "B": 3, "C": 0}))
        draw.assert_not_called()

    def test_votes_from_others_break_the_remaining_tie(self):
        with patch("app.election.secrets.choice") as draw:
            result = elect(SLOTS, ballots("ACB", "CBA"))

        self.assertEqual(result, ElectionResult("C", "votes_from_others", {"A": 2, "B": 2, "C": 2}))
        draw.assert_not_called()

    def test_complete_tie_draws_from_all_three_players(self):
        with patch("app.election.secrets.choice", return_value="C") as draw:
            result = elect(SLOTS, ballots("BCA", "CAB"))

        self.assertEqual(result, ElectionResult("C", "random_draw", {"A": 2, "B": 2, "C": 2}))
        draw.assert_called_once_with(("A", "B", "C"))

    def test_draw_uses_only_unique_remaining_candidates(self):
        with patch("app.election.secrets.choice", return_value="B") as draw:
            result = elect(("C", "B", "A", "A"), ballots("BAC", "BAC"))

        self.assertEqual(result, ElectionResult("B", "random_draw", {"A": 2, "B": 2, "C": 2}))
        draw.assert_called_once_with(("A", "B"))
