-- Each identity's approved request exceptions, by Latet's id and scope category alone (P2-6, D3; root's ruling on
-- F-3): written in the transaction that keeps the identity's approved settings, its rows replaced whole, so that one
-- revoked goes at once, and again as its space is built (a new version may suspend one, R-100; an edition may forbid
-- permanent ones); one past its end is never listed. `latet server status` lists the permanent ones from it, and those
-- that need approval again or that the edition does not allow. Never a description, a destination, a project's folder
-- or what an exception names: those stay in the identity's sealed settings.
CREATE TABLE request_exception_index (
    identity TEXT NOT NULL REFERENCES identities (id) ON DELETE CASCADE,
    exception TEXT NOT NULL CHECK (exception ~ '^x[0-9a-f]{10}$'),
    scope TEXT NOT NULL CHECK (scope IN ('identity', 'project')),
    permanent BOOLEAN NOT NULL,
    expires TIMESTAMPTZ,
    -- in-force: it grants what it names; suspended: approved under a request profile this version revised (it needs
    -- approval again, R-100); forbidden: permanent, and the edition allows none.
    state TEXT NOT NULL CHECK (state IN ('in-force', 'suspended', 'forbidden')),
    PRIMARY KEY (identity, exception),
    CHECK (permanent = (expires IS NULL))
);
CREATE INDEX request_exception_index_permanent ON request_exception_index (permanent, identity);
