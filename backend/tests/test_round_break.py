import unittest
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from app import db
from app.config import ExperimentConfig
from app.experiment import BallotAction, Experiment, ParticipantConnection, ReadyAction


@asynccontextmanager
async def transaction(_):
    yield None


class RoundBreakTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.experiment = Experiment("unused", ExperimentConfig(("human", "human", "ai"), 60))
        self.session = {
            "id": 1, "phase": "break", "round_number": 1, "discussion_seconds": 60,
            "deadline": None, "break_deadline": None, "reconnect_deadline": None,
        }
        self.ready = ReadyAction(type="ready", round=1)

    async def test_last_round_one_ballot_starts_ten_second_break(self):
        ballot = BallotAction(type="ballot", round=1, candidate="B")
        voting = {**self.session, "phase": "voting"}
        with (
            patch.object(db, "record_ballot", AsyncMock(return_value=True)),
            patch.object(db, "voters_for_round", AsyncMock(return_value={"A", "B", "C"})),
            patch.object(db, "change_phase", AsyncMock(return_value=self.session)) as change_phase,
        ):
            earliest = datetime.now(UTC) + timedelta(seconds=10)
            updated, error = await self.experiment._apply(None, voting, "A", ballot)
            latest = datetime.now(UTC) + timedelta(seconds=10)
        self.assertEqual(updated["phase"], "break")
        self.assertIsNone(error)
        self.assertEqual(change_phase.call_args.args, (None, 1, "break", 1))
        self.assertLessEqual(earliest, change_phase.call_args.kwargs["break_deadline"])
        self.assertLessEqual(change_phase.call_args.kwargs["break_deadline"], latest)

    async def test_countdown_rejects_early_readiness(self):
        self.session["break_deadline"] = datetime.now(UTC) + timedelta(seconds=2)
        with patch.object(db, "record_round_2_ready", AsyncMock()) as record:
            updated, error = await self.experiment._apply(None, self.session, "A", self.ready)
        self.assertIs(updated, self.session)
        self.assertIsNotNone(error)
        record.assert_not_awaited()

    async def test_countdown_broadcasts_wait_with_own_readiness(self):
        first, second = ParticipantConnection(1, "A"), ParticipantConnection(1, "B")
        self.experiment._connections.update((first, second))
        expired = {**self.session, "break_deadline": datetime.now(UTC) - timedelta(seconds=1)}
        with (
            patch.object(db, "transaction", transaction),
            patch.object(db, "active_session", AsyncMock(return_value=expired)),
            patch.object(db, "change_phase", AsyncMock(return_value=self.session)) as change_phase,
            patch.object(db, "round_2_readiness", AsyncMock(return_value={"A": True, "B": False})),
        ):
            self.assertIsNone(await self.experiment._check_deadline())
        change_phase.assert_awaited_once_with(None, 1, "break", 1)
        first_snapshot = first.updates.get_nowait()
        second_snapshot = second.updates.get_nowait()
        self.assertEqual(first_snapshot["phase"], "break")
        self.assertIsNone(first_snapshot["deadline"])
        self.assertTrue(first_snapshot["round_2_ready"])
        self.assertFalse(second_snapshot["round_2_ready"])

    async def test_round_two_starts_only_once_all_humans_are_ready(self):
        discussion = {**self.session, "phase": "discussion", "round_number": 2}
        for readiness, expected in [
            ({"A": True, "B": False}, self.session),
            ({"A": True, "B": True}, discussion),
            ({"A": True}, discussion),
        ]:
            with (
                self.subTest(readiness=readiness),
                patch.object(db, "record_round_2_ready", AsyncMock(return_value=True)),
                patch.object(db, "round_2_readiness", AsyncMock(return_value=readiness)),
                patch.object(self.experiment, "_start_discussion", AsyncMock(return_value=discussion)) as start,
            ):
                updated, error = await self.experiment._apply(None, self.session, "A", self.ready)
                self.assertIs(updated, expected)
                self.assertIsNone(error)
                if expected is discussion:
                    start.assert_awaited_once_with(None, self.session, 2)
                else:
                    start.assert_not_awaited()
