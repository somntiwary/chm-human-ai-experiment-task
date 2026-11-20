import itertools
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml

from app import db
from app.config import ExperimentConfig, load_experiment_config
from app.experiment import Experiment


class ConfigTests(unittest.TestCase):
    def load(self, participants, seconds):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yml"
            path.write_text(yaml.safe_dump({
                "participants": participants, "discussion_seconds": seconds,
            }), encoding="utf-8")
            return load_experiment_config(path)

    def test_duration_can_be_changed_from_sixty_to_one_hundred_seconds(self):
        for seconds in (1, 60, 100, 2_147_483_647):
            with self.subTest(seconds=seconds):
                config = self.load(["human", "ai", "ai"], seconds)
                self.assertEqual(config.discussion_seconds, seconds)
                self.assertEqual(config.participants, ("human", "ai", "ai"))

    def test_all_supported_compositions_accept_a_custom_duration_in_any_order(self):
        for participants in (
            ("human", "human", "human"),
            ("human", "human", "ai"),
            ("human", "ai", "ai"),
        ):
            for ordered in set(itertools.permutations(participants)):
                with self.subTest(participants=ordered):
                    self.assertEqual(self.load(list(ordered), 100), ExperimentConfig(ordered, 100))

    def test_invalid_durations_are_rejected_instead_of_silently_coerced(self):
        for seconds in (True, False, 0, -1, "100", 100.5, None, 2_147_483_648):
            with self.subTest(seconds=seconds), self.assertRaisesRegex(ValueError, "positive 32-bit integer"):
                self.load(["human", "human", "human"], seconds)

    def test_unsupported_compositions_are_rejected(self):
        for participants in (
            ["ai", "ai", "ai"], ["human", "ai"],
            ["human", "human", "human", "ai"], ["human", "bot", "ai"],
        ):
            with self.subTest(participants=participants), self.assertRaises(ValueError):
                self.load(participants, 100)


class DiscussionDurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_rounds_use_the_duration_saved_with_the_session(self):
        experiment = Experiment("unused", ExperimentConfig(("human", "human", "human"), 100))
        # Editing YAML affects future sessions, not an existing group's saved clock.
        for stored_seconds in (60, 100):
            for round_number in (1, 2):
                session = {"id": 7, "discussion_seconds": stored_seconds}
                with self.subTest(seconds=stored_seconds, round_number=round_number):
                    before = datetime.now(UTC)
                    with patch.object(db, "change_phase", AsyncMock(return_value={})) as change:
                        await experiment._start_discussion(None, session, round_number)
                    after = datetime.now(UTC)
                    connection, session_id, phase, actual_round, deadline = change.await_args.args
                    self.assertEqual((connection, session_id, phase, actual_round), (None, 7, "discussion", round_number))
                    self.assertGreaterEqual(deadline, before + timedelta(seconds=stored_seconds))
                    self.assertLessEqual(deadline, after + timedelta(seconds=stored_seconds))


if __name__ == "__main__":
    unittest.main()
