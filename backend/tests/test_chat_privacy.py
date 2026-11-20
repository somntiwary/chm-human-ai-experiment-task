import unittest
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from app import db
from app.config import ExperimentConfig
from app.experiment import ChatAction, Experiment


@asynccontextmanager
async def transaction(_):
    yield None


class ChatPrivacyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.experiment = Experiment("unused", ExperimentConfig(("human", "ai", "ai"), 60))
        self.session = {
            "id": 2, "phase": "discussion", "round_number": 1,
            "deadline": datetime.now(UTC) + timedelta(seconds=60), "reconnect_deadline": None,
        }

    async def test_an_ai_vote_announcement_cannot_be_saved_as_chat(self):
        action = ChatAction(type="message", round=1, text="I will vote for Candidate A.")
        with (
            patch.object(db, "transaction", transaction),
            patch.object(db, "session_for_update", AsyncMock(return_value=self.session)),
            patch.object(db, "record_message", AsyncMock()) as save,
        ):
            self.assertEqual(await self.experiment._submit(2, "B", action, ai=True), "ballot_disclosure")
        save.assert_not_awaited()

    async def test_round_two_regressions_cannot_be_saved_or_broadcast_in_either_round(self):
        invalid_messages = (
            ("I will proceed to vote for myself as the Decision Maker.", "ballot_disclosure"),
            ("I will proceed to choose myself as the Decision Maker. I believe this is the most appropriate choice given the current circumstances.", "ballot_disclosure"),
            (
                "I will proceed to vote for myself as the Decision Maker. "
                "Let's move forward with the public discussion.",
                "ballot_disclosure",
            ),
            (
                "I cannot vote for myself as the Decision Maker, "
                "as I am not eligible to vote in this process.",
                "incorrect_eligibility",
            ),
        )
        for round_number in (1, 2):
            session = {**self.session, "round_number": round_number}
            for slot in ("A", "B", "C"):
                for text, reason in invalid_messages:
                    with (
                        self.subTest(round_number=round_number, slot=slot, text=text),
                        patch.object(db, "transaction", transaction),
                        patch.object(db, "session_for_update", AsyncMock(return_value=session)),
                        patch.object(db, "messages_for_round", AsyncMock(return_value=[])),
                        patch.object(db, "ai_slots", AsyncMock(return_value=[slot])),
                        patch.object(db, "record_message", AsyncMock()) as save,
                        patch.object(self.experiment, "_view", AsyncMock(return_value=({}, set(), set()))),
                        patch.object(self.experiment, "_publish") as broadcast,
                    ):
                        action = ChatAction(type="message", round=round_number, text=text)
                        self.assertEqual(
                            await self.experiment._submit(2, slot, action, ai=True), reason,
                        )
                        save.assert_not_awaited()
                        broadcast.assert_not_called()

    async def test_legacy_invalid_ai_messages_are_hidden_without_deleting_human_chat(self):
        disclosure = "I will proceed to vote for myself as the Decision Maker."
        rows = [
            {"id": 1, "sender_slot": "B", "text": disclosure},
            {
                "id": 2, "sender_slot": "C",
                "text": "I cannot vote for myself as the Decision Maker, as I am not eligible to vote in this process.",
            },
            {"id": 3, "sender_slot": "A", "text": disclosure},
            {"id": 4, "sender_slot": "C", "text": "I'd look at the reasons each person has given."},
        ]
        for round_number in (1, 2):
            for phase in ("discussion", "voting"):
                with (
                    self.subTest(round_number=round_number, phase=phase),
                    patch.object(db, "messages_for_round", AsyncMock(return_value=rows)),
                    patch.object(db, "ai_slots", AsyncMock(return_value=["B", "C"])),
                    patch.object(db, "voters_for_round", AsyncMock(return_value=set())),
                    patch.object(db, "record_message", AsyncMock()) as save,
                ):
                    snapshot, _, _ = await self.experiment._view(
                        None, {**self.session, "phase": phase, "round_number": round_number},
                    )
                    self.assertEqual(snapshot["messages"], [
                        {"id": message["id"], "sender": message["sender_slot"], "text": message["text"]}
                        for message in rows[2:]
                    ])
                    self.assertNotIn("ballots", snapshot)
                    save.assert_not_awaited()

    async def test_voting_state_hides_a_legacy_ai_announcement_but_preserves_human_chat(self):
        rows = [
            {"id": 2, "sender_slot": "B", "text": "I will vote for Candidate A."},
            {"id": 3, "sender_slot": "A", "text": "I will vote for Candidate A."},
            {"id": 4, "sender_slot": "C", "text": "I'd look for who listens to others."},
        ]
        voting = {**self.session, "phase": "voting", "deadline": None}
        with (
            patch.object(db, "messages_for_round", AsyncMock(return_value=rows)),
            patch.object(db, "ai_slots", AsyncMock(return_value=["B", "C"])),
            patch.object(db, "voters_for_round", AsyncMock(return_value={"B"})),
        ):
            snapshot, _, _ = await self.experiment._view(None, voting)
        self.assertEqual(snapshot["messages"], [
            {"id": row["id"], "sender": row["sender_slot"], "text": row["text"]} for row in rows[1:]
        ])
        self.assertNotIn("ballots", snapshot)
