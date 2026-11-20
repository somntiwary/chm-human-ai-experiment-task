import asyncio
import logging
import secrets
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

import psycopg
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from . import db
from .config import ExperimentConfig
from .discussion import ReplyTurn, direct_addressees, reply_turns
from .discussion_output import public_chat_violation
from .election import elect
from .llm_player import DiscussionOutcome, LLMPlayer, discussion_fingerprint, is_attention_ping, is_bare_greeting


logger = logging.getLogger(__name__)
turn_logger = logging.getLogger("uvicorn.error.discussion")
SLOTS = ("A", "B", "C")
RECONNECT_GRACE = timedelta(seconds=15)
ROUND_BREAK = timedelta(seconds=10)
WALL_CLOCK_CHECK_SECONDS = 0.25
MAX_PENDING_HUMAN_REPLIES = 6
MAX_IDLE_INFERENCE_SECONDS = 15.0
RoundNumber = Annotated[int, Field(strict=True, ge=1, le=2)]


class ChatAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["message"]
    round: RoundNumber
    text: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500, pattern=r"^[^\x00]+$")
    ]


class BallotAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["ballot"]
    round: RoundNumber
    candidate: Literal["A", "B", "C"]


class ReadyAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["ready"]
    round: RoundNumber


Action = Annotated[ChatAction | BallotAction | ReadyAction, Field(discriminator="type")]


class SessionFull(Exception):
    pass


class SeatExpired(Exception):
    pass


def _seat_expired(session) -> bool:
    deadline = session["reconnect_deadline"]
    return session["phase"] == "abandoned" or (deadline is not None and datetime.now(UTC) >= deadline)


def _ai_turn_open(session, phase: str, round_number: int) -> bool:
    return (
        session is not None
        and session["phase"] == phase
        and session["round_number"] == round_number
        and not _seat_expired(session)
        and (phase != "discussion" or datetime.now(UTC) < session["deadline"])
    )


def _human_message_key(text: str) -> str:
    words = discussion_fingerprint(text).split()
    while words and words[0] in {"hey", "hi", "hello", "helo", "hlo"}:
        words.pop(0)
    return " ".join(words)


def _autonomous_focus(transcript, slot: str):
    """Do not answer another player's addressed turn or dig past it to an old topic."""
    for row in reversed(transcript):
        if row["sender_slot"] == slot:
            continue
        names = direct_addressees(row["text"])
        if names and slot not in names:
            return None
        if is_bare_greeting(row["text"]):
            continue
        return row
    return None


async def _without_ai_vote_disclosures(connection, session_id, rows):
    # Preserve the audit record without repeating AI ballot declarations or false voting rules.
    disclosed = {row["text"] for row in rows if public_chat_violation(row["text"])}
    if not disclosed:
        return rows
    ai_slots = await db.ai_slots(connection, session_id)
    return [row for row in rows if row["sender_slot"] not in ai_slots or row["text"] not in disclosed]


async def _wait_for_change(changed: asyncio.Event, deadline: datetime | None) -> None:
    if deadline is None:
        await changed.wait()
        return
    while not changed.is_set():
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            return
        # Async waits are monotonic, but the stored deadline and browser use UTC.
        with suppress(TimeoutError):
            await asyncio.wait_for(changed.wait(), timeout=min(remaining, WALL_CLOCK_CHECK_SECONDS))


@dataclass(eq=False)
class ParticipantConnection:
    session_id: int
    slot: str
    updates: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=16))

    def push(self, update: dict) -> None:
        if self.updates.full():
            self.updates.get_nowait()
        self.updates.put_nowait(update)


class Experiment:
    def __init__(self, database_url: str, config: ExperimentConfig):
        self.database_url = database_url
        self.config = config
        self._lock = asyncio.Lock()
        self._clock_changed = asyncio.Event()
        self._discussion_changed = asyncio.Event()
        self._answering_discussion: tuple[int, int, str] | None = None
        self._connections: set[ParticipantConnection] = set()
        self._opening_session_id: int | None = None
        self._llm = LLMPlayer.from_env()
        self._ai_phase: tuple[int, int, str] | None = None
        self._ai_task: asyncio.Task | None = None

    async def join(self, join_key: UUID) -> str:
        async with self._lock:
            async with db.transaction(self.database_url) as connection:
                existing = await db.player_for_join(connection, join_key)
                if existing:
                    if _seat_expired(existing):
                        raise SeatExpired
                    return existing["token"]

                session = await self._active_session(connection)
                created = session is None
                if created:
                    kinds = list(self.config.participants)
                    secrets.SystemRandom().shuffle(kinds)
                    session = await db.create_session(
                        connection, zip(SLOTS, kinds), self.config.discussion_seconds
                    )
                session_id = session["id"]
                # Admit the initial group while its first socket is still connecting.
                if not created and session_id != self._opening_session_id and not self._has_connections(session_id):
                    raise SessionFull("The session is waiting for players to reconnect. Try again shortly.")
                token = secrets.token_urlsafe(32)
                if session["phase"] != "waiting" or not await db.claim_human_slot(
                    connection, session_id, token, join_key
                ):
                    raise SessionFull("The current session is full.")

                started = not await db.has_open_human_slots(connection, session_id)
                if started:
                    session = await self._start_discussion(connection, session, 1)
                    snapshot, voters, ready_slots = await self._view(connection, session)

            if created:
                self._opening_session_id = session_id
            if started:
                self._publish(session_id, snapshot, voters, ready_slots)
            self._clock_changed.set()
            return token

    async def subscribe(self, token: str) -> ParticipantConnection | None:
        async with self._lock:
            async with db.transaction(self.database_url) as connection:
                player = await db.player_for_token(connection, token)
                if player is None:
                    return None
                session = await db.session_for_update(connection, player["session_id"])
                if _seat_expired(session):
                    return None
                if session["reconnect_deadline"] is not None:
                    await db.set_reconnect_deadline(connection, session["id"], None)
                snapshot, voters, ready_slots = await self._view(connection, session)

            participant = ParticipantConnection(player["session_id"], player["slot"])
            self._connections.add(participant)
            if participant.session_id == self._opening_session_id:
                self._opening_session_id = None
            self._send_view(participant, snapshot, voters, ready_slots)
            self._clock_changed.set()
            return participant

    async def unsubscribe(self, participant: ParticipantConnection) -> None:
        async with self._lock:
            self._connections.discard(participant)
            self._clock_changed.set()

    async def act(self, participant: ParticipantConnection, action: Action) -> None:
        error = await self._submit(participant.session_id, participant.slot, action)
        if error is not None:
            participant.push({"type": "error", "message": error})

    async def _submit(self, session_id: int, slot: str, action: Action, *, ai=False) -> str | None:
        async with self._lock:
            async with db.transaction(self.database_url) as connection:
                session = await db.session_for_update(connection, session_id)
                if ai:
                    phase = "discussion" if isinstance(action, ChatAction) else "voting"
                    if not _ai_turn_open(session, phase, action.round):
                        return "That AI turn has ended."
                    if isinstance(action, ChatAction):
                        if self._discussion_changed.is_set():
                            return "superseded_by_human_message"
                        violation = public_chat_violation(action.text)
                        if violation:
                            return violation
                        fingerprint = discussion_fingerprint(action.text)
                        messages = await db.messages_for_round(connection, session_id, action.round)
                        if is_bare_greeting(action.text):
                            slots = await db.ai_slots(connection, session_id)
                            messages = [row for row in messages if row["sender_slot"] in slots]
                        elif action.round == 2:
                            slots = await db.ai_slots(connection, session_id)
                            previous = await db.messages_for_round(connection, session_id, 1)
                            messages.extend(row for row in previous if row["sender_slot"] in slots)
                        if any(discussion_fingerprint(row["text"]) == fingerprint for row in messages):
                            return "duplicate"
                updated, error = await self._apply(connection, session, slot, action)
                if error is None:
                    snapshot, voters, ready_slots = await self._view(connection, updated)

            if error is not None:
                return error

            phase_changed = (session["phase"], session["round_number"]) != (
                updated["phase"], updated["round_number"]
            )
            # A ballot updates its voter until the round is ready to advance.
            recipient = slot if isinstance(action, BallotAction) and not phase_changed else None
            self._publish(session_id, snapshot, voters, ready_slots, recipient=recipient)
            if isinstance(action, ChatAction) and not ai:
                # Finish a human answer already being generated. New input is
                # saved above and gets the next opportunity; optional turns are
                # interrupted so they cannot delay that opportunity.
                answering = self._answering_discussion
                if answering is None or answering[:2] != (session_id, action.round):
                    self._discussion_changed.set()
            if phase_changed:
                self._clock_changed.set()
            return None

    async def _apply(self, connection, session, slot: str, action: Action):
        phase = session["phase"]
        session_id = session["id"]
        round_number = session["round_number"]
        if phase == "results" and isinstance(action, BallotAction) and action.round == round_number:
            return session, None
        if phase in ("results", "abandoned") or action.round != round_number:
            return session, "That round is no longer active."

        if isinstance(action, ReadyAction):
            if phase != "break" or session["break_deadline"] is not None:
                return session, "You can join Round 2 after the break countdown."
            if not await db.record_round_2_ready(connection, session_id, slot):
                return session, "Only an assigned human player can join Round 2."
            readiness = await db.round_2_readiness(connection, session_id)
            if all(readiness.values()):
                session = await self._start_discussion(connection, session, 2)
            return session, None

        if isinstance(action, ChatAction):
            if phase != "discussion" or datetime.now(UTC) >= session["deadline"]:
                return session, "Chat is open during discussion."
            await db.record_message(connection, session_id, round_number, slot, action.text)
            return session, None

        if phase != "voting":
            return session, "Voting is open during the voting phase."
        if not await db.record_ballot(connection, session_id, round_number, slot, action.candidate):
            return session, "Your vote is already recorded."
        if len(await db.voters_for_round(connection, session_id, round_number)) == len(SLOTS):
            if round_number == 1:
                session = await db.change_phase(
                    connection, session_id, "break", 1, break_deadline=datetime.now(UTC) + ROUND_BREAK
                )
            else:
                await self._finalize(connection, session_id)
                session = await db.change_phase(connection, session_id, "results", round_number)
        return session, None

    async def _finalize(self, connection, session_id):
        """Reuse or store a result while the caller holds the session row lock."""
        result = await db.election_result(connection, session_id)
        if result is not None:
            return result

        ballots = await db.ballots_for_session(connection, session_id)
        if len(ballots) != 2 * len(SLOTS):
            raise RuntimeError("An election requires every ballot from both rounds.")
        outcome = elect(SLOTS, ballots)
        return await db.record_election_result(
            connection, session_id, outcome.winner, outcome.rule, outcome.totals
        )

    async def _start_discussion(self, connection, session, round_number):
        deadline = datetime.now(UTC) + timedelta(seconds=session["discussion_seconds"])
        return await db.change_phase(connection, session["id"], "discussion", round_number, deadline)

    async def run_clock(self) -> None:
        while True:
            try:
                deadline = await self._check_deadline()
            except psycopg.Error:
                logger.warning("Could not check the study deadlines")
                await asyncio.sleep(1)
                continue
            await _wait_for_change(self._clock_changed, deadline)

    async def _check_deadline(self) -> datetime | None:
        async with self._lock:
            self._clock_changed.clear()
            async with db.transaction(self.database_url) as connection:
                session = await self._active_session(connection)
                if session is None:
                    self._schedule_ai(None)
                    return None

                now = datetime.now(UTC)
                reconnect_deadline = session["reconnect_deadline"]
                if reconnect_deadline is None and not self._has_connections(session["id"]):
                    reconnect_deadline = now + RECONNECT_GRACE
                    await db.set_reconnect_deadline(connection, session["id"], reconnect_deadline)

                phase = session["phase"]
                deadline = session["break_deadline"] if phase == "break" else session["deadline"]
                countdown_over = deadline is not None and now >= deadline
                if countdown_over:
                    next_phase = "voting" if phase == "discussion" else "break"
                    session = await db.change_phase(
                        connection, session["id"], next_phase, session["round_number"]
                    )
                    snapshot, voters, ready_slots = await self._view(connection, session)
                    deadline = None

            if countdown_over:
                self._publish(session["id"], snapshot, voters, ready_slots)
            self._schedule_ai(session)
            deadlines = [value for value in (deadline, reconnect_deadline) if value is not None]
            return min(deadlines, default=None)

    def _schedule_ai(self, session) -> None:
        phase = None
        if session is not None and session["phase"] in ("discussion", "voting"):
            phase = (session["id"], session["round_number"], session["phase"])
        if phase == self._ai_phase:
            return
        self._ai_phase = phase
        if self._ai_task is not None:
            self._ai_task.cancel()
        self._ai_task = asyncio.create_task(self._run_ai_turns(session)) if phase else None

    async def stop_ai(self) -> None:
        if self._ai_task is not None:
            self._ai_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._ai_task

    async def _run_ai_turns(self, session) -> None:
        while True:
            try:
                async with db.transaction(self.database_url) as connection:
                    slots = await db.ai_slots(connection, session["id"])
                if slots:
                    await self._play_ai_turns(session, slots)
                return
            except psycopg.Error:
                logger.warning("AI turns could not reach PostgreSQL. Retrying outstanding turns.")
                await asyncio.sleep(0.5)

    async def _play_ai_turns(self, session, slots: list[str]) -> None:
        if session["phase"] == "discussion":
            await self._run_discussion(session, slots)
            return

        session_id, round_number = session["id"], session["round_number"]
        for slot in slots:
            transcript = await self._vote_context(session_id, round_number, slot)
            if transcript is not None:
                candidate = await self._llm.vote(slot, round_number, transcript)
                await self._submit(
                    session_id, slot, BallotAction(type="ballot", round=round_number, candidate=candidate),
                    ai=True,
                )

    async def _run_discussion(self, session, slots: list[str]) -> None:
        session_id, round_number, deadline = session["id"], session["round_number"], session["deadline"]
        duration = session["discussion_seconds"]
        stagger = min(2.0, duration * 0.08)
        opening_delay = min(5 if round_number == 1 else 9, duration * 0.2)
        opening_at = deadline - timedelta(seconds=duration - opening_delay)
        join_pause = timedelta(seconds=min(4, duration * 0.25))
        idle_pause = timedelta(seconds=min(9, duration * 0.35))
        idle_cutoff = deadline - timedelta(seconds=min(7, duration * 0.25))
        idle_pending = True
        latest_message_id = None
        ready_at = datetime.now(UTC)
        last_activity_at = ready_at
        first_opener = secrets.choice(slots)
        openers = [first_opener, *(slot for slot in slots if slot != first_opener)]
        required, optional = deque(), deque()
        cursor = None
        answered_player = None
        answered_key = None
        attempted_human_replies: set[tuple[int, str]] = set()
        primary_slot = first_opener
        prior_round_replies = []
        if round_number == 2:
            async with db.transaction(self.database_url) as connection:
                prior_round_replies = [
                    row["text"] for row in await db.messages_for_round(connection, session_id, 1)
                    if row["sender_slot"] in slots
                ]

        def log_turn(slot, trigger, focus, remaining, status, reason, outcome: DiscussionOutcome | None = None):
            turn_logger.info(
                "AI discussion session=%d round=%d slot=%s trigger=%s trigger_message_id=%s "
                "seconds_remaining=%.3f status=%s source=%s provider=%s model=%s reason=%s elapsed_ms=%s",
                session_id, round_number, slot, trigger, focus["id"] if focus is not None else "-",
                remaining, status, outcome.source if outcome else "none",
                outcome.provider if outcome else "-", (outcome.model or "-") if outcome else "-",
                reason, outcome.elapsed_ms if outcome else "-",
            )

        while datetime.now(UTC) < deadline:
            transcript = await self._discussion_transcript(session_id, round_number)
            if transcript is None:
                return
            already_spoken = {row["sender_slot"] for row in transcript}
            has_human_message = any(slot not in slots for slot in already_spoken)
            openers = [slot for slot in openers if slot not in already_spoken]
            if transcript and transcript[-1]["id"] != latest_message_id:
                latest_message_id = transcript[-1]["id"]
                last_activity_at = datetime.now(UTC)
            if cursor is None:
                # On worker restart this is only a replay boundary: messages do
                # not store which question they answered. Pending turns are local.
                latest_ai = next((row for row in reversed(transcript) if row["sender_slot"] in slots), None)
                cursor = latest_ai["id"] if latest_ai is not None else 0
                if latest_ai is not None:
                    primary_slot = latest_ai["sender_slot"]
            for row in transcript:
                if row["id"] <= cursor:
                    continue
                cursor = row["id"]
                sender = row["sender_slot"]
                if sender in slots:
                    primary_slot = sender
                    answered_player = None
                    answered_key = None
                    continue
                # Only a human contribution can open another quiet follow-up
                # opportunity. AI posts cannot sustain an endless exchange.
                idle_pending = True
                if sender == answered_player and _human_message_key(row["text"]) == answered_key:
                    # A repeated question sent during inference belongs to the
                    # answer posted immediately after it, rather than a new turn.
                    continue
                if is_attention_ping(row["text"]) and (
                    sender == answered_player or any(
                        queued["sender_slot"] == sender and not is_bare_greeting(queued["text"])
                        for _, queued in required
                    )
                ):
                    # A check-in while waiting is part of the pending exchange.
                    continue
                order = [primary_slot, *(slot for slot in slots if slot != primary_slot)]
                turns = reply_turns(row["text"], order)
                superseded = list(optional)
                optional.clear()
                for skipped, previous in superseded:
                    log_turn(skipped.slot, "direct" if skipped.direct else "conversation", previous,
                             (deadline - datetime.now(UTC)).total_seconds(), "skipped", "superseded_by_human_message")
                # Keep distinct unanswered questions, with the latest input next.
                # Repeated versions of the same question share one opportunity.
                for turn in reversed(turns):
                    if not turn.required:
                        optional.appendleft((turn, row))
                        continue
                    key = _human_message_key(row["text"])
                    repeated = [
                        (queued, previous) for queued, previous in required
                        if queued.slot == turn.slot and previous["sender_slot"] == sender
                        and _human_message_key(previous["text"]) == key
                    ]
                    for entry in repeated:
                        required.remove(entry)
                        skipped, previous = entry
                        log_turn(skipped.slot, "direct" if skipped.direct else "conversation", previous,
                                 (deadline - datetime.now(UTC)).total_seconds(), "skipped", "coalesced_human_message")
                    required.appendleft((turn, row))
                    if len(required) > MAX_PENDING_HUMAN_REPLIES:
                        skipped, previous = required.pop()
                        log_turn(skipped.slot, "direct" if skipped.direct else "conversation", previous,
                                 (deadline - datetime.now(UTC)).total_seconds(), "skipped", "human_reply_queue_full")

            now = datetime.now(UTC)
            pending = required or optional
            if required:
                next_turn_at = now
            elif optional:
                next_turn_at = ready_at
            else:
                next_turn_at = deadline
                if openers:
                    next_turn_at = last_activity_at + join_pause if has_human_message else opening_at
                elif idle_pending:
                    next_turn_at = last_activity_at + idle_pause
                next_turn_at = max(ready_at, next_turn_at)
                if now >= idle_cutoff or next_turn_at >= idle_cutoff:
                    next_turn_at = deadline
            if now < next_turn_at:
                await _wait_for_change(self._discussion_changed, min(deadline, next_turn_at))
                continue
            if now >= deadline:
                return

            idle = False
            if pending:
                turn, focus = pending.popleft()
                trigger = "direct" if turn.direct else "conversation"
                another = any(row["id"] == focus["id"] for _, row in required)
            elif openers:
                turn = ReplyTurn(openers.pop(0), required=False)
                idle = has_human_message
                trigger = "idle_join" if idle else "opening"
                focus = _autonomous_focus(transcript, turn.slot)
                another = bool(openers)
                if transcript and focus is None and any(not is_bare_greeting(row["text"]) for row in transcript):
                    continue
            else:
                idle_pending = False
                last_sender = transcript[-1]["sender_slot"] if transcript else None
                slot = min(slots, key=lambda candidate: (candidate == last_sender, candidate == primary_slot))
                turn, trigger, another = ReplyTurn(slot, required=False), "idle", False
                idle = True
            if idle:
                focus = _autonomous_focus(transcript, turn.slot)
                if focus is None and trigger != "idle_join":
                    continue

            remaining = (deadline - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                return
            human_turn = (
                (focus["id"], turn.slot)
                if focus is not None and focus["sender_slot"] not in slots else None
            )
            if human_turn is not None:
                if human_turn in attempted_human_replies:
                    log_turn(turn.slot, trigger, focus, remaining, "skipped", "human_reply_already_attempted")
                    continue
                attempted_human_replies.add(human_turn)
            primary_reply = pending is required
            reserve = min(0.35 if primary_reply else 4, duration * 0.2, remaining / 3)
            spacing = 0 if primary_reply else stagger
            budget = max(0, (remaining - reserve - spacing * another) / (1 + another))
            if idle:
                budget = min(budget, MAX_IDLE_INFERENCE_SECONDS)
            status = "error"
            reason = "unknown_error"
            outcomes: list[DiscussionOutcome] = []
            answering = None
            if primary_reply and focus is not None:
                answering = (session_id, round_number, focus["sender_slot"])
                self._answering_discussion = answering
            try:
                idle_options = {"idle": True} if idle else {}
                request = self._llm.discuss(
                    turn.slot, round_number, transcript, focus=focus, seconds_remaining=remaining,
                    timeout_seconds=budget, required=turn.required, wrap_up=False,
                    turn_reason=trigger,
                    human_reply=human_turn is not None,
                    prior_round_replies=prior_round_replies, report=outcomes.append, **idle_options,
                )
                text, interrupted = await self._interruptible_reply(request)
                if interrupted:
                    status, reason = "skipped", "superseded_by_human_message"
                    if trigger in ("opening", "idle_join"):
                        openers.insert(0, turn.slot)
                    continue
                reason = outcomes[0].reason if outcomes else "no_response"
                if text is None:
                    status = "skipped"
                else:
                    error = await self._submit(
                        session_id, turn.slot, ChatAction(type="message", round=round_number, text=text), ai=True
                    )
                    if error:
                        status = "discarded"
                        reason = (
                            error if error in ("duplicate", "superseded_by_human_message", "ballot_disclosure", "incorrect_eligibility")
                            else "round_closed"
                        )
                    else:
                        status = "posted"
                        if primary_reply:
                            primary_slot = turn.slot
                        if answering is not None:
                            # Ignore pings in the next snapshot up to this posted reply.
                            answered_player = answering[2]
                            answered_key = _human_message_key(focus["text"])
            except asyncio.CancelledError:
                status, reason = "cancelled", "round_closed"
                raise
            finally:
                if self._answering_discussion is answering:
                    self._answering_discussion = None
                log_turn(turn.slot, trigger, focus, remaining, status, reason, outcomes[0] if outcomes else None)
            if (
                primary_reply and not turn.direct and focus is not None
                and focus["sender_slot"] not in slots and len(slots) > 1
                and (status == "skipped" or status == "discarded" and reason in {"duplicate", "ballot_disclosure", "incorrect_eligibility"})
            ):
                for alternate in slots:
                    if alternate == turn.slot or (focus["id"], alternate) in attempted_human_replies:
                        continue
                    if any(queued.slot == alternate and row["id"] == focus["id"] for queued, row in required):
                        continue
                    # Promote an existing optional response instead of invoking
                    # the same player twice. Newer human questions stay ahead.
                    for entry in list(optional):
                        queued, row = entry
                        if queued.slot == alternate and row["id"] == focus["id"]:
                            optional.remove(entry)
                    position = next(
                        (index for index, (_, row) in enumerate(required) if row["id"] < focus["id"]),
                        len(required),
                    )
                    entry = (ReplyTurn(alternate, required=True), focus)
                    required.insert(position, entry)
                    if len(required) > MAX_PENDING_HUMAN_REPLIES:
                        skipped, previous = required.pop()
                        log_turn(skipped.slot, "direct" if skipped.direct else "conversation", previous,
                                 (deadline - datetime.now(UTC)).total_seconds(), "skipped", "human_reply_queue_full")
                    if entry in required:
                        log_turn(alternate, "alternate_responder", focus,
                                 (deadline - datetime.now(UTC)).total_seconds(), "queued", "alternate_responder")
            ready_at = datetime.now(UTC) + timedelta(seconds=stagger)

    async def _interruptible_reply(self, request) -> tuple[str | None, bool]:
        response = asyncio.create_task(request)
        changed = asyncio.create_task(self._discussion_changed.wait())
        try:
            done, _ = await asyncio.wait((response, changed), return_when=asyncio.FIRST_COMPLETED)
            if changed in done:
                return None, True
            return response.result(), False
        finally:
            for task in (response, changed):
                if not task.done():
                    task.cancel()
            await asyncio.gather(response, changed, return_exceptions=True)

    async def _discussion_transcript(self, session_id: int, round_number: int):
        async with self._lock:
            self._discussion_changed.clear()
            async with db.transaction(self.database_url) as connection:
                session = await db.session_for_update(connection, session_id)
                if not _ai_turn_open(session, "discussion", round_number):
                    return None
                rows = await db.messages_for_round(connection, session_id, round_number)
                rows = await _without_ai_vote_disclosures(connection, session_id, rows)
                return [{**row, "round_number": round_number} for row in rows]

    async def _vote_context(self, session_id: int, round_number: int, slot: str):
        async with self._lock:
            async with db.transaction(self.database_url) as connection:
                session = await db.session_for_update(connection, session_id)
                if not _ai_turn_open(session, "voting", round_number):
                    return None
                if slot in await db.voters_for_round(connection, session_id, round_number):
                    return None
                rows = await db.public_transcript(connection, session_id, round_number)
                return await _without_ai_vote_disclosures(connection, session_id, rows)

    async def _active_session(self, connection):
        session = await db.active_session(connection)
        if session is not None and _seat_expired(session):
            await db.change_phase(connection, session["id"], "abandoned", session["round_number"])
            return None
        return session

    def _has_connections(self, session_id: int) -> bool:
        return any(peer.session_id == session_id for peer in self._connections)

    async def _view(self, connection, session) -> tuple[dict, set[str], set[str]]:
        phase = session["phase"]
        round_number = session["round_number"]
        messages = []
        if phase in ("discussion", "voting"):
            rows = await db.messages_for_round(connection, session["id"], round_number)
            rows = await _without_ai_vote_disclosures(connection, session["id"], rows)
            messages = [
                {"id": row["id"], "sender": row["sender_slot"], "text": row["text"]}
                for row in rows
            ]
        voters = set()
        if phase == "voting":
            voters = await db.voters_for_round(connection, session["id"], round_number)
        ready_slots = set()
        if phase == "break":
            readiness = await db.round_2_readiness(connection, session["id"])
            ready_slots = {slot for slot, ready in readiness.items() if ready}
        deadline = session["break_deadline"] if phase == "break" else session["deadline"]
        snapshot = {
            "type": "state",
            "phase": phase,
            "round": round_number if phase != "waiting" else None,
            "deadline": deadline.isoformat() if deadline else None,
            "server_time": datetime.now(UTC).isoformat(),
            "players": [{"slot": slot, "label": f"Player {slot}"} for slot in SLOTS],
            "messages": messages,
        }
        if phase == "results":
            # Phase 2 sessions can have saved ballots without a saved result.
            result = await self._finalize(connection, session["id"])
            snapshot["result"] = {
                "winner": result["winner_slot"],
                "rule": result["deciding_rule"],
                "totals": {slot: result["totals"][slot] for slot in SLOTS},
            }
        return snapshot, voters, ready_slots

    def _publish(self, session_id, snapshot, voters, ready_slots, *, recipient=None) -> None:
        for participant in self._connections:
            if participant.session_id == session_id and recipient in (None, participant.slot):
                self._send_view(participant, snapshot, voters, ready_slots)

    @staticmethod
    def _send_view(participant, snapshot, voters, ready_slots) -> None:
        participant.push({
            **snapshot,
            "you": participant.slot,
            "has_voted": participant.slot in voters,
            "round_2_ready": participant.slot in ready_slots,
        })
