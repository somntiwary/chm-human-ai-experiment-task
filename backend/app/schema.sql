CREATE TABLE IF NOT EXISTS sessions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    phase text NOT NULL DEFAULT 'waiting',
    round_number smallint NOT NULL DEFAULT 1 CHECK (round_number IN (1, 2)),
    discussion_seconds integer NOT NULL CHECK (discussion_seconds > 0),
    deadline timestamptz,
    reconnect_deadline timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT sessions_phase_lifecycle_check
        CHECK (phase IN ('waiting', 'discussion', 'voting', 'break', 'results', 'abandoned')
            AND (phase <> 'break' OR round_number = 1)),
    CHECK ((phase = 'discussion') = (deadline IS NOT NULL))
);

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS reconnect_deadline timestamptz;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS break_deadline timestamptz
    CHECK (break_deadline IS NULL OR phase = 'break');

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sessions'::regclass AND conname = 'sessions_phase_lifecycle_check'
    ) THEN
        ALTER TABLE sessions
            DROP CONSTRAINT IF EXISTS sessions_phase_check,
            DROP CONSTRAINT IF EXISTS sessions_lifecycle_check,
            ADD CONSTRAINT sessions_phase_lifecycle_check
                CHECK (phase IN ('waiting', 'discussion', 'voting', 'break', 'results', 'abandoned')
                    AND (phase <> 'break' OR round_number = 1));
    END IF;
END;
$migration$;

CREATE UNIQUE INDEX IF NOT EXISTS one_live_session_with_break
    ON sessions ((1)) WHERE phase IN ('waiting', 'discussion', 'voting', 'break');
DROP INDEX IF EXISTS one_live_session;
DROP INDEX IF EXISTS one_active_session;

CREATE TABLE IF NOT EXISTS players (
    session_id bigint NOT NULL REFERENCES sessions (id),
    slot text NOT NULL CHECK (slot IN ('A', 'B', 'C')),
    kind text NOT NULL CHECK (kind IN ('human', 'ai')),
    token text UNIQUE,
    join_key uuid UNIQUE,
    PRIMARY KEY (session_id, slot),
    CHECK ((token IS NULL) = (join_key IS NULL)),
    CHECK (kind = 'human' OR token IS NULL)
);

ALTER TABLE players ADD COLUMN IF NOT EXISTS round_2_ready boolean NOT NULL DEFAULT false
    CHECK (kind = 'human' OR NOT round_2_ready);

CREATE TABLE IF NOT EXISTS messages (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id bigint NOT NULL,
    round_number smallint NOT NULL CHECK (round_number IN (1, 2)),
    sender_slot text NOT NULL,
    text text NOT NULL CHECK (char_length(text) BETWEEN 1 AND 500),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (session_id, sender_slot) REFERENCES players (session_id, slot)
);

CREATE TABLE IF NOT EXISTS ballots (
    session_id bigint NOT NULL,
    round_number smallint NOT NULL CHECK (round_number IN (1, 2)),
    voter_slot text NOT NULL,
    candidate_slot text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, round_number, voter_slot),
    FOREIGN KEY (session_id, voter_slot) REFERENCES players (session_id, slot),
    FOREIGN KEY (session_id, candidate_slot) REFERENCES players (session_id, slot)
);

CREATE TABLE IF NOT EXISTS election_results (
    session_id bigint PRIMARY KEY REFERENCES sessions (id),
    winner_slot text NOT NULL,
    deciding_rule text NOT NULL
        CHECK (deciding_rule IN ('total_votes', 'round_two_votes', 'votes_from_others', 'random_draw')),
    totals jsonb NOT NULL,
    FOREIGN KEY (session_id, winner_slot) REFERENCES players (session_id, slot)
);
