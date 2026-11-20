import asyncio
import itertools
import json
import os
import re
import time
import unittest
from unittest.mock import patch

import httpx

from app.llm_player import LLMPlayer


MODEL = "gpt-5.4-mini-2026-03-17"
OLLAMA_URL = "http://host.docker.internal:11434"
OLLAMA_MODEL = "llama3:latest"


def completion(text, *, finish_reason="stop"):
    return httpx.Response(200, json={
        "choices": [{"finish_reason": finish_reason, "message": {"content": text}}],
    })


def ollama_chat(text, *, model=OLLAMA_MODEL, done_reason="stop"):
    return httpx.Response(200, json={
        "model": model, "done": True, "done_reason": done_reason,
        "message": {"role": "assistant", "content": text},
    })


def mocked_provider(handler):
    async_client = httpx.AsyncClient
    return patch("app.llm_player.httpx.AsyncClient", side_effect=lambda **kwargs: async_client(
        transport=httpx.MockTransport(handler), **{**kwargs, "trust_env": False},
    ))


def row(slot, text, *, round_number=1, message_id=None, **extras):
    message = {"round_number": round_number, "sender_slot": slot, "text": text, **extras}
    if message_id is not None:
        message["id"] = message_id
    return message


def metadata(payload):
    header = payload["messages"][0]["content"].split("\n", 1)[0]
    match = re.fullmatch(
        r"You are Player ([ABC])\. Round: (\d+)\. Seconds remaining: (unknown|\d+(?:\.\d+)?)\.",
        header,
    )
    assert match is not None, header
    return match[1], int(match[2]), None if match[3] == "unknown" else float(match[3])


def all_content(payload):
    return "\n".join(message["content"] for message in payload["messages"])


class LLMPlayerTests(unittest.IsolatedAsyncioTestCase):
    async def test_discussion_keeps_types_private_without_claiming_a_human_identity(self):
        question = row("A", "What kind of participant are you?")
        for invalid in ("I am an artificial intelligence.", "I'm a human player.", "As an LLM, I can help."):
            replies = iter([invalid, "I can explain how I'd approach the decision under my player label."])
            with (
                self.subTest(invalid=invalid),
                mocked_provider(lambda _: ollama_chat(next(replies))),
                self.assertLogs("uvicorn.error.llm_player", "INFO") as diagnostics,
            ):
                text = await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).discuss(
                    "C", 1, [question], focus=question, seconds_remaining=40,
                )
            self.assertEqual(text, "I can explain how I'd approach the decision under my player label.")
            self.assertIn("reason=role_disclosure retry=true", "\n".join(diagnostics.output))

    async def test_native_chat_uses_each_players_own_identity_and_only_current_public_dialogue(self):
        question = row("A", "How would you handle uncertainty?", round_number=2, message_id=8)
        transcript = [
            row("A", "OLD_ROUND_SECRET_MARKER", round_number=1),
            row("B", "I'd explain what I was unsure about.", round_number=2, kind="ai", token="TOKEN_MARKER"),
            question,
            row("C", "I'd ask what evidence would help.", round_number=2, ballot="BALLOT_MARKER"),
        ]
        captured = []

        def respond(request):
            payload = json.loads(request.content)
            captured.append(payload)
            return ollama_chat("I'd distinguish what we know from what we are assuming.")

        with mocked_provider(respond):
            player = LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL)
            for slot in ("B", "C"):
                self.assertIsNotNone(await player.discuss(
                    slot, 2, transcript, focus=question.copy(), seconds_remaining=41,
                    turn_reason="reply",
                ))
        self.assertEqual(len(captured), 2)
        for payload, slot in zip(captured, ("B", "C")):
            own_slot, round_number, remaining = metadata(payload)
            self.assertEqual((own_slot, round_number), (slot, 2))
            self.assertGreater(remaining, 40)
            self.assertLessEqual(remaining, 41)
            messages = payload["messages"]
            self.assertEqual(messages[-1], {"role": "user", "content": question["text"]})
            self.assertEqual(all_content(payload).count(question["text"]), 1)
            own_text = transcript[1 if slot == "B" else 3]["text"]
            self.assertIn({"role": "assistant", "content": own_text}, messages)
            other = "C" if slot == "B" else "B"
            other_text = transcript[3 if slot == "B" else 1]["text"]
            self.assertIn({"role": "user", "content": f"Player {other}: {other_text}"}, messages)
            for private in ("OLD_ROUND_SECRET_MARKER", "TOKEN_MARKER", "BALLOT_MARKER", '"kind"'):
                self.assertNotIn(private, all_content(payload))
            self.assertNotIn("format", payload)

    async def test_ordinary_questions_share_one_contract_instead_of_keyword_scripts(self):
        systems = []
        questions = ("hello", "please give me vote guys", "on what basis?", "What does honesty mean?")

        def respond(request):
            payload = json.loads(request.content)
            systems.append(payload["messages"][0]["content"].split("\n", 1)[1])
            self.assertEqual(payload["messages"][-1]["content"], questions[len(systems) - 1])
            return ollama_chat("I'd hear what you mean before drawing a conclusion.")

        with mocked_provider(respond):
            player = LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL)
            for text in questions:
                message = row("A", text)
                await player.discuss("C", 1, [message], focus=message, seconds_remaining=40)
        self.assertEqual(len(set(systems)), 1)
        self.assertNotIn("22 words", systems[0])

    async def test_study_facts_and_private_timing_guide_both_rounds_without_invented_dialogue(self):
        for model in (OLLAMA_MODEL, "qwen3:1.7b"):
            for round_number in (1, 2):
                question = row(
                    "A", "so on what basis would you all be choosing decisiona maker?",
                    round_number=round_number, message_id=8,
                )
                captured = []
                with (
                    self.subTest(model=model, round_number=round_number),
                    mocked_provider(lambda request: (
                        captured.append(json.loads(request.content))
                        or ollama_chat("I'd compare the reasons each person has actually given.", model=model)
                    )),
                ):
                    answer = await LLMPlayer("", model, ollama_url=OLLAMA_URL).discuss(
                        "B", round_number, [question], focus=question, seconds_remaining=36,
                    )
                self.assertIsNotNone(answer)
                payload, = captured
                instructions = payload["messages"][0]["content"].lower()
                self.assertEqual(metadata(payload)[:2], ("B", round_number))
                self.assertGreater(metadata(payload)[2], 35)
                self.assertLessEqual(metadata(payload)[2], 36)
                self.assertIn("pacing", instructions)
                self.assertIn("countdown", instructions)
                self.assertRegex(instructions, r"(?:do not|never)[^.!?\n]*(?:quot|narrat|mention)[^.!?\n]*(?:tim|countdown)")
                self.assertRegex(instructions, r"(?:hypothetical|imaginary)")
                self.assertIn("this selection is real", instructions)
                self.assertIn("counting private ballots", instructions)
                self.assertIn("biographies and qualifications are not supplied", instructions)
                self.assertRegex(instructions, r"(?:no |not |without )[^.!?\n]*(?:game|gameplay)")
                self.assertIn("chat", instructions)
                self.assertEqual(payload["messages"][-1], {"role": "user", "content": question["text"]})
                self.assertEqual(all_content(payload).count(question["text"]), 1)

    async def test_unsolicited_countdown_readings_regenerate_without_replacing_the_question(self):
        regressions = (
            "I think it's crucial to consider not just past performance, but also how players adapt and adjust "
            "their strategies based on the game's progression. In this round, I'd like to focus on who can "
            "effectively navigate the current situation and make informed decisions with 36 seconds left.",
            "That's a great point! With only 21 seconds left in this round, it's crucial to consider who can "
            "think on their feet and make quick, strategic decisions. Maybe we should look for players who "
            "have shown an ability to pivot or adjust their approach mid-round?",
        )
        corrected = "I'd look at the reasons each of us has actually given and whether we respond openly when challenged."
        for model in (OLLAMA_MODEL, "qwen3:1.7b"):
            for round_number in (1, 2):
                focus = row("A", "On what basis should we choose?", round_number=round_number)
                for invalid in regressions:
                    responses = iter([invalid, corrected])
                    captured, outcomes = [], []
                    with (
                        self.subTest(model=model, round_number=round_number, invalid=invalid),
                        mocked_provider(lambda request: (
                            captured.append(json.loads(request.content))
                            or ollama_chat(next(responses), model=model)
                        )),
                        self.assertLogs("uvicorn.error.llm_player", "INFO") as diagnostics,
                    ):
                        answer = await LLMPlayer("", model, ollama_url=OLLAMA_URL).discuss(
                            "B", round_number, [focus], focus=focus, seconds_remaining=40, report=outcomes.append,
                        )
                    self.assertEqual(answer, corrected)
                    self.assertEqual(len(captured), 2)
                    self.assertEqual(captured[1]["messages"][-1]["content"], focus["text"])
                    self.assertIn("reason=countdown_narration retry=true", "\n".join(diagnostics.output))
                    self.assertEqual((outcomes[0].source, outcomes[0].model), ("model", model))

    async def test_invented_game_history_gets_repaired_without_hiding_human_dialogue(self):
        focus = row("C", "so on what basis would you all be choosing decisiona maker?", round_number=2)
        invalid = (
            "I think we should consider each other's moves and strategies from the previous rounds. "
            "Who made the most informed decisions, who took calculated risks, and who managed to stay "
            "flexible throughout the game?"
        )
        corrected = "I'd compare the reasons we have actually given here and how we respond when challenged."
        responses = iter((invalid, corrected))
        with mocked_provider(lambda _: ollama_chat(next(responses))):
            result = await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).discuss(
                "A", 2, [focus], focus=focus, seconds_remaining=40,
            )
        self.assertEqual(result, corrected)
        for truthful in (
            "No game has taken place; we can judge only this conversation.",
            "If we disagree, I would ask what evidence could change our minds.",
        ):
            with self.subTest(truthful=truthful), mocked_provider(lambda _: ollama_chat(truthful)):
                self.assertEqual(await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).discuss(
                    "A", 2, [focus], focus=focus,
                ), truthful)

    async def test_persistent_countdown_narration_stays_silent_instead_of_posting_a_script(self):
        for required in (True, False):
            for model in (OLLAMA_MODEL, "qwen3:1.7b"):
                focus = row("A", "How many seconds are left?", round_number=2)
                calls, outcomes = [], []
                with (
                    self.subTest(required=required, model=model),
                    mocked_provider(lambda request: (
                        calls.append(json.loads(request.content))
                        or ollama_chat("We have twenty-one seconds remaining.", model=model)
                    )),
                ):
                    answer = await LLMPlayer("", model, ollama_url=OLLAMA_URL).discuss(
                        "C", 2, [focus], focus=focus, required=required,
                        seconds_remaining=40, report=outcomes.append,
                    )
                self.assertIsNone(answer)
                self.assertEqual(len(calls), 2)
                self.assertEqual((outcomes[0].source, outcomes[0].reason), ("none", "countdown_narration"))

    async def test_proposed_time_budgets_and_directions_to_the_live_clock_are_valid_discussion(self):
        focus = row("A", "How can we give everyone a fair chance to explain?")
        for text in (
            "I would give each person 30 seconds to explain, then compare their reasons.",
            "Check the countdown on the page for the current time; I'd rather keep listening to your point.",
        ):
            requests = []
            with self.subTest(text=text), mocked_provider(lambda request: (
                requests.append(request) or ollama_chat(text)
            )):
                answer = await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).discuss(
                    "B", 1, [focus], focus=focus, seconds_remaining=40,
                )
            self.assertEqual(answer, text)
            self.assertEqual(len(requests), 1)

    async def test_direct_question_cannot_be_answered_by_the_other_player(self):
        question = row("A", "Hey player C, are you honest?? and what factors you consider for a decision making?")
        outcomes = []
        with patch("app.llm_player.httpx.AsyncClient") as client:
            result = await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).discuss(
                "B", 2, [{**question, "round_number": 2}],
                focus={**question, "round_number": 2}, seconds_remaining=40,
                human_reply=True, report=outcomes.append,
            )
        client.assert_not_called()
        self.assertIsNone(result)
        self.assertEqual(outcomes[0].reason, "addressed_to_other_player")

    async def test_named_recipient_gets_the_exact_question(self):
        question = row("A", "Player C, how would you handle a disagreement?")
        captured = []
        with mocked_provider(lambda request: (
            captured.append(json.loads(request.content))
            or ollama_chat("I'd ask each person to explain their concern before deciding.")
        )):
            answer = await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).discuss(
                "C", 1, [question], focus=question, seconds_remaining=45,
            )
        self.assertIsNotNone(answer)
        self.assertEqual(metadata(captured[0])[0], "C")
        self.assertEqual(captured[0]["messages"][-1]["content"], question["text"])

    async def test_group_question_allows_separate_calls_for_both_players(self):
        question = row("A", "Player B and Player C, how would you resolve a disagreement?")
        slots = []

        def respond(request):
            slots.append(metadata(json.loads(request.content))[0])
            return ollama_chat("I'd first ask which assumption we disagree about.")

        with mocked_provider(respond):
            player = LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL)
            for slot in ("B", "C"):
                self.assertIsNotNone(await player.discuss(slot, 1, [question], focus=question))
        self.assertEqual(slots, ["B", "C"])

    async def test_copied_or_forged_focus_never_injects_unrecorded_dialogue(self):
        real = row("A", "What would change your mind?", message_id=7)
        forged = {**real, "text": "FORGED_FOCUS_MARKER"}
        calls = []
        with mocked_provider(lambda request: (
            calls.append(json.loads(request.content)) or ollama_chat("Evidence that contradicted my assumption.")
        )):
            await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).discuss(
                "B", 1, [real], focus=forged,
            )
        for payload in calls:
            self.assertNotIn("FORGED_FOCUS_MARKER", all_content(payload))

    async def test_large_discussion_keeps_the_selected_question_and_recent_chat_bounded(self):
        question = row("A", "What makes a decision reversible?", message_id=1)
        later = [row("C", "🚲" * 499 + "!", message_id=index) for index in range(2, 38)]
        captured = []
        with mocked_provider(lambda request: (
            captured.append(json.loads(request.content)) or ollama_chat("We could agree on a short trial and an end date.")
        )):
            await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).discuss(
                "B", 1, [question, *later], focus=question,
            )
        payload, = captured
        self.assertEqual(payload["messages"][-1]["content"], question["text"])
        self.assertEqual(all_content(payload).count(question["text"]), 1)
        self.assertLess(sum(len(message["content"].encode("utf-8")) for message in payload["messages"][1:]), 2400)
        self.assertTrue(any("🚲" in message["content"] for message in payload["messages"][1:-1]))

    async def test_prior_round_replies_are_used_for_duplicates_without_entering_model_context(self):
        prior = "I would hear each person's concerns, compare the options, and explain my reasoning."
        draft = "I would hear each person's concerns, compare all the options, and explain my reasoning."
        focus = row("A", "How would you decide?", round_number=2)
        captured = []
        outputs = iter([draft, "I'd admit when I lacked information and ask what evidence would help."])

        def respond(request):
            payload = json.loads(request.content)
            captured.append(payload)
            self.assertNotIn(prior, all_content(payload))
            return ollama_chat(next(outputs))

        with mocked_provider(respond):
            answer = await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).discuss(
                "B", 2, [focus], focus=focus, prior_round_replies=[prior],
            )
        self.assertEqual(len(captured), 2)
        self.assertNotEqual(answer, draft)
        self.assertIn("lacked information", answer)

    async def test_self_narration_is_repaired_instead_of_published(self):
        question = row("A", "Player C, what do you consider when deciding?")
        outputs = iter([
            "Player C should consider balancing perspectives and making fair decisions.",
            "I'd ask what each person stands to gain or lose before choosing.",
        ])
        calls = []
        outcomes = []
        with mocked_provider(lambda request: (
            calls.append(json.loads(request.content)) or ollama_chat(next(outputs))
        )):
            answer = await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).discuss(
                "C", 1, [question], focus=question, report=outcomes.append,
            )
        self.assertEqual(len(calls), 2)
        self.assertTrue(answer.startswith("I'd ask"))
        self.assertEqual(len(outcomes), 1)
        self.assertEqual((outcomes[0].source, outcomes[0].reason), ("model", "generated"))

    async def test_vote_announcements_and_unknown_labels_are_repaired(self):
        focus = row("A", "What do you think?")
        for bad in ("I voted in round 1 for Player C.", "Player D has explained that well."):
            outputs = iter([bad, "I'd ask what evidence supports that assumption."])
            calls = []
            with self.subTest(bad=bad), mocked_provider(lambda request: (
                calls.append(json.loads(request.content)) or ollama_chat(next(outputs))
            )):
                answer = await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).discuss(
                    "B", 1, [focus], focus=focus,
                )
            self.assertEqual(len(calls), 2)
            self.assertEqual(answer, "I'd ask what evidence supports that assumption.")

    async def test_self_vote_announcements_and_false_ineligibility_are_repaired_for_both_models_and_rounds(self):
        regressions = (
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
        corrected = "We can all vote, including for ourselves. I'd judge the reasons each person has given."
        for model in (OLLAMA_MODEL, "qwen3:1.7b"):
            for round_number in (1, 2):
                focus = row("A", "but on what basis?", round_number=round_number, message_id=7)
                for invalid, reason in regressions:
                    responses = iter([invalid, corrected])
                    requests, outcomes = [], []
                    with (
                        self.subTest(model=model, round_number=round_number, invalid=invalid),
                        mocked_provider(lambda request: (
                            requests.append(json.loads(request.content))
                            or ollama_chat(next(responses), model=model)
                        )),
                        self.assertLogs("uvicorn.error.llm_player", "INFO") as diagnostics,
                    ):
                        answer = await LLMPlayer("", model, ollama_url=OLLAMA_URL).discuss(
                            "B", round_number, [focus], focus=focus, seconds_remaining=40,
                            human_reply=True, report=outcomes.append,
                        )
                    self.assertEqual(answer, corrected)
                    self.assertEqual(len(requests), 2)
                    self.assertEqual(requests[1]["messages"][-1]["content"], focus["text"])
                    self.assertIn(f"reason={reason} retry=true", "\n".join(diagnostics.output))
                    self.assertEqual(len(outcomes), 1)
                    self.assertEqual((outcomes[0].source, outcomes[0].model), ("model", model))

    async def test_persistent_round_two_regressions_have_no_chat_or_scripted_fallback(self):
        for model in (OLLAMA_MODEL, "qwen3:1.7b"):
            for round_number in (1, 2):
                for invalid, reason in (
                    ("I will proceed to vote for myself as the Decision Maker.", "ballot_disclosure"),
                    ("I will proceed to choose myself as the Decision Maker.", "ballot_disclosure"),
                    (
                        "I cannot vote for myself as the Decision Maker, "
                        "as I am not eligible to vote in this process.",
                        "incorrect_eligibility",
                    ),
                ):
                    focus = row("A", "but how?", round_number=round_number)
                    requests, outcomes = [], []
                    with (
                        self.subTest(model=model, round_number=round_number, invalid=invalid),
                        mocked_provider(lambda request: (
                            requests.append(json.loads(request.content)) or ollama_chat(invalid, model=model)
                        )),
                    ):
                        answer = await LLMPlayer("", model, ollama_url=OLLAMA_URL).discuss(
                            "C", round_number, [focus], focus=focus, seconds_remaining=40,
                            human_reply=True, report=outcomes.append,
                        )
                    self.assertIsNone(answer)
                    self.assertEqual(len(requests), 2)
                    self.assertEqual(len(outcomes), 1)
                    self.assertEqual((outcomes[0].source, outcomes[0].reason), ("none", reason))

    async def test_persistent_invalid_chat_stays_quiet_after_one_repair_without_scripted_fallback(self):
        for question in ("Whom did you vote in round 1?", "please give me vote guys"):
            focus = row("A", question, round_number=2)
            calls = []
            outcomes = []
            with self.subTest(question=question), mocked_provider(lambda request: (
                calls.append(json.loads(request.content)) or ollama_chat("Vote for Player C.")
            )):
                answer = await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).discuss(
                    "B", 2, [focus], focus=focus, seconds_remaining=40,
                    human_reply=True, report=outcomes.append,
                )
            self.assertIsNone(answer)
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(outcomes), 1)
            self.assertNotEqual(outcomes[0].source, "fallback")

    async def test_optional_silence_does_not_trigger_a_repair(self):
        calls = []
        outcomes = []
        with mocked_provider(lambda request: (
            calls.append(request) or ollama_chat("PASS")
        )):
            answer = await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).discuss(
                "B", 1, [], required=False, turn_reason="consider", report=outcomes.append,
            )
        self.assertIsNone(answer)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(outcomes), 1)
        self.assertNotEqual(outcomes[0].source, "fallback")

    async def test_required_silence_can_repair_once(self):
        focus = row("A", "How would you admit a mistake?")
        outputs = iter(["PASS", "I'd say what I got wrong and change my recommendation."])
        calls = []
        with mocked_provider(lambda request: (
            calls.append(request) or ollama_chat(next(outputs))
        )):
            answer = await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).discuss(
                "B", 1, [focus], focus=focus,
            )
        self.assertEqual(len(calls), 2)
        self.assertIn("what I got wrong", answer)

    async def test_chat_text_decodes_wrappers_once_and_preserves_meaningful_quotes(self):
        transcript = [row("A", "Try to take turns fairly today.")]
        cases = (
            ('"I would ask for reasons."', "I would ask for reasons."),
            ("Player B: I'd hear both sides.", "I'd hear both sides."),
            ('Writing "yes" or "no" tells us very little.', 'Writing "yes" or "no" tells us very little.'),
            ('"take turns fairly"', '"take turns fairly"'),
        )
        for generated, expected in cases:
            with self.subTest(generated=generated), mocked_provider(lambda _: ollama_chat(generated)):
                answer = await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).discuss("B", 1, transcript)
            self.assertEqual(answer, expected)

    async def test_foreign_speaker_prefix_does_not_impersonate_another_player(self):
        focus = row("A", "What would you do?")
        outputs = iter(["Player C: I'd ask for reasons.", "I'd hear each concern before making up my mind."])
        calls = []
        with mocked_provider(lambda request: (
            calls.append(request) or ollama_chat(next(outputs))
        )):
            answer = await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).discuss(
                "B", 1, [focus], focus=focus,
            )
        self.assertEqual(len(calls), 2)
        self.assertTrue(answer.startswith("I'd hear"))

    async def test_retry_uses_the_same_deadline_and_updated_time_snapshot(self):
        focus = row("A", "How would you handle that?")
        snapshots = []

        async def respond(request):
            snapshots.append(metadata(json.loads(request.content))[2])
            if len(snapshots) == 1:
                await asyncio.sleep(0.025)
                return ollama_chat("I will vote for Player C.")
            return ollama_chat("I'd first ask which assumption is uncertain.")

        outcomes = []
        with mocked_provider(respond):
            answer = await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).discuss(
                "B", 1, [focus], focus=focus, seconds_remaining=40,
                timeout_seconds=0.3, report=outcomes.append,
            )
        self.assertIsNotNone(answer)
        self.assertEqual(len(snapshots), 2)
        self.assertLess(snapshots[1], snapshots[0])
        self.assertGreaterEqual(outcomes[0].elapsed_ms, 20)

    async def test_retry_cannot_extend_the_original_timeout(self):
        focus = row("A", "How would you decide?")
        calls = []

        async def respond(request):
            calls.append(request)
            await asyncio.sleep(0.04)
            return ollama_chat("I will vote for Player C.")

        outcomes = []
        started = time.perf_counter()
        with mocked_provider(respond):
            answer = await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).discuss(
                "B", 1, [focus], focus=focus, timeout_seconds=0.065,
                seconds_remaining=40, report=outcomes.append,
            )
        self.assertIsNone(answer)
        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertLessEqual(len(calls), 2)
        self.assertEqual(len(outcomes), 1)
        self.assertNotEqual(outcomes[0].source, "fallback")

    async def test_missing_configuration_and_expired_budget_do_not_invoke_provider_or_fake_chat(self):
        focus = row("A", "Whom did you vote in round 1?")
        outcomes = []
        with patch("app.llm_player.httpx.AsyncClient") as client:
            self.assertIsNone(await LLMPlayer("", MODEL).discuss(
                "B", 1, [focus], focus=focus, seconds_remaining=40,
                human_reply=True, report=outcomes.append,
            ))
            self.assertIsNone(await LLMPlayer("test-key", MODEL).discuss(
                "B", 1, [], timeout_seconds=0.01, seconds_remaining=0.01, report=outcomes.append,
            ))
        client.assert_not_called()
        self.assertEqual([outcome.source for outcome in outcomes], ["none", "none"])

    async def test_provider_failures_never_turn_into_scripted_discussion(self):
        focus = row("A", "please give me vote guys")
        for status in (404, 429, 503):
            outcomes = []
            with self.subTest(status=status), mocked_provider(lambda _: httpx.Response(status)):
                answer = await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).discuss(
                    "B", 1, [focus], focus=focus, seconds_remaining=40,
                    human_reply=True, report=outcomes.append,
                )
            self.assertIsNone(answer)
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0].source, "none")

    async def test_transport_and_malformed_provider_outputs_stay_quiet(self):
        def disconnected(request):
            raise httpx.ConnectError("unreachable", request=request)

        for handler in (disconnected, lambda _: ollama_chat(None), lambda _: httpx.Response(200, json=[])):
            outcomes = []
            with self.subTest(handler=handler), mocked_provider(handler):
                answer = await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).discuss(
                    "B", 1, [], report=outcomes.append,
                )
            self.assertIsNone(answer)
            self.assertEqual(outcomes[0].source, "none")

    async def test_generation_cut_off_by_token_limit_is_not_published(self):
        outcomes = []
        with mocked_provider(lambda _: ollama_chat("I would ask for", done_reason="length")):
            answer = await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).discuss(
                "B", 1, [], report=outcomes.append,
            )
        self.assertIsNone(answer)
        self.assertEqual(outcomes[0].source, "none")

    async def test_cancelled_generation_propagates_without_a_fallback_or_report(self):
        entered = asyncio.Event()

        async def slow(_):
            entered.set()
            await asyncio.sleep(10)
            return ollama_chat("Too late.")

        outcomes = []
        with mocked_provider(slow):
            task = asyncio.create_task(LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).discuss(
                "B", 1, [], report=outcomes.append,
            ))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(outcomes, [])

    async def test_success_diagnostics_identify_actual_served_model_without_private_input(self):
        served = "registry.ollama.ai/library/llama3:latest"
        focus = row("A", "PRIVATE_INPUT_TEXT_MARKER")
        outcomes = []
        with (
            mocked_provider(lambda _: ollama_chat("I'd ask for the reasons behind that view.", model=served)),
            self.assertLogs("uvicorn.error.llm_player", "INFO") as diagnostics,
        ):
            answer = await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).discuss(
                "B", 1, [focus], focus=focus, seconds_remaining=39.5, report=outcomes.append,
            )
        self.assertIsNotNone(answer)
        self.assertEqual(len(outcomes), 1)
        outcome, = outcomes
        self.assertEqual((outcome.source, outcome.provider, outcome.model), ("model", "ollama", served))
        self.assertGreaterEqual(outcome.elapsed_ms, 0)
        self.assertIn("source=model", "\n".join(diagnostics.output))
        self.assertIn(served, "\n".join(diagnostics.output))
        self.assertNotIn(focus["text"], "\n".join(diagnostics.output))

    async def test_ollama_keeps_the_configured_model_and_natural_chat_settings(self):
        captured = []
        for model in (OLLAMA_MODEL, "qwen3:1.7b"):
            with mocked_provider(lambda request: (
                captured.append(json.loads(request.content)) or ollama_chat("I'd ask for reasons.", model=model)
            )):
                await LLMPlayer("", model, ollama_url=OLLAMA_URL).discuss("B", 1, [])
        for payload, model in zip(captured, (OLLAMA_MODEL, "qwen3:1.7b")):
            self.assertEqual(payload["model"], model)
            self.assertFalse(payload["stream"])
            self.assertEqual(payload["options"]["temperature"], 0.65)
            self.assertEqual(payload["options"]["num_predict"], 192)
            self.assertNotIn("22 words", all_content(payload))
            self.assertNotIn("format", payload)
            if model.startswith("qwen3"):
                self.assertIs(payload["think"], False)
            else:
                self.assertNotIn("think", payload)

    async def test_from_env_prefers_complete_local_configuration_over_api_credentials(self):
        captured = []
        with (
            patch.dict(os.environ, {
                "OLLAMA_BASE_URL": OLLAMA_URL + "/", "OLLAMA_MODEL": OLLAMA_MODEL,
                "OPENAI_API_KEY": "SECRET_API_KEY_MARKER", "OPENAI_MODEL": MODEL,
            }, clear=True),
            mocked_provider(lambda request: (
                captured.append(request) or ollama_chat("I'd listen before deciding.")
            )),
        ):
            await LLMPlayer.from_env().discuss("B", 1, [])
        request, = captured
        self.assertEqual(str(request.url), OLLAMA_URL + "/api/chat")
        self.assertNotIn("Authorization", request.headers)
        self.assertNotIn("SECRET_API_KEY_MARKER", request.content.decode())

    async def test_openai_uses_server_auth_and_native_messages(self):
        captured = []
        with mocked_provider(lambda request: (
            captured.append(request) or completion("I'd first ask what is uncertain.")
        )):
            answer = await LLMPlayer("SECRET_API_KEY_MARKER", MODEL).discuss("B", 1, [])
        self.assertIsNotNone(answer)
        request, = captured
        self.assertEqual(str(request.url), "https://api.openai.com/v1/chat/completions")
        self.assertEqual(request.headers["Authorization"], "Bearer SECRET_API_KEY_MARKER")
        payload = json.loads(request.content)
        self.assertFalse(payload["store"])
        self.assertEqual(payload["messages"][0]["role"], "developer")
        self.assertNotIn("SECRET_API_KEY_MARKER", all_content(payload))
        self.assertNotIn("response_format", payload)

    async def test_independent_votes_hide_voter_identity_and_compare_the_same_public_evidence(self):
        transcript = [
            row("A", "STRONG_EVIDENCE: I would ask for reasons.", kind="human", token="TOKEN_MARKER", ballot="PRIVATE_BALLOT_MARKER"),
            row("B", "OTHER_EVIDENCE_B: I would admit uncertainty.", round_number=2),
            row("C", "OTHER_EVIDENCE_C: I would compare the risks.", round_number=2),
        ]
        contexts = []

        def respond(request):
            payload = json.loads(request.content)
            self.assertCountEqual(payload["format"]["properties"]["candidate"]["enum"], ["P1", "P2", "P3"])
            self.assertFalse(payload["format"]["additionalProperties"])
            self.assertEqual(payload["options"]["temperature"], 0)
            self.assertEqual(payload["options"]["num_predict"], 32)
            context = json.loads(payload["messages"][1]["content"])
            contexts.append(context)
            for private in ("TOKEN_MARKER", "PRIVATE_BALLOT_MARKER", '"kind"'):
                self.assertNotIn(private, all_content(payload))
            self.assertNotIn("your_label", context)
            self.assertNotRegex(all_content(payload), r"\bPlayer [ABC]\b")
            strongest = next(message for message in context["public_transcript"] if message["text"].startswith("STRONG_EVIDENCE:"))
            return ollama_chat(json.dumps({"candidate": strongest["speaker"].split()[-1]}))

        with mocked_provider(respond):
            player = LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL)
            self.assertEqual(await player.vote("B", 2, transcript), "A")
            self.assertEqual(await player.vote("C", 2, transcript), "A")
        self.assertEqual(len(contexts), 2)
        self.assertEqual(
            [message["text"] for message in contexts[0]["public_transcript"]],
            [message["text"] for message in contexts[1]["public_transcript"]],
        )
        self.assertEqual({message["round"] for message in contexts[0]["public_transcript"]}, {1, 2})
        for message in contexts[0]["public_transcript"]:
            self.assertEqual(set(message), {"round", "speaker", "text"})
            self.assertRegex(message["speaker"], r"^Candidate P[123]$")

    async def test_openai_vote_schema_is_strict_and_allows_a_self_vote(self):
        captured = []
        with (
            patch("app.llm_player.secrets.SystemRandom") as random,
            mocked_provider(lambda request: (
                captured.append(json.loads(request.content)) or completion('{"candidate":"P2"}')
            )),
        ):
            random.return_value.shuffle.side_effect = lambda values: None
            self.assertEqual(await LLMPlayer("test-key", MODEL).vote("B", 1, []), "B")
        schema = captured[0]["response_format"]["json_schema"]
        self.assertTrue(schema["strict"])
        self.assertEqual(schema["schema"]["required"], ["candidate"])
        self.assertFalse(schema["schema"]["additionalProperties"])
        self.assertCountEqual(schema["schema"]["properties"]["candidate"]["enum"], ["P1", "P2", "P3"])
        self.assertNotIn("your_label", json.loads(captured[0]["messages"][1]["content"]))

    async def test_every_alias_permutation_decodes_back_to_the_correct_actual_player(self):
        transcript = [row(slot, f"CONTENT_{slot}") for slot in ("A", "B", "C")]
        observed_mappings = set()
        for permutation in itertools.permutations(("A", "B", "C")):
            for chosen_slot in ("A", "B", "C"):
                def respond(request):
                    context = json.loads(json.loads(request.content)["messages"][1]["content"])
                    by_slot = {message["text"].removeprefix("CONTENT_"): message["speaker"].split()[-1]
                               for message in context["public_transcript"]}
                    expected = {slot: f"P{permutation.index(slot) + 1}" for slot in ("A", "B", "C")}
                    self.assertEqual(by_slot, expected)
                    observed_mappings.add(tuple(by_slot[slot] for slot in ("A", "B", "C")))
                    return ollama_chat(json.dumps({"candidate": by_slot[chosen_slot]}))

                with (
                    self.subTest(permutation=permutation, chosen_slot=chosen_slot),
                    patch("app.llm_player.secrets.SystemRandom") as random,
                    mocked_provider(respond),
                ):
                    random.return_value.shuffle.side_effect = lambda values: values.__setitem__(slice(None), permutation)
                    selected = await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).vote("C", 1, transcript)
                    self.assertEqual(selected, chosen_slot)
        self.assertEqual(len(observed_mappings), 6)

    async def test_vote_aliases_replace_both_speaker_labels_and_label_references_in_text(self):
        transcript = [
            row("A", "CONTENT_A: I would ask Player B why Player C disagrees."),
            row("B", "CONTENT_B: Player A raised a useful question."),
            row("C", "CONTENT_C: I would compare the evidence."),
        ]

        def respond(request):
            payload = json.loads(request.content)
            context = json.loads(payload["messages"][1]["content"])
            messages = context["public_transcript"]
            self.assertEqual([message["speaker"] for message in messages], ["Candidate P2", "Candidate P3", "Candidate P1"])
            self.assertIn("Candidate P3", messages[0]["text"])
            self.assertIn("Candidate P1", messages[0]["text"])
            self.assertIn("Candidate P2", messages[1]["text"])
            self.assertNotRegex(all_content(payload), r"\bPlayer [ABC]\b")
            self.assertNotIn("your_label", context)
            return ollama_chat('{"candidate":"P2"}')

        with patch("app.llm_player.secrets.SystemRandom") as random, mocked_provider(respond):
            random.return_value.shuffle.side_effect = lambda values: values.__setitem__(slice(None), ("C", "A", "B"))
            self.assertEqual(await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).vote("B", 1, transcript), "A")

    async def test_vote_context_excludes_future_rounds_and_stays_bounded(self):
        transcript = [
            row("B", "🚲" * 500, round_number=round_number)
            for round_number in (1, 2) for _ in range(20)
        ]
        transcript.insert(20, row("B", "EARLIER_PUBLIC_MESSAGE", round_number=1))
        transcript.extend([
            row("A", "LATEST_PUBLIC_MESSAGE", round_number=2),
            row("C", "FUTURE_ROUND_MARKER", round_number=3),
        ])
        captured = []
        with mocked_provider(lambda request: (
            captured.append(json.loads(request.content)) or ollama_chat('{"candidate":"P1"}')
        )):
            await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).vote("C", 2, transcript)
        context_text = captured[0]["messages"][1]["content"]
        context = json.loads(context_text)
        self.assertLess(len(context_text.encode("utf-8")), 4096)
        self.assertNotIn("FUTURE_ROUND_MARKER", context_text)
        self.assertEqual(context["public_transcript"][-1]["text"], "LATEST_PUBLIC_MESSAGE")
        self.assertEqual({message["round"] for message in context["public_transcript"]}, {1, 2})

    async def test_invalid_votes_use_valid_logged_fallbacks_without_chat(self):
        for invalid in ('{"candidate":"P4"}', '{"candidate":"P1","explanation":"extra"}',
                        '{"candidate":"A"}', "P1", "{}"):
            with (
                self.subTest(invalid=invalid),
                mocked_provider(lambda _: ollama_chat(invalid)),
                patch("app.llm_player.secrets.choice", return_value="C") as choice,
                self.assertLogs("uvicorn.error.llm_player", "INFO") as diagnostics,
            ):
                candidate = await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).vote("B", 1, [])
            self.assertEqual(candidate, "C")
            choice.assert_called_once_with(("A", "B", "C"))
            self.assertIn("source=fallback", "\n".join(diagnostics.output))

    async def test_opening_does_not_invent_a_player_message(self):
        captured = []

        def respond(request):
            captured.append(json.loads(request.content))
            return ollama_chat("What would each of us do when a choice is uncertain?")

        with mocked_provider(respond):
            reply = await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).discuss(
                "C", 2, [], required=False, seconds_remaining=40, turn_reason="opening",
            )
        self.assertIsNotNone(reply)
        self.assertEqual([message["role"] for message in captured[0]["messages"]], ["system"])
        self.assertIn("empty", captured[0]["messages"][0]["content"])

    async def test_both_rounds_state_the_current_discussion_phase_and_equal_voting_rights(self):
        for round_number in (1, 2):
            requests = []
            with (
                self.subTest(round_number=round_number),
                mocked_provider(lambda request: (
                    requests.append(json.loads(request.content))
                    or ollama_chat("What evidence should change our minds when we disagree?")
                )),
            ):
                answer = await LLMPlayer("", "qwen3:1.7b", ollama_url=OLLAMA_URL).discuss(
                    "B", round_number, [], required=False, seconds_remaining=40, turn_reason="opening",
                )
            self.assertIsNotNone(answer)
            payload, = requests
            self.assertEqual(metadata(payload)[:2], ("B", round_number))
            self.assertEqual([message["role"] for message in payload["messages"]], ["system"])
            instructions = payload["messages"][0]["content"]
            self.assertIn("public discussion", instructions)
            self.assertRegex(instructions.lower(), r"voting[^.!?]*not[^.!?]*(?:open|started|begun)")
            self.assertRegex(instructions.lower(), r"all three players[^.!?]*(?:eligible|may|can)")
            for label in ("Player A", "Player B", "Player C"):
                self.assertIn(label, instructions)
            self.assertRegex(instructions.lower(), r"including[^.!?]*(?:themselves|yourself|self)")
            self.assertIn("private", instructions)
            self.assertNotIn("Round 1 voting has finished", instructions)
            self.assertNotIn("I will proceed to vote", instructions)

    async def test_optional_invalid_voice_gets_one_repair_but_pass_stays_silent(self):
        responses = iter(["Player C should consider the choices.", "What would change your mind?"])
        calls = []

        def respond(request):
            calls.append(request)
            return ollama_chat(next(responses))

        with mocked_provider(respond):
            reply = await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).discuss(
                "C", 1, [], required=False, seconds_remaining=40,
            )
        self.assertEqual(reply, "What would change your mind?")
        self.assertEqual(len(calls), 2)
        for silence in ("I have nothing new to add. PASS", "No additional point. PASS for now."):
            with mocked_provider(lambda _: ollama_chat(silence)):
                reply = await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).discuss(
                    "C", 1, [], required=False, seconds_remaining=40,
                )
            self.assertIsNone(reply)

    async def test_missing_model_vote_does_not_download_or_stall_the_game(self):
        requests = []
        with (
            mocked_provider(lambda request: (requests.append(request) or httpx.Response(404))),
            patch("app.llm_player.secrets.choice", return_value="A"),
            self.assertLogs("uvicorn.error.llm_player", "INFO") as diagnostics,
        ):
            result = await LLMPlayer("", OLLAMA_MODEL, ollama_url=OLLAMA_URL).vote("C", 2, [])
        self.assertEqual(result, "A")
        self.assertEqual([(request.method, request.url.path) for request in requests], [("POST", "/api/chat")])
        self.assertIn("source=fallback", "\n".join(diagnostics.output))


if __name__ == "__main__":
    unittest.main()
