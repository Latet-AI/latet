-- E4: the event log. Counts by the hour, never an event's text: what happened, for which identity as the company
-- chose, in which group, project and agent. A project is the one name a client gives: the log keeps 50 of an
-- identity's in an hour by name (a thousand of a group's, for a row that keeps no identity), the others as 'other'.
CREATE TABLE ee_events (
    hour BIGINT NOT NULL,                 -- seconds since 1970 (UTC) divided by 3600
    type TEXT NOT NULL,
    identity_id TEXT NOT NULL,            -- '' when none is kept (a person, with people: group-only)
    kind TEXT NOT NULL,                   -- person, key, or ''
    group_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    project TEXT NOT NULL,
    code TEXT NOT NULL,                   -- the event's outcome, else its reason
    decider TEXT NOT NULL,                -- who decided (an event's by), kept as people are
    category TEXT NOT NULL,               -- '': the events themselves; else a kind of value they masked
    events BIGINT NOT NULL,
    masked BIGINT NOT NULL,
    PRIMARY KEY (hour, type, identity_id, kind, group_id, agent, project, code, decider, category)
);
-- The projects the log keeps by name, of each holder in each hour: an identity's (group_id ''), or, for the rows that
-- keep no identity, a group's (identity_id ''). A write that names a project reads its holders' here, by their key,
-- under their locks, never the hour's counts; pruned with them.
CREATE TABLE ee_event_projects (
    hour BIGINT NOT NULL,
    identity_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    project TEXT NOT NULL,
    PRIMARY KEY (hour, identity_id, group_id, project)
);
-- The batches written, kept a day: one written again after an error is counted once.
CREATE TABLE ee_event_batches (id TEXT PRIMARY KEY, at DOUBLE PRECISION NOT NULL);
CREATE INDEX ee_event_batches_at ON ee_event_batches (at);
-- Keys every instance needs the same, sealed with the master key (the pseudonyms').
CREATE TABLE ee_event_keys (name TEXT PRIMARY KEY, value BYTEA NOT NULL);
-- When each job of the events last ran, and what it noted.
CREATE TABLE ee_jobs (name TEXT PRIMARY KEY, at DOUBLE PRECISION, note TEXT);
