-- Separate protected authority for authenticated Claude project bindings.
CREATE TABLE legacy_plugin_quota (
    singleton SMALLINT PRIMARY KEY CHECK (singleton = 1),
    entries SMALLINT NOT NULL CHECK (entries BETWEEN 0 AND 4096),
    generation BIGINT NOT NULL CHECK (generation >= 0)
);
INSERT INTO legacy_plugin_quota VALUES (1, 0, 0);

CREATE TABLE legacy_plugin_binding (
    issuer TEXT NOT NULL CHECK (issuer ~ '^u_[a-z2-7]{10}$'),
    session_digest BYTEA NOT NULL CHECK (octet_length(session_digest) = 32),
    project_id BYTEA NOT NULL CHECK (octet_length(project_id) = 6),
    binding_digest BYTEA NOT NULL CHECK (octet_length(binding_digest) = 32),
    required BOOLEAN NOT NULL,
    generation BIGINT NOT NULL CHECK (generation > 0),
    expires_us BIGINT NOT NULL CHECK (expires_us >= 0),
    PRIMARY KEY (issuer, session_digest)
);
CREATE INDEX legacy_plugin_binding_expiry
    ON legacy_plugin_binding (expires_us, issuer, session_digest);
