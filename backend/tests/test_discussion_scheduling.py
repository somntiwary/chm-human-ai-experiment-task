import asyncio
import unittest
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from app import db
from app.config import ExperimentConfig
from app.experiment import ChatAction, Experiment, _autonomous_focus


@asynccontextmanager
async def transaction(_):
    yield None


class DiscussionSchedulingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.opening_choice = patch("app.experiment.secrets.choice", side_effect=lambda slots: slots[0])
        self.opening_choice.start()
        self.addCleanup(self.opening_choice.stop)
        self.experiment = Experiment("unused", ExperimentConfig(("human", "ai", "ai"), 60))
        self.session = {
            "id": 1, "phase": "discussion", "round_number": 2, "discussion_seconds": 60,
            "deadline": datetime.now(UTC) + timedelta(seconds=45), "reconnect_deadline": None,
        }

    def test_autonomous_focus_does_not_answer_another_players_directed_turn(self):
        for sender in ("A", "B"):
            transcript = [
                {"id": 1, "sender_slot": "A", "text": "What do you look for in a decision maker?"},
                {"id": 2, "sender_slot": sender, "text": "Hey player C, are you honest?? and what factors you consider for a decision making?"},
            ]
            self.assertIsNone(_autonomous_focus(transcript, "B" if sender == "A" else "A"))
            self.assertIs(_autonomous_focus(transcript, "C"), transcript[-1])

    async def test_initial_general_human_message_uses_the_randomized_first_responder(self):
        transcript = [{"id": 1, "round_number": 2, "sender_slot": "C", "text": "What would make this discussion useful?"}]
        with (
            patch("app.experiment.secrets.choice", return_value="A") as choice,
            patch.object(db, "transaction", transaction),
            patch.object(db, "messages_for_round", AsyncMock(return_value=[])),
            patch.object(self.experiment, "_discussion_transcript", AsyncMock(side_effect=[transcript, None])),
            patch.object(self.experiment, "_submit", AsyncMock(return_value=None)),
            patch.object(self.experiment._llm, "discuss", AsyncMock(return_value="I would hear each person's reasons.")) as discuss,
        ):
            await self.experiment._run_discussion(self.session, ["A", "B"])

        choice.assert_called_once_with(["A", "B"])
        self.assertEqual(discuss.await_args.args[0], "A")
        self.assertEqual(discuss.await_args.kwargs["focus"], transcript[0])

    async def test_general_followup_reaches_the_latest_optional_ai_point(self):
        transcript = [
            {"id": 1, "round_number": 2, "sender_slot": "A", "text": "I would compare the reasons."},
            {"id": 2, "round_number": 2, "sender_slot": "B", "text": "I would consider the impact on everyone."},
            {"id": 3, "round_number": 2, "sender_slot": "C", "text": "Player A, what would you do when evidence is missing?"},
        ]
        calls = []
        injected = False

        async def snapshot(*_):
            nonlocal injected
            if len(calls) >= 2:
                return None
            if calls and not injected:
                injected = True
                transcript.extend([
                    {"id": 5, "round_number": 2, "sender_slot": "B", "text": "I would also look at who bears the risk."},
                    {"id": 6, "round_number": 2, "sender_slot": "C", "text": "on what basis?"},
                ])
            return list(transcript)

        async def discuss(slot, _round, _transcript, *, focus, **_options):
            calls.append((slot, focus["id"]))
            return "I would identify which facts would change my mind."

        async def submit(_session, slot, action, **_options):
            transcript.append({"id": len(transcript) + 1, "round_number": 2, "sender_slot": slot, "text": action.text})

        with (
            patch.object(db, "transaction", transaction),
            patch.object(db, "messages_for_round", AsyncMock(return_value=[])),
            patch.object(self.experiment, "_discussion_transcript", snapshot),
            patch.object(self.experiment, "_submit", submit),
            patch.object(self.experiment._llm, "discuss", discuss),
        ):
            await asyncio.wait_for(self.experiment._run_discussion(self.session, ["A", "B"]), timeout=1)

        self.assertEqual(calls, [("A", 3), ("B", 6)])

    async def test_failed_named_human_question_does_not_leak_into_other_players_join_or_idle(self):
        now = datetime.now(UTC)
        self.session.update(deadline=now + timedelta(seconds=60))
        transcript = [{
            "id": 1, "round_number": 2, "sender_slot": "A",
            "text": "Hey player C, are you honest?? and what factors you consider for a decision making?",
        }]

        async def wait_for_change(_event, wake_at):
            nonlocal now
            now = wake_at

        with (
            patch("app.experiment.datetime") as clock,
            patch("app.experiment._wait_for_change", wait_for_change),
            patch.object(db, "transaction", transaction),
            patch.object(db, "messages_for_round", AsyncMock(return_value=[])),
            patch.object(self.experiment, "_discussion_transcript", AsyncMock(return_value=transcript)),
            patch.object(self.experiment, "_submit", AsyncMock(return_value=None)) as submit,
            patch.object(self.experiment._llm, "discuss", AsyncMock(return_value=None)) as discuss,
        ):
            clock.now.side_effect = lambda _: now
            await asyncio.wait_for(self.experiment._run_discussion(self.session, ["B", "C"]), timeout=1)

        self.assertEqual([call.args[0] for call in discuss.await_args_list], ["C"])
        self.assertEqual(discuss.await_args.kwargs["turn_reason"], "direct")
        submit.assert_not_awaited()

    async def test_round_two_gives_humans_time_and_only_offers_optional_randomized_openings(self):
        now = datetime.now(UTC)
        started = now
        self.session.update(deadline=now + timedelta(seconds=60))
        turns = []

        async def wait_for_change(_event, wake_at):
            nonlocal now
            now = wake_at

        async def discuss(slot, _round, _transcript, **options):
            turns.append((slot, options["required"], options["turn_reason"], now - started))
            return None

        with (
            patch("app.experiment.datetime") as clock,
            patch("app.experiment._wait_for_change", wait_for_change),
            patch("app.experiment.secrets.choice", return_value="B"),
            patch.object(db, "transaction", transaction),
            patch.object(db, "messages_for_round", AsyncMock(return_value=[])),
            patch.object(self.experiment, "_discussion_transcript", AsyncMock(return_value=[])),
            patch.object(self.experiment._llm, "discuss", discuss),
        ):
            clock.now.side_effect = lambda _: now
            await asyncio.wait_for(self.experiment._run_discussion(self.session, ["A", "B"]), timeout=1)

        self.assertEqual(turns, [
            ("B", False, "opening", timedelta(seconds=9)),
            ("A", False, "opening", timedelta(seconds=11)),
        ])

    async def test_human_input_reopens_one_ai_followup_but_ai_messages_do_not_extend_the_chain(self):
        now = datetime.now(UTC)
        self.session.update(round_number=1, deadline=now + timedelta(seconds=60))
        transcript = [
            {"id": 1, "round_number": 1, "sender_slot": "A", "text": "We should hear both sides."},
            {"id": 2, "round_number": 1, "sender_slot": "C", "text": "I would ask for reasons."},
        ]
        turns, published = [], []
        injected = False

        async def snapshot(*_):
            nonlocal injected
            if len(published) == 1 and not injected:
                injected = True
                transcript.append({"id": 4, "round_number": 1, "sender_slot": "B", "text": "What does honesty look like?"})
            return list(transcript)

        async def wait_for_change(_event, wake_at):
            nonlocal now
            now = wake_at

        async def discuss(slot, _round, _transcript, *, focus, required, turn_reason, **_options):
            turns.append((slot, focus["id"], required, turn_reason))
            return f"A distinct point from turn {len(turns)}."

        async def submit(_session, slot, action, **_options):
            published.append(action.text)
            transcript.append({"id": len(transcript) + 1, "round_number": 1, "sender_slot": slot, "text": action.text})

        with (
            patch("app.experiment.datetime") as clock,
            patch("app.experiment._wait_for_change", wait_for_change),
            patch.object(self.experiment, "_discussion_transcript", snapshot),
            patch.object(self.experiment, "_submit", submit),
            patch.object(self.experiment._llm, "discuss", discuss),
        ):
            clock.now.side_effect = lambda _: now
            await asyncio.wait_for(self.experiment._run_discussion(self.session, ["A", "C"]), timeout=1)

        self.assertEqual(turns, [("A", 2, False, "idle"), ("A", 4, True, "conversation"), ("C", 5, False, "idle")])

    async def test_latest_nomination_is_next_without_losing_an_older_direct_question(self):
        texts = [
            ("A", "Transparency matters."),
            ("B", "We should listen to everyone."),
            ("C", "Player A, what makes a fair decision?"),
            ("C", "i want to become the decision maker please vot me"),
        ]
        transcript = [
            {"id": index, "round_number": 2, "sender_slot": slot, "text": text}
            for index, (slot, text) in enumerate(texts, 1)
        ]
        earlier = [
            {"id": 10, "sender_slot": "A", "text": "First-round AI text."},
            {"id": 11, "sender_slot": "C", "text": "First-round human text."},
        ]
        with (
            patch.object(db, "transaction", transaction),
            patch.object(db, "messages_for_round", AsyncMock(return_value=earlier)),
            patch.object(self.experiment, "_discussion_transcript", AsyncMock(side_effect=[transcript, transcript, None])),
            patch.object(self.experiment, "_submit", AsyncMock(return_value=None)) as submit,
            patch.object(self.experiment._llm, "discuss", AsyncMock(side_effect=[
                "What would you do if we disagree?", "A fair decision weighs everyone's reasons.",
            ])) as discuss,
        ):
            await self.experiment._run_discussion(self.session, ["A", "B"])
        self.assertEqual([call.args[0] for call in discuss.await_args_list], ["B", "A"])
        self.assertEqual([call.kwargs["focus"]["id"] for call in discuss.await_args_list], [4, 3])
        self.assertTrue(all(call.kwargs["required"] for call in discuss.await_args_list))
        self.assertEqual(discuss.await_args.kwargs["prior_round_replies"], ["First-round AI text."])
        self.assertEqual(submit.await_count, 2)

    async def test_pending_distinct_human_questions_are_bounded(self):
        transcript = [
            {"id": 1, "round_number": 2, "sender_slot": "A", "text": "Listening matters."},
            {"id": 2, "round_number": 2, "sender_slot": "B", "text": "Compare reasons."},
            *[
                {"id": message_id, "round_number": 2, "sender_slot": "C", "text": f"How would you decide case {message_id}?"}
                for message_id in range(3, 13)
            ],
        ]
        calls = []

        async def snapshot(*_):
            return transcript if len(calls) < 6 else None

        async def discuss(_slot, _round, _transcript, *, focus, **_options):
            calls.append(focus["id"])
            return f"I would compare the reasons in case {focus['id']}."

        with (
            patch.object(db, "transaction", transaction),
            patch.object(db, "messages_for_round", AsyncMock(return_value=[])),
            patch.object(self.experiment, "_discussion_transcript", snapshot),
            patch.object(self.experiment, "_submit", AsyncMock(return_value=None)),
            patch.object(self.experiment._llm, "discuss", discuss),
            self.assertLogs("uvicorn.error.discussion", "INFO") as diagnostics,
        ):
            await asyncio.wait_for(self.experiment._run_discussion(self.session, ["A", "B"]), timeout=1)

        self.assertEqual(calls, [12, 11, 10, 9, 8, 7])
        overflow = [line for line in diagnostics.output if "reason=human_reply_queue_full" in line]
        self.assertEqual(len(overflow), 4)

    async def test_repeated_question_and_ping_share_one_queued_opportunity(self):
        transcript = [
            {"id": 1, "round_number": 2, "sender_slot": "A", "text": "Listening matters."},
            {"id": 2, "round_number": 2, "sender_slot": "B", "text": "Compare reasons."},
            {"id": 3, "round_number": 2, "sender_slot": "C", "text": "Whom did you vote in round 1?"},
            {"id": 4, "round_number": 2, "sender_slot": "C", "text": "hey whom did you vote in round 1??"},
            {"id": 5, "round_number": 2, "sender_slot": "C", "text": "hey?"},
        ]
        with (
            patch.object(db, "transaction", transaction),
            patch.object(db, "messages_for_round", AsyncMock(return_value=[])),
            patch.object(self.experiment, "_discussion_transcript", AsyncMock(side_effect=[transcript, None])),
            patch.object(self.experiment, "_submit", AsyncMock(return_value=None)),
            patch.object(self.experiment._llm, "discuss", AsyncMock(return_value="My ballot is private; I weighed everyone's reasoning.")) as discuss,
        ):
            await self.experiment._run_discussion(self.session, ["A", "B"])
        discuss.assert_awaited_once()
        self.assertEqual(discuss.await_args.kwargs["focus"]["id"], 4)

    async def test_unaddressed_question_gets_one_alternate_after_first_reply_is_withheld(self):
        for text, rejected in (
            ("i want to become the decision maker please vot me", None),
            ("Both of you, what does honesty mean?", None),
            ("What would earn your support?", "duplicate"),
            ("What would earn your support?", "ballot_disclosure"),
        ):
            with self.subTest(text=text, rejected=rejected):
                experiment = Experiment("unused", ExperimentConfig(("human", "ai", "ai"), 60))
                transcript = [
                    {"id": 1, "round_number": 2, "sender_slot": "A", "text": "Listening matters."},
                    {"id": 2, "round_number": 2, "sender_slot": "B", "text": "Compare reasons."},
                    {"id": 3, "round_number": 2, "sender_slot": "C", "text": text},
                ]
                with (
                    patch.object(db, "transaction", transaction),
                    patch.object(db, "messages_for_round", AsyncMock(return_value=[])),
                    patch.object(experiment, "_discussion_transcript", AsyncMock(side_effect=[transcript, transcript, None])),
                    patch.object(experiment, "_submit", AsyncMock(side_effect=[rejected, None] if rejected else [None])) as submit,
                    patch.object(experiment._llm, "discuss", AsyncMock(side_effect=[
                        "First model output rejected before publication." if rejected else None,
                        "How would you handle evidence that challenges your preferred option?",
                    ])) as discuss,
                    self.assertLogs("uvicorn.error.discussion", "INFO") as diagnostics,
                ):
                    await experiment._run_discussion(self.session, ["A", "B"])

                self.assertEqual([call.args[0] for call in discuss.await_args_list], ["B", "A"])
                self.assertEqual([call.kwargs["focus"]["id"] for call in discuss.await_args_list], [3, 3])
                self.assertTrue(all(call.kwargs["required"] for call in discuss.await_args_list))
                self.assertEqual(submit.await_count, 2 if rejected else 1)
                self.assertEqual(submit.await_args.args[1], "A")
                self.assertEqual(sum("trigger=alternate_responder" in line for line in diagnostics.output), 1)

    async def test_both_responses_withheld_stop_after_one_opportunity_per_player(self):
        now = datetime.now(UTC)
        self.session.update(round_number=1, deadline=now + timedelta(seconds=6))
        transcript = [
            {"id": 1, "round_number": 1, "sender_slot": "A", "text": "Listening matters."},
            {"id": 2, "round_number": 1, "sender_slot": "B", "text": "Compare reasons."},
            {"id": 3, "round_number": 1, "sender_slot": "C", "text": "What makes a fair decision?"},
        ]

        async def wait_for_change(_event, wake_at):
            nonlocal now
            now = wake_at

        with (
            patch("app.experiment.datetime") as clock,
            patch("app.experiment._wait_for_change", wait_for_change),
            patch.object(self.experiment, "_discussion_transcript", AsyncMock(return_value=transcript)),
            patch.object(self.experiment, "_submit", AsyncMock(return_value=None)) as submit,
            patch.object(self.experiment._llm, "discuss", AsyncMock(return_value=None)) as discuss,
        ):
            clock.now.side_effect = lambda _: now
            await asyncio.wait_for(self.experiment._run_discussion(self.session, ["A", "B"]), timeout=1)

        self.assertEqual([call.args[0] for call in discuss.await_args_list], ["B", "A"])
        submit.assert_not_awaited()

    async def test_failed_direct_or_single_ai_question_is_not_rerouted(self):
        for slots, text in ((["A", "B"], "Player B, what makes a fair decision?"), (["A"], "What makes a fair decision?")):
            with self.subTest(slots=slots, text=text):
                now = datetime.now(UTC)
                session = {**self.session, "round_number": 1, "deadline": now + timedelta(seconds=6)}
                experiment = Experiment("unused", ExperimentConfig(("human", "ai", "ai"), 60))
                transcript = [
                    {"id": index, "round_number": 1, "sender_slot": slot, "text": "Listening matters."}
                    for index, slot in enumerate(slots, 1)
                ]
                transcript.append({"id": len(slots) + 1, "round_number": 1, "sender_slot": "C", "text": text})

                async def wait_for_change(_event, wake_at):
                    nonlocal now
                    now = wake_at

                with (
                    patch("app.experiment.datetime") as clock,
                    patch("app.experiment._wait_for_change", wait_for_change),
                    patch.object(experiment, "_discussion_transcript", AsyncMock(return_value=transcript)),
                    patch.object(experiment, "_submit", AsyncMock(return_value=None)) as submit,
                    patch.object(experiment._llm, "discuss", AsyncMock(return_value=None)) as discuss,
                ):
                    clock.now.side_effect = lambda _: now
                    await asyncio.wait_for(experiment._run_discussion(session, slots), timeout=1)

                discuss.assert_awaited_once()
                self.assertEqual(discuss.await_args.args[0], slots[-1])
                submit.assert_not_awaited()

    async def test_newer_human_question_precedes_queued_alternate(self):
        transcript = [
            {"id": 1, "round_number": 2, "sender_slot": "A", "text": "Listening matters."},
            {"id": 2, "round_number": 2, "sender_slot": "B", "text": "Compare reasons."},
            {"id": 3, "round_number": 2, "sender_slot": "C", "text": "What makes a fair decision?"},
        ]
        calls = []

        async def snapshot(*_):
            return list(transcript) if len(calls) < 3 else None

        async def discuss(slot, _round, _transcript, *, focus, **_options):
            calls.append((slot, focus["id"]))
            if len(calls) == 1:
                transcript.append({"id": 4, "round_number": 2, "sender_slot": "C", "text": "And what does honesty mean?"})
                return None
            return "Admit when the evidence weakens your preferred option."

        with (
            patch.object(db, "transaction", transaction),
            patch.object(db, "messages_for_round", AsyncMock(return_value=[])),
            patch.object(self.experiment, "_discussion_transcript", snapshot),
            patch.object(self.experiment, "_submit", AsyncMock(return_value=None)),
            patch.object(self.experiment._llm, "discuss", discuss),
        ):
            await asyncio.wait_for(self.experiment._run_discussion(self.session, ["A", "B"]), timeout=1)
        self.assertEqual(calls, [("B", 3), ("B", 4), ("A", 3)])

    async def test_same_player_ping_does_not_replace_a_queued_nomination(self):
        transcript = [
            {"id": 1, "round_number": 2, "sender_slot": "A", "text": "Listen before deciding."},
            {"id": 2, "round_number": 2, "sender_slot": "B", "text": "Consider different options."},
            {"id": 3, "round_number": 2, "sender_slot": "C", "text": "vote me"},
            {"id": 4, "round_number": 2, "sender_slot": "C", "text": "hey?"},
        ]
        with (
            patch.object(db, "transaction", transaction),
            patch.object(db, "messages_for_round", AsyncMock(return_value=[])),
            patch.object(self.experiment, "_discussion_transcript", AsyncMock(side_effect=[transcript, None])),
            patch.object(self.experiment, "_submit", AsyncMock(return_value=None)),
            patch.object(self.experiment._llm, "discuss", AsyncMock(return_value="How would you decide?")) as discuss,
        ):
            await self.experiment._run_discussion(self.session, ["A", "B"])
        discuss.assert_awaited_once()
        self.assertEqual(discuss.await_args.kwargs["focus"], transcript[2])

    async def test_same_player_ping_does_not_cancel_a_reply_already_generating(self):
        now = datetime.now(UTC)
        self.session.update(round_number=1, deadline=now + timedelta(seconds=6))
        transcript = [
            {"id": 1, "sender_slot": "A", "text": "Listen before deciding."},
            {"id": 2, "sender_slot": "B", "text": "Consider different options."},
            {"id": 3, "sender_slot": "C", "text": "vote me"},
        ]

        async def record_message(_connection, _session_id, _round_number, slot, text):
            transcript.append({"id": len(transcript) + 1, "sender_slot": slot, "text": text})

        async def wait_for_change(_event, wake_at):
            nonlocal now
            now = wake_at

        async def discuss(_slot, _round_number, _transcript, *, focus, **_options):
            self.assertEqual(focus["text"], "vote me")
            error = await self.experiment._submit(1, "C", ChatAction(type="message", round=1, text="hey?"))
            self.assertIsNone(error)
            # Let the event watcher run before inference finishes.
            await asyncio.sleep(0)
            return "What would you do if the group disagreed?"

        with (
            patch("app.experiment.datetime") as clock,
            patch("app.experiment._wait_for_change", wait_for_change),
            patch.object(db, "transaction", transaction),
            patch.object(db, "session_for_update", AsyncMock(return_value=self.session)),
            patch.object(db, "messages_for_round", AsyncMock(side_effect=lambda *_: list(transcript))),
            patch.object(db, "record_message", record_message),
            patch.object(self.experiment._llm, "discuss", AsyncMock(side_effect=discuss)) as model,
        ):
            clock.now.side_effect = lambda _: now
            await asyncio.wait_for(self.experiment._run_discussion(self.session, ["A", "B"]), timeout=1)

        self.assertEqual([(row["sender_slot"], row["text"]) for row in transcript[-2:]], [
            ("C", "hey?"),
            ("B", "What would you do if the group disagreed?"),
        ])
        model.assert_awaited_once()
        self.assertIsNone(self.experiment._answering_discussion)

    async def test_in_flight_human_answer_finishes_and_distinct_follow_up_is_next(self):
        now = datetime.now(UTC)
        self.session.update(round_number=1, deadline=now + timedelta(seconds=6))
        transcript = [
            {"id": 1, "round_number": 1, "sender_slot": "A", "text": "Listen before deciding."},
            {"id": 2, "round_number": 1, "sender_slot": "B", "text": "Consider different options."},
            {"id": 3, "round_number": 1, "sender_slot": "C", "text": "Whom did you vote in round 1?"},
        ]

        async def record_message(_connection, _session_id, round_number, slot, text):
            transcript.append({
                "id": len(transcript) + 1, "round_number": round_number, "sender_slot": slot, "text": text,
            })

        async def wait_for_change(_event, wake_at):
            nonlocal now
            now = wake_at

        async def discuss(_slot, _round, _transcript, *, focus, **_options):
            if focus["id"] == 3:
                for text in (
                    "hey whom did you vote in round 1??",
                    "What does honesty look like in a difficult decision?",
                    "hey?",
                ):
                    error = await self.experiment._submit(1, "C", ChatAction(type="message", round=1, text=text))
                    self.assertIsNone(error)
                    self.assertFalse(self.experiment._discussion_changed.is_set())
                await asyncio.sleep(0)
                return "My ballot is private, but I considered how each player explained their choices."
            self.assertEqual(focus["id"], 5)
            return "Honesty means admitting uncertainty even when that weakens your preferred option."

        with (
            patch("app.experiment.datetime") as clock,
            patch("app.experiment._wait_for_change", wait_for_change),
            patch.object(db, "transaction", transaction),
            patch.object(db, "session_for_update", AsyncMock(return_value=self.session)),
            patch.object(db, "messages_for_round", AsyncMock(side_effect=lambda *_: list(transcript))),
            patch.object(db, "record_message", record_message),
            patch.object(self.experiment._llm, "discuss", AsyncMock(side_effect=discuss)) as model,
        ):
            clock.now.side_effect = lambda _: now
            await asyncio.wait_for(self.experiment._run_discussion(self.session, ["A", "B"]), timeout=1)

        self.assertEqual([call.kwargs["focus"]["id"] for call in model.await_args_list], [3, 5])
        self.assertEqual([row["sender_slot"] for row in transcript[-2:]], ["B", "B"])
        self.assertIn("Honesty means admitting uncertainty", transcript[-1]["text"])
        self.assertIsNone(self.experiment._answering_discussion)

    async def test_answer_finishing_after_discussion_deadline_is_not_published(self):
        now = datetime.now(UTC)
        self.session.update(round_number=1, deadline=now + timedelta(seconds=6))
        transcript = [
            {"id": 1, "round_number": 1, "sender_slot": "A", "text": "Listen before deciding."},
            {"id": 2, "round_number": 1, "sender_slot": "B", "text": "Consider different options."},
            {"id": 3, "round_number": 1, "sender_slot": "C", "text": "What does honesty mean?"},
        ]

        async def discuss(*_args, **_options):
            nonlocal now
            now = self.session["deadline"] + timedelta(milliseconds=1)
            return "Honesty includes admitting uncertainty."

        with (
            patch("app.experiment.datetime") as clock,
            patch.object(db, "transaction", transaction),
            patch.object(db, "session_for_update", AsyncMock(return_value=self.session)),
            patch.object(db, "messages_for_round", AsyncMock(return_value=transcript)),
            patch.object(db, "record_message", AsyncMock()) as record,
            patch.object(self.experiment._llm, "discuss", discuss),
            self.assertLogs("uvicorn.error.discussion", "INFO") as diagnostics,
        ):
            clock.now.side_effect = lambda _: now
            await self.experiment._run_discussion(self.session, ["A", "B"])

        record.assert_not_awaited()
        self.assertTrue(any("status=discarded" in line and "reason=round_closed" in line for line in diagnostics.output))

    async def test_new_human_message_interrupts_an_optional_ai_reply(self):
        self.session["discussion_seconds"] = 1
        transcript = [
            {"id": 1, "round_number": 2, "sender_slot": "A", "text": "What qualities matter?"},
            {"id": 2, "round_number": 2, "sender_slot": "B", "text": "Listening matters to me."},
            {"id": 3, "round_number": 2, "sender_slot": "C", "text": "Both of you, what would earn your vote?"},
        ]
        posted = []
        optional_cancelled = asyncio.Event()

        async def snapshot(*_):
            self.experiment._discussion_changed.clear()
            return list(transcript) if len(posted) < 2 else None

        async def discuss(slot, _round, _transcript, *, focus, required, **_options):
            if focus["id"] == 3 and required:
                return "How would you handle a difficult decision?"
            if focus["id"] == 3:
                transcript.append({
                    "id": 5, "round_number": 2, "sender_slot": "C", "text": "i think hjonesty matters",
                })
                self.experiment._discussion_changed.set()
                try:
                    await asyncio.sleep(10)
                finally:
                    optional_cancelled.set()
            self.assertEqual(focus["id"], 5)
            self.assertTrue(required)
            return "Honesty could mean saying when evidence contradicts your preference."

        async def submit(_session, slot, action, **_options):
            posted.append(action.text)
            transcript.append({
                "id": len(transcript) + 1, "round_number": 2, "sender_slot": slot, "text": action.text,
            })

        with (
            patch.object(db, "transaction", transaction),
            patch.object(db, "messages_for_round", AsyncMock(return_value=[])),
            patch.object(self.experiment, "_discussion_transcript", snapshot),
            patch.object(self.experiment, "_submit", submit),
            patch.object(self.experiment._llm, "discuss", discuss),
            self.assertLogs("uvicorn.error.discussion", "INFO") as diagnostics,
        ):
            await asyncio.wait_for(self.experiment._run_discussion(self.session, ["A", "B"]), timeout=2)

        self.assertTrue(optional_cancelled.is_set())
        self.assertEqual(posted, [
            "How would you handle a difficult decision?",
            "Honesty could mean saying when evidence contradicts your preference.",
        ])
        self.assertTrue(any(
            "trigger_message_id=3" in line and "reason=superseded_by_human_message" in line
            for line in diagnostics.output
        ))

    async def test_follow_up_keeps_the_previous_responder_and_gets_the_remaining_time(self):
        self.session["deadline"] = datetime.now(UTC) + timedelta(seconds=6)
        first = {"id": 1, "round_number": 2, "sender_slot": "C", "text": "Player A, what would earn your vote?"}
        reply = {"id": 2, "round_number": 2, "sender_slot": "A", "text": "I'd listen to both sides."}
        follow_up = {"id": 3, "round_number": 2, "sender_slot": "C", "text": "vote meeeee"}
        with (
            patch.object(db, "transaction", transaction),
            patch.object(db, "messages_for_round", AsyncMock(return_value=[])),
            patch.object(self.experiment, "_discussion_transcript", AsyncMock(
                side_effect=[[first], [first, reply, follow_up], [first, reply, follow_up], None],
            )),
            patch.object(self.experiment, "_submit", AsyncMock(return_value=None)),
            patch("app.experiment._wait_for_change", AsyncMock()),
            patch.object(self.experiment._llm, "discuss", AsyncMock(return_value="Tell me how you'd decide.")) as discuss,
        ):
            await self.experiment._run_discussion(self.session, ["A", "B"])

        self.assertEqual([call.args[0] for call in discuss.await_args_list], ["A", "A"])
        self.assertEqual(discuss.await_args.kwargs["focus"], follow_up)
        self.assertGreater(discuss.await_args.kwargs["timeout_seconds"], 5)
        self.assertFalse(discuss.await_args.kwargs["wrap_up"])

    async def test_an_early_greeting_leaves_a_quiet_turn_for_the_other_player(self):
        now = datetime.now(UTC)
        self.session.update(round_number=1, deadline=now + timedelta(seconds=60))
        transcript = [{"id": 1, "round_number": 1, "sender_slot": "B", "text": "hi everyone"}]
        turns = []

        async def wait_for_change(_event, wake_at):
            nonlocal now
            now = wake_at

        async def discuss(slot, _round, _transcript, *, focus, required, idle=False, **_options):
            turns.append((slot, required, idle, focus["text"] if focus else None, now))
            if not idle:
                return "Hi Player B!"
            if focus is None:
                return "I'd look for someone who listens before deciding."
            return None

        async def submit(_session, slot, action, **_options):
            transcript.append({
                "id": len(transcript) + 1, "round_number": 1, "sender_slot": slot, "text": action.text,
            })

        with (
            patch("app.experiment.datetime") as clock,
            patch("app.experiment._wait_for_change", wait_for_change),
            patch.object(self.experiment, "_discussion_transcript", AsyncMock(side_effect=lambda *_: list(transcript))),
            patch.object(self.experiment, "_submit", submit),
            patch.object(self.experiment._llm, "discuss", discuss),
        ):
            clock.now.side_effect = lambda _: now
            await self.experiment._run_discussion(self.session, ["A", "C"])

        self.assertEqual([turn[:4] for turn in turns], [
            ("A", True, False, "hi everyone"),
            ("C", False, True, None),
            ("A", False, True, "I'd look for someone who listens before deciding."),
        ])
        self.assertEqual(turns[1][4] - turns[0][4], timedelta(seconds=4))
        self.assertEqual(turns[2][4] - turns[1][4], timedelta(seconds=9))

    async def test_unspoken_player_has_enough_time_for_local_inference_during_a_quiet_join(self):
        now = datetime.now(UTC)
        self.session.update(deadline=now + timedelta(seconds=40))
        transcript = [
            {"id": 1, "round_number": 2, "sender_slot": "B", "text": "I would admit uncertainty rather than pretend."},
            {"id": 2, "round_number": 2, "sender_slot": "A", "text": "That would help us weigh the risks honestly."},
        ]
        calls = []

        async def wait_for_change(_event, wake_at):
            nonlocal now
            now = wake_at

        async def discuss(slot, _round, _transcript, **options):
            calls.append((slot, options))
            return None

        with (
            patch("app.experiment.datetime") as clock,
            patch("app.experiment._wait_for_change", wait_for_change),
            patch.object(db, "transaction", transaction),
            patch.object(db, "messages_for_round", AsyncMock(return_value=[])),
            patch.object(self.experiment, "_discussion_transcript", AsyncMock(side_effect=[transcript, transcript, None])),
            patch.object(self.experiment._llm, "discuss", discuss),
        ):
            clock.now.side_effect = lambda _: now
            await asyncio.wait_for(self.experiment._run_discussion(self.session, ["A", "C"]), timeout=1)

        self.assertEqual(len(calls), 1)
        slot, options = calls[0]
        self.assertEqual((slot, options["turn_reason"], options["required"]), ("C", "idle_join", False))
        self.assertGreater(options["timeout_seconds"], 8)
        self.assertLessEqual(options["timeout_seconds"], 15)
        self.assertLess(options["timeout_seconds"], options["seconds_remaining"])

    async def test_human_input_interrupts_idle_chatter_and_gets_an_immediate_reply(self):
        now = datetime.now(UTC)
        self.session.update(round_number=1, deadline=now + timedelta(seconds=60))
        transcript = [
            {"id": 1, "round_number": 1, "sender_slot": "A", "text": "How should we decide?"},
            {"id": 2, "round_number": 1, "sender_slot": "C", "text": "Listening is important."},
        ]
        turns = []
        idle_budgets = []
        interrupted = asyncio.Event()

        async def wait_for_change(_event, wake_at):
            nonlocal now
            now = wake_at

        async def snapshot(*_):
            self.experiment._discussion_changed.clear()
            return list(transcript)

        async def discuss(slot, _round, _transcript, *, focus, required, idle=False, **_options):
            turns.append((slot, required, idle, focus["text"], now))
            if idle:
                idle_budgets.append(_options["timeout_seconds"])
                transcript.append({
                    "id": 3, "round_number": 1, "sender_slot": "B", "text": "Player C, what does that mean?",
                })
                self.experiment._discussion_changed.set()
                try:
                    await asyncio.sleep(30)
                finally:
                    interrupted.set()
            return "Let each person finish before asking a follow-up."

        with (
            patch("app.experiment.datetime") as clock,
            patch("app.experiment._wait_for_change", wait_for_change),
            patch.object(self.experiment, "_discussion_transcript", snapshot),
            patch.object(self.experiment, "_submit", AsyncMock(return_value=None)) as submit,
            patch.object(self.experiment._llm, "discuss", discuss),
        ):
            clock.now.side_effect = lambda _: now
            await asyncio.wait_for(self.experiment._run_discussion(self.session, ["A", "C"]), timeout=1)

        self.assertTrue(interrupted.is_set())
        self.assertGreater(idle_budgets[0], 8)
        self.assertEqual([turn[:4] for turn in turns], [
            ("A", False, True, "Listening is important."),
            ("C", True, False, "Player C, what does that mean?"),
        ])
        self.assertEqual(turns[0][4], turns[1][4])
        submit.assert_awaited_once_with(
            1, "C", ChatAction(type="message", round=1, text="Let each person finish before asking a follow-up."), ai=True,
        )

    async def test_ai_cannot_save_a_duplicate_from_another_round(self):
        response = ChatAction(
            type="message", round=2, text='"Player C, what would you do if we disagree?"',
        )
        with (
            patch.object(db, "transaction", transaction),
            patch.object(db, "session_for_update", AsyncMock(return_value=self.session)),
            patch.object(db, "ai_slots", AsyncMock(return_value=["A", "B"])),
            patch.object(db, "messages_for_round", AsyncMock(side_effect=[[], [{
                "id": 6, "sender_slot": "B", "text": "Player A, what would you do if we disagree?"
            }]])),
            patch.object(db, "record_message", AsyncMock()) as record,
        ):
            error = await self.experiment._submit(1, "B", response, ai=True)
        self.assertEqual(error, "duplicate")
        record.assert_not_awaited()

    async def test_ai_hello_can_echo_a_human_but_posts_only_once_per_round(self):
        response = ChatAction(type="message", round=2, text="Hello!")
        rows = [{"id": 6, "sender_slot": "C", "text": "Hello?"}]
        with (
            patch.object(db, "transaction", transaction),
            patch.object(db, "session_for_update", AsyncMock(return_value=self.session)),
            patch.object(db, "ai_slots", AsyncMock(return_value=["A", "B"])),
            patch.object(db, "messages_for_round", AsyncMock(return_value=rows)) as messages,
            patch.object(self.experiment, "_apply", AsyncMock(return_value=(self.session, None))) as apply,
            patch.object(self.experiment, "_view", AsyncMock(return_value=({}, set(), set()))),
        ):
            self.assertIsNone(await self.experiment._submit(1, "B", response, ai=True))
            messages.assert_awaited_once_with(None, 1, 2)
            rows.append({"id": 7, "sender_slot": "B", "text": "Hello!"})
            self.assertEqual(await self.experiment._submit(1, "A", response, ai=True), "duplicate")
        apply.assert_awaited_once()
