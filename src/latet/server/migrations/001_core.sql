-- Latet on a server, its core: identities and their secrets, the administration's tokens and log, and each identity's
-- settings, vault and history. Values, texts, fakes and images are sealed (with the identity's root key, itself
-- sealed with the server's master key); what is in clear holds none: ids, keyed hashes, kinds, times, counts, rule
-- ids, flags, agent and route names.

CREATE TABLE identities (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('person', 'key')),
    name TEXT NOT NULL,
    email TEXT,
    disabled BOOLEAN NOT NULL DEFAULT FALSE,
    created DOUBLE PRECISION NOT NULL,
    last_seen DOUBLE PRECISION,
    source TEXT NOT NULL DEFAULT 'local' CHECK (source IN ('local', 'scim', 'oidc')),
    external_id TEXT UNIQUE
);

-- Every secret is kept by a keyed hash (HMAC-SHA256 under a key derived from the master key, in hex), and every row of
-- the three tables below carries a tag, keyed the same way, over what the row says: someone who writes the database
-- without the master key can make neither the hash of a secret of his own choosing nor a row that a lookup believes.
CREATE TABLE tokens (
    id TEXT PRIMARY KEY,
    identity_id TEXT NOT NULL REFERENCES identities (id) ON DELETE CASCADE,
    hash TEXT NOT NULL UNIQUE,      -- keyed hash of the secret
    route TEXT NOT NULL UNIQUE,     -- keyed hash of the route key derived from it
    label TEXT NOT NULL,
    created DOUBLE PRECISION NOT NULL,
    last_used DOUBLE PRECISION,
    tag TEXT NOT NULL               -- over id, identity_id, hash and route
);
CREATE INDEX tokens_identity ON tokens (identity_id);

CREATE TABLE codes (
    hash TEXT PRIMARY KEY,          -- keyed hash of a one-time code
    identity_id TEXT NOT NULL REFERENCES identities (id) ON DELETE CASCADE,
    expires DOUBLE PRECISION NOT NULL,
    tag TEXT NOT NULL               -- over hash, identity_id and expires
);

CREATE TABLE admin_tokens (
    id TEXT PRIMARY KEY,
    hash TEXT NOT NULL UNIQUE,      -- keyed hash of the secret
    label TEXT NOT NULL,
    created DOUBLE PRECISION NOT NULL,
    last_used DOUBLE PRECISION,
    expires DOUBLE PRECISION,       -- NULL: never (issued with --never)
    tag TEXT NOT NULL               -- over id, hash and expires
);

CREATE TABLE root_keys (
    identity_id TEXT PRIMARY KEY REFERENCES identities (id) ON DELETE CASCADE,
    sealed BYTEA NOT NULL,          -- with the master key
    created DOUBLE PRECISION NOT NULL
);

-- A text sealed with the master key when the server first starts, opened at every start: a start with another key
-- stops there, rather than at the first identity whose root key it fails to open.
CREATE TABLE master_check (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    sealed BYTEA NOT NULL,          -- with the master key
    created DOUBLE PRECISION NOT NULL
);

CREATE TABLE settings (
    owner TEXT NOT NULL,            -- '' for the shared settings, else the identity's id
    identity_id TEXT REFERENCES identities (id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('shared', 'own', 'reviewed')),
    data BYTEA NOT NULL,            -- sealed JSON: the shared with the master key, an identity's with its root key
    changed DOUBLE PRECISION NOT NULL,
    changed_by TEXT,
    -- Counted by each write of an identity's own part, which names the count it read: one made on an older copy than
    -- another instance's write is refused (compare-and-set), never written over it.
    version BIGINT NOT NULL DEFAULT 1,
    PRIMARY KEY (owner, kind),
    CHECK ((identity_id IS NULL AND owner = '') OR owner = identity_id)
);

-- How many announced changes an identity's vault, its settings and the shared settings have had: a change bumps its
-- owner's counter in its own transaction, and its announcement carries the count, so that an instance that missed the
-- announcement finds the change by comparing. (The waiting changes of an identity's settings are the table `changes`.)
-- An identity's two counts go with it, as the rest of its space does.
CREATE TABLE change_counters (
    owner TEXT PRIMARY KEY,         -- '' for the shared settings, '<id>:settings' for an identity's, else its id
    identity_id TEXT GENERATED ALWAYS AS (NULLIF(split_part(owner, ':', 1), '')) STORED
        REFERENCES identities (id) ON DELETE CASCADE,  -- whose count it is: none for the shared settings'
    counter BIGINT NOT NULL
);

CREATE TABLE vault_entities (
    identity_id TEXT NOT NULL REFERENCES identities (id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    seq BIGSERIAL,
    etype TEXT NOT NULL,
    eclass INTEGER NOT NULL,
    original BYTEA NOT NULL,        -- sealed
    original_key BYTEA NOT NULL,    -- keyed hash of the value alone, no type in it: one entity per value
    surrogate BYTEA NOT NULL,       -- sealed
    surrogate_key BYTEA NOT NULL,   -- keyed hash of the fake: one value per fake
    counter INTEGER NOT NULL,
    created DOUBLE PRECISION NOT NULL,
    rule TEXT,
    PRIMARY KEY (identity_id, id),
    UNIQUE (identity_id, original_key),
    UNIQUE (identity_id, surrogate_key)
);

-- Every string a vault uses, a value's or a fake's, by its keyed hash: a fake is never a value already known, nor
-- another fake, whichever instance makes it. A value equal to a fake given before stays a value: the fake's row names
-- its entity as well (also), and when the fake's entity is forgotten the row becomes the value's.
CREATE TABLE vault_strings (
    identity_id TEXT NOT NULL REFERENCES identities (id) ON DELETE CASCADE,
    key BYTEA NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('original', 'surrogate')),
    entity_id TEXT NOT NULL,
    also TEXT,                      -- the entity whose value the string is as well
    PRIMARY KEY (identity_id, key)
);
CREATE INDEX vault_strings_entity ON vault_strings (identity_id, entity_id);
CREATE INDEX vault_strings_also ON vault_strings (identity_id, also) WHERE also IS NOT NULL;

CREATE TABLE reviews (
    identity_id TEXT NOT NULL REFERENCES identities (id) ON DELETE CASCADE,
    key BYTEA NOT NULL,             -- keyed hash of the text the local reviewer read
    data BYTEA NOT NULL,            -- sealed
    ts DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (identity_id, key)
);
CREATE INDEX reviews_ts ON reviews (identity_id, ts);

CREATE TABLE image_decisions (
    identity_id TEXT NOT NULL REFERENCES identities (id) ON DELETE CASCADE,
    key BYTEA NOT NULL,
    data BYTEA NOT NULL,
    ts DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (identity_id, key)
);

CREATE TABLE blocks (
    identity_id TEXT NOT NULL REFERENCES identities (id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    kind TEXT NOT NULL,
    data BYTEA NOT NULL,            -- sealed [masked text, replacements]
    last_seen DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (identity_id, id)
);
CREATE INDEX blocks_seen ON blocks (identity_id, last_seen);

-- A conversation's key and a project's id are kept as the identity's keyed hashes (a project's id is an unkeyed hash of
-- its folder, which a list of usual folders would give back): the history's filters key what they look for alike.
CREATE TABLE exchanges (
    identity_id TEXT NOT NULL REFERENCES identities (id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    seq BIGSERIAL,
    ts DOUBLE PRECISION NOT NULL,   -- when its request began
    recorded DOUBLE PRECISION NOT NULL, -- when it was kept: the time its blocks and images were last carried at
    session TEXT NOT NULL,          -- keyed hash of the conversation's key
    agent TEXT NOT NULL,
    flags TEXT NOT NULL,            -- ' blocked leak ' (padded, for LIKE)
    upstream TEXT,
    new_masked INTEGER NOT NULL DEFAULT 0,
    project TEXT,                   -- keyed hash of the project's id
    summary BYTEA NOT NULL,         -- sealed JSON
    refs BYTEA,
    request_headers BYTEA,
    response BYTEA,
    leaks BYTEA,
    body_bytes INTEGER NOT NULL DEFAULT 0,
    images BYTEA,
    PRIMARY KEY (identity_id, id)
);
CREATE INDEX exchanges_ts ON exchanges (identity_id, ts);
CREATE INDEX exchanges_session ON exchanges (identity_id, session, seq);
CREATE INDEX exchanges_project ON exchanges (identity_id, project, ts);

CREATE TABLE review_records (
    identity_id TEXT NOT NULL REFERENCES identities (id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    ts DOUBLE PRECISION NOT NULL,
    session TEXT NOT NULL,          -- keyed hash of the conversation's key
    project TEXT,                   -- keyed hash of the project's id
    block TEXT NOT NULL,
    kind TEXT NOT NULL,
    inline BOOLEAN NOT NULL,
    summary BYTEA NOT NULL,         -- sealed JSON
    PRIMARY KEY (identity_id, id)
);
CREATE INDEX review_records_ts ON review_records (identity_id, ts);

CREATE TABLE images (
    identity_id TEXT NOT NULL REFERENCES identities (id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    data BYTEA NOT NULL,
    last_seen DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (identity_id, id)
);

-- How many bytes each identity's history keeps (its exchanges, blocks, images and reviews' records: their sealed
-- values, and a few hundred bytes a row for the rest), counted in the transaction that keeps or drops them: what the
-- server's limit of a history (history_limit_mb) is held to.
CREATE TABLE history_bytes (
    identity_id TEXT PRIMARY KEY REFERENCES identities (id) ON DELETE CASCADE,
    bytes BIGINT NOT NULL
);

CREATE TABLE changes (
    identity_id TEXT NOT NULL REFERENCES identities (id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    data BYTEA,                     -- sealed: the change and the settings it would apply; NULL once it ended
    created DOUBLE PRECISION NOT NULL,
    state TEXT,                     -- how it ended (approved, rejected, stale, superseded)
    ended DOUBLE PRECISION,
    PRIMARY KEY (identity_id, id)
);

-- The administration log is a hash chain, keyed like the tokens' tags: each entry carries the hash of the one before it
-- and its own, and someone who writes the table without the key cannot make either.
CREATE TABLE audit (
    id BIGSERIAL PRIMARY KEY,
    ts DOUBLE PRECISION NOT NULL,
    actor TEXT NOT NULL,            -- an identity's id, admin:<token id>, or server
    action TEXT NOT NULL,           -- <area>.<verb>
    target TEXT,
    details JSONB NOT NULL DEFAULT '{}',
    address TEXT,
    prev TEXT NOT NULL,             -- the hash of the entry before (the anchor's for the oldest kept; '' at first)
    hash TEXT NOT NULL              -- keyed hash (HMAC-SHA256) of prev and this entry's fields
);
CREATE INDEX audit_ts ON audit (ts);
CREATE INDEX audit_action ON audit (action, ts);

-- Where the chain starts once entries were pruned: the id and hash of the last one pruned, with a keyed tag over both
-- (a pruned log whose anchor was written by hand, from a hash read in the table, does not verify).
CREATE TABLE audit_anchor (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    through BIGINT NOT NULL,
    hash TEXT NOT NULL,
    tag TEXT NOT NULL               -- over through and hash
);
