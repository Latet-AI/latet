-- Central approvals (Latet Enterprise, E5): what waits for someone other than whoever asked, and how it ended. Never a
-- text or a value; a change's reasons (which name the folders and destinations the person chose) and a text's agent,
-- kinds and number are sealed with the master key (purpose ``approvals``, the row's id their additional data).
CREATE TABLE ee_approvals (
    id TEXT PRIMARY KEY,
    identity_id TEXT NOT NULL,
    identity_name TEXT NOT NULL,
    identity_kind TEXT NOT NULL CHECK (identity_kind IN ('person', 'key')),
    kind TEXT NOT NULL CHECK (kind IN ('change', 'batch')),
    route TEXT NOT NULL CHECK (route IN ('security', 'controllers')),
    asked_by TEXT,
    summary BYTEA NOT NULL,
    created DOUBLE PRECISION NOT NULL,
    state TEXT,
    decided DOUBLE PRECISION,
    by_id TEXT,
    by_name TEXT
);
CREATE INDEX ee_approvals_waiting ON ee_approvals (route, created) WHERE state IS NULL;
CREATE INDEX ee_approvals_identity ON ee_approvals (identity_id) WHERE state IS NULL;
CREATE INDEX ee_approvals_decided ON ee_approvals (decided) WHERE state IS NOT NULL;
-- A person's own and a key's controllers' history, and the security team's, read newest first (R65).
CREATE INDEX ee_approvals_identity_decided ON ee_approvals (identity_id, decided) WHERE state IS NOT NULL;
CREATE INDEX ee_approvals_route_decided ON ee_approvals (route, decided) WHERE state IS NOT NULL;
-- What one made in an API key's space: with it, every arm of what one person sees of the history has an index, so that
-- PostgreSQL serves their OR from them (a controller's or an admin's history is never read back to the retention's end).
CREATE INDEX ee_approvals_asked_decided ON ee_approvals (asked_by, decided)
    WHERE state IS NOT NULL AND asked_by IS NOT NULL;
