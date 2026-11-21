-- E6, synced terms (Latet Enterprise). No term is held in clear: values are sealed with the server's master key (its
-- `terms` sealer, the row's id as additional data), and a term's id is a hash keyed with 32 random bytes kept sealed in
-- ee_terms_state.
CREATE TABLE ee_terms (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    type TEXT NOT NULL,
    value BYTEA NOT NULL,
    first_seen DOUBLE PRECISION NOT NULL,
    gone DOUBLE PRECISION
);
CREATE INDEX ee_terms_source ON ee_terms (source);
CREATE INDEX ee_terms_gone ON ee_terms (gone) WHERE gone IS NOT NULL;
CREATE TABLE ee_term_runs (
    source TEXT PRIMARY KEY,
    run JSONB,
    asked DOUBLE PRECISION,
    asked_by TEXT
);
CREATE TABLE ee_terms_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version BIGINT NOT NULL,
    ids_key BYTEA
);
INSERT INTO ee_terms_state (id, version) VALUES (1, 0);
