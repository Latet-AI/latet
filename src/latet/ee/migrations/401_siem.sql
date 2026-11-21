-- E4: each event queued for each SIEM output, until that output took it.
CREATE TABLE ee_siem_queue (
    seq BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    output TEXT NOT NULL,
    at DOUBLE PRECISION NOT NULL,         -- when it was queued
    type TEXT NOT NULL DEFAULT '',        -- the event's type: a queue kept short drops `apps` first, `admin` never
    ref BIGINT,                           -- an `admin` event's entry in the administration log (a gap's first)
    event TEXT NOT NULL                   -- the event's JSON: counts and codes, never a value
);
CREATE INDEX ee_siem_queue_output ON ee_siem_queue (output, seq);
CREATE INDEX ee_siem_queue_type ON ee_siem_queue (output, type, seq);
-- How each output is doing, the same for every instance: what was dropped, the last delivery, the failure going on,
-- and when to try again.
CREATE TABLE ee_siem_state (
    output TEXT PRIMARY KEY,
    dropped BIGINT NOT NULL DEFAULT 0,
    last_ok DOUBLE PRECISION,
    last_error TEXT,
    failing_since DOUBLE PRECISION,
    retry_at DOUBLE PRECISION,
    tries INTEGER NOT NULL DEFAULT 0,
    blocked BOOLEAN NOT NULL DEFAULT false,  -- an entry of the administration log it refuses holds the queue (I5)
    collapsed BIGINT NOT NULL DEFAULT 0,     -- entries the bound collapsed into a gap marker, until it delivers again
    held_to BIGINT                           -- the last place of a run at its head it refused, each alone: a skip drops it
);
