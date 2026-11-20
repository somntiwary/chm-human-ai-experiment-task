import logging
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


logger = logging.getLogger(__name__)


def database_is_ready(database_url: str) -> bool:
    try:
        with psycopg.connect(database_url, connect_timeout=3, autocommit=True) as connection:
            connection.execute("SELECT 1")
    except psycopg.Error:
        logger.warning("PostgreSQL readiness check failed")
        return False
    return True


@asynccontextmanager
async def transaction(database_url: str):
    async with await psycopg.AsyncConnection.connect(
        database_url, row_factory=dict_row, connect_timeout=3
    ) as connection:
        yield connection


async def initialize(database_url: str) -> None:
    schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    async with transaction(database_url) as connection:
        await connection.execute(schema)


async def active_session(connection):
    cursor = await connection.execute(
        "SELECT * FROM sessions WHERE phase IN ('waiting', 'discussion', 'voting', 'break')"
    )
    return await cursor.fetchone()


async def session_for_update(connection, session_id):
    cursor = await connection.execute(
        "SELECT * FROM sessions WHERE id = %s FOR UPDATE", (session_id,)
    )
    return await cursor.fetchone()


async def create_session(connection, slots, discussion_seconds):
    cursor = await connection.execute(
        "INSERT INTO sessions (discussion_seconds) VALUES (%s) RETURNING *",
        (discussion_seconds,),
    )
    session = await cursor.fetchone()
    async with connection.cursor() as cursor:
        await cursor.executemany(
            "INSERT INTO players (session_id, slot, kind) VALUES (%s, %s, %s)",
            [(session["id"], slot, kind) for slot, kind in slots],
        )
    return session


async def player_for_join(connection, join_key):
    cursor = await connection.execute(
        """
        SELECT players.token, sessions.phase, sessions.reconnect_deadline
        FROM players JOIN sessions ON sessions.id = players.session_id
        WHERE players.join_key = %s
        """,
        (join_key,),
    )
    return await cursor.fetchone()


async def player_for_token(connection, token):
    cursor = await connection.execute(
        "SELECT session_id, slot FROM players WHERE token = %s", (token,)
    )
    return await cursor.fetchone()


async def claim_human_slot(connection, session_id, token, join_key) -> bool:
    cursor = await connection.execute(
        """
        UPDATE players SET token = %s, join_key = %s
        WHERE session_id = %s AND slot = (
            SELECT slot FROM players
            WHERE session_id = %s AND kind = 'human' AND token IS NULL
            ORDER BY slot LIMIT 1
        )
        """,
        (token, join_key, session_id, session_id),
    )
    return cursor.rowcount == 1


async def has_open_human_slots(connection, session_id) -> bool:
    cursor = await connection.execute(
        """
        SELECT 1 FROM players
        WHERE session_id = %s AND kind = 'human' AND token IS NULL
        LIMIT 1
        """,
        (session_id,),
    )
    return await cursor.fetchone() is not None


async def ai_slots(connection, session_id) -> list[str]:
    cursor = await connection.execute(
        "SELECT slot FROM players WHERE session_id = %s AND kind = 'ai' ORDER BY slot",
        (session_id,),
    )
    return [row["slot"] for row in await cursor.fetchall()]


async def change_phase(connection, session_id, phase, round_number, deadline=None, *, break_deadline=None):
    cursor = await connection.execute(
        """
        UPDATE sessions
        SET phase = %s, round_number = %s, deadline = %s, break_deadline = %s,
            reconnect_deadline = CASE
                WHEN %s IN ('results', 'abandoned') THEN NULL ELSE reconnect_deadline
            END
        WHERE id = %s RETURNING *
        """,
        (phase, round_number, deadline, break_deadline, phase, session_id),
    )
    return await cursor.fetchone()


async def set_reconnect_deadline(connection, session_id, deadline):
    await connection.execute(
        "UPDATE sessions SET reconnect_deadline = %s WHERE id = %s",
        (deadline, session_id),
    )


async def record_round_2_ready(connection, session_id, slot) -> bool:
    cursor = await connection.execute(
        """
        UPDATE players SET round_2_ready = true
        WHERE session_id = %s AND slot = %s AND kind = 'human' AND token IS NOT NULL
        """,
        (session_id, slot),
    )
    return cursor.rowcount == 1


async def round_2_readiness(connection, session_id) -> dict[str, bool]:
    cursor = await connection.execute(
        "SELECT slot, round_2_ready FROM players WHERE session_id = %s AND kind = 'human'",
        (session_id,),
    )
    return {row["slot"]: row["round_2_ready"] for row in await cursor.fetchall()}


async def record_message(connection, session_id, round_number, slot, text):
    await connection.execute(
        "INSERT INTO messages (session_id, round_number, sender_slot, text) VALUES (%s, %s, %s, %s)",
        (session_id, round_number, slot, text),
    )


async def messages_for_round(connection, session_id, round_number):
    cursor = await connection.execute(
        """
        SELECT id, sender_slot, text FROM messages
        WHERE session_id = %s AND round_number = %s ORDER BY id
        """,
        (session_id, round_number),
    )
    return await cursor.fetchall()


async def public_transcript(connection, session_id, round_number):
    cursor = await connection.execute(
        """
        SELECT round_number, sender_slot, text FROM messages
        WHERE session_id = %s AND round_number <= %s ORDER BY round_number, id
        """,
        (session_id, round_number),
    )
    return await cursor.fetchall()


async def record_ballot(connection, session_id, round_number, voter, candidate) -> bool:
    cursor = await connection.execute(
        """
        INSERT INTO ballots (session_id, round_number, voter_slot, candidate_slot)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (session_id, round_number, voter_slot) DO NOTHING
        """,
        (session_id, round_number, voter, candidate),
    )
    return cursor.rowcount == 1


async def voters_for_round(connection, session_id, round_number) -> set[str]:
    cursor = await connection.execute(
        "SELECT voter_slot FROM ballots WHERE session_id = %s AND round_number = %s",
        (session_id, round_number),
    )
    return {row["voter_slot"] for row in await cursor.fetchall()}


async def ballots_for_session(connection, session_id):
    cursor = await connection.execute(
        """
        SELECT round_number, voter_slot, candidate_slot
        FROM ballots WHERE session_id = %s
        """,
        (session_id,),
    )
    return await cursor.fetchall()


async def election_result(connection, session_id):
    cursor = await connection.execute(
        "SELECT winner_slot, deciding_rule, totals FROM election_results WHERE session_id = %s",
        (session_id,),
    )
    return await cursor.fetchone()


async def record_election_result(connection, session_id, winner, rule, totals):
    cursor = await connection.execute(
        """
        INSERT INTO election_results (session_id, winner_slot, deciding_rule, totals)
        VALUES (%s, %s, %s, %s)
        RETURNING winner_slot, deciding_rule, totals
        """,
        (session_id, winner, rule, Jsonb(totals)),
    )
    return await cursor.fetchone()
