import itertools
import unittest

from app.vote_context import select_vote_evidence


def message(index, slot, round_number=1, size=70):
    return {
        "id": index, "sender_slot": slot, "round_number": round_number,
        "text": f"Contribution {index}: " + "x" * size,
    }


class VoteEvidenceTests(unittest.TestCase):
    def test_small_transcript_keeps_complete_originals_in_order(self):
        rows = [message(1, "C"), message(2, "A"), message(3, "B")]
        self.assertEqual(select_vote_evidence(rows, 1), rows)
        for actual, original in zip(select_vote_evidence(rows, 1), rows):
            self.assertIs(actual, original)

    def test_quiet_early_contributions_survive_a_longer_tail_of_chat(self):
        rows = [message(1, "A", 1), message(2, "C", 1)]
        rows += [message(index, "B" if index % 2 else "C", 1, 110) for index in range(3, 44)]
        rows += [message(44, "A", 2)]
        rows += [message(index, "B" if index % 2 else "C", 2, 110) for index in range(45, 86)]
        selected = select_vote_evidence(rows, 2)
        self.assertIn(rows[0], selected)
        self.assertIn(rows[43], selected)
        self.assertEqual({row["sender_slot"] for row in selected}, {"A", "B", "C"})
        self.assertEqual({row["round_number"] for row in selected}, {1, 2})

    def test_each_round_and_player_can_keep_initial_and_latest_contributions(self):
        rows = []
        endpoints = []
        for round_number in (1, 2):
            for slot in ("B", "C", "A"):
                group = [message(len(rows) + offset, slot, round_number, 110) for offset in range(8)]
                rows.extend(group)
                endpoints.extend((group[0], group[-1]))
        selected = select_vote_evidence(rows, 2)
        for row in endpoints:
            self.assertIn(row, selected)
        self.assertEqual(selected, sorted(selected, key=lambda row: row["id"]))

    def test_selection_is_equivariant_when_candidate_labels_are_swapped(self):
        rows = [message(index, ("C", "A", "B")[index % 3], 1 + (index // 24), 105) for index in range(48)]
        expected_ids = [row["id"] for row in select_vote_evidence(rows, 2)]
        for labels in itertools.permutations(("A", "B", "C")):
            mapping = dict(zip(("A", "B", "C"), labels))
            renamed = [{**row, "sender_slot": mapping[row["sender_slot"]]} for row in rows]
            with self.subTest(labels=labels):
                self.assertEqual([row["id"] for row in select_vote_evidence(renamed, 2)], expected_ids)

    def test_unused_reservations_are_available_without_dropping_whole_messages(self):
        rows = [message(index, "B", size=340) for index in range(5)]
        self.assertGreater(sum(len(row["text"].encode()) + 48 for row in rows), 1000)
        self.assertEqual(select_vote_evidence(rows, 1), rows)

    def test_budget_is_respected_and_oversized_utf8_messages_are_not_clipped(self):
        oversized = {**message(1, "A"), "text": "🚲" * 500}
        rows = [oversized, message(2, "B"), message(3, "C"), message(4, "B", 2)]
        for budget in (0, 1, 47, 48, 149, 500, 1000, 3000):
            with self.subTest(budget=budget):
                selected = select_vote_evidence(rows, 2, budget)
                self.assertLessEqual(sum(len(row["text"].encode("utf-8")) + 48 for row in selected), budget)
                self.assertTrue(all(any(row is original for original in rows) for row in selected))
                if budget < 2048:
                    self.assertNotIn(oversized, selected)
        self.assertEqual(oversized["text"], "🚲" * 500)

    def test_future_rounds_and_unknown_players_are_excluded(self):
        rows = [message(1, "A"), message(2, "B", 2), message(3, "C", 3), message(4, "D")]
        self.assertEqual(select_vote_evidence(rows, 1), rows[:1])
        self.assertEqual(select_vote_evidence(rows, 2), rows[:2])

    def test_invalid_round_or_budget_is_rejected(self):
        for round_number, budget in ((0, 3000), (3, 3000), (1, -1)):
            with self.subTest(round_number=round_number, budget=budget), self.assertRaises(ValueError):
                select_vote_evidence([], round_number, budget)
