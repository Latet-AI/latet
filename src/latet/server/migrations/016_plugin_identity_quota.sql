-- The plugin bindings' quota, a row for each identity (root's ruling I-3 on P2-6a's review): an identity that fills its
-- own never refuses another identity's binding, and each identity's writers lock its row alone, never one row for the
-- whole server. The count is bounded here by 015's server-wide bound, which an identity's bindings may still reach
-- after this migration; the code keeps an identity below its own, smaller quota for new bindings. Each row's sequence
-- starts at the server-wide one, so a binding's generation never comes back.
CREATE TABLE legacy_plugin_identity_quota (
    issuer TEXT PRIMARY KEY CHECK (issuer ~ '^u_[a-z2-7]{10}$'),
    entries SMALLINT NOT NULL CHECK (entries BETWEEN 0 AND 4096),
    generation BIGINT NOT NULL CHECK (generation >= 0)
);
INSERT INTO legacy_plugin_identity_quota (issuer, entries, generation)
SELECT b.issuer, count(*),
       GREATEST(COALESCE((SELECT q.generation FROM legacy_plugin_quota q WHERE q.singleton = 1), 0), max(b.generation))
FROM legacy_plugin_binding b
GROUP BY b.issuer;
DROP TABLE legacy_plugin_quota;

-- An identity reclaims its own expired bindings.
CREATE INDEX legacy_plugin_binding_issuer_expiry
    ON legacy_plugin_binding (issuer, expires_us, session_digest);
