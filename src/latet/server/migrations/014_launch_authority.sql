CREATE TABLE launch_registry (
    singleton SMALLINT PRIMARY KEY CHECK (singleton = 1),
    format SMALLINT NOT NULL CHECK (format = 1),
    epoch BYTEA NOT NULL CHECK (octet_length(epoch) = 16),
    sealed_secret BYTEA NOT NULL CHECK (octet_length(sealed_secret) BETWEEN 30 AND 256),
    wrap_version SMALLINT NOT NULL CHECK (wrap_version BETWEEN 1 AND 255),
    revision NUMERIC(20,0) NOT NULL CHECK (revision BETWEEN 0 AND 18446744073709551615),
    entries SMALLINT NOT NULL CHECK (entries BETWEEN 0 AND 4096),
    bindings SMALLINT NOT NULL CHECK (bindings BETWEEN 0 AND 1024 AND bindings <= entries),
    tag BYTEA NOT NULL CHECK (octet_length(tag) = 32)
);

CREATE TABLE launch_identity (
    issuer TEXT PRIMARY KEY CHECK (issuer ~ '^u_[a-z2-7]{10}$'),
    epoch BYTEA NOT NULL CHECK (octet_length(epoch) = 16),
    revision NUMERIC(20,0) NOT NULL CHECK (revision BETWEEN 0 AND 18446744073709551615),
    entries SMALLINT NOT NULL CHECK (entries BETWEEN 1 AND 4096),
    bindings SMALLINT NOT NULL CHECK (bindings BETWEEN 0 AND 64 AND bindings <= entries),
    tag BYTEA NOT NULL CHECK (octet_length(tag) = 32)
);

CREATE TABLE launch_entry (
    locator BYTEA PRIMARY KEY CHECK (octet_length(locator) = 32),
    launch_id BYTEA NOT NULL UNIQUE CHECK (octet_length(launch_id) = 16),
    issuer TEXT NOT NULL REFERENCES launch_identity (issuer),
    epoch BYTEA NOT NULL CHECK (octet_length(epoch) = 16),
    revision NUMERIC(20,0) NOT NULL CHECK (revision BETWEEN 0 AND 18446744073709551615),
    generation NUMERIC(20,0) NOT NULL CHECK (generation BETWEEN 0 AND 18446744073709551615),
    op_seq NUMERIC(20,0) NOT NULL CHECK (op_seq BETWEEN 0 AND 18446744073709551615),
    state SMALLINT NOT NULL CHECK (state BETWEEN 1 AND 3),
    deadline_us BIGINT NOT NULL CHECK (deadline_us >= 0),
    binding_reserved BOOLEAN NOT NULL CHECK (binding_reserved OR state = 3),
    binding_digest BYTEA NOT NULL CHECK (octet_length(binding_digest) = 32),
    published_token BYTEA CHECK (octet_length(published_token) = 16),
    published_blob_digest BYTEA CHECK (octet_length(published_blob_digest) = 32),
    terminal_reason SMALLINT NOT NULL CHECK (terminal_reason BETWEEN 0 AND 3),
    disclosure_possible BOOLEAN NOT NULL,
    exit_confirmed BOOLEAN NOT NULL,
    op_kind SMALLINT NOT NULL CHECK (op_kind BETWEEN 0 AND 5),
    op_id BYTEA CHECK (octet_length(op_id) = 16),
    op_owner BYTEA CHECK (octet_length(op_owner) = 16),
    op_base_gen NUMERIC(20,0) CHECK (op_base_gen BETWEEN 0 AND 18446744073709551615),
    op_target_gen NUMERIC(20,0) CHECK (op_target_gen BETWEEN 0 AND 18446744073709551615),
    op_payload_digest BYTEA CHECK (octet_length(op_payload_digest) = 32),
    tag BYTEA NOT NULL CHECK (octet_length(tag) = 32),
    CHECK ((published_token IS NULL) = (published_blob_digest IS NULL)),
    CHECK (state <> 2 OR (published_token IS NOT NULL AND disclosure_possible)),
    CHECK ((state = 3) = (terminal_reason <> 0)),
    CHECK (NOT exit_confirmed OR state = 3),
    CHECK ((op_kind = 0) = (op_id IS NULL)),
    CHECK ((op_kind = 0) = (op_owner IS NULL)),
    CHECK ((op_kind = 0) = (op_base_gen IS NULL)),
    CHECK ((op_kind = 0) = (op_target_gen IS NULL)),
    CHECK ((op_kind IN (0,5)) = (op_payload_digest IS NULL)),
    CHECK (binding_reserved OR (op_kind = 0 AND published_token IS NULL))
);

CREATE INDEX launch_entry_issuer_state ON launch_entry (issuer, state);
CREATE INDEX launch_entry_binding_deadline ON launch_entry (binding_reserved, deadline_us);
