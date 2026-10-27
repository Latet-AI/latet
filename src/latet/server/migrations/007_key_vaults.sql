-- An API key's vault is read from PostgreSQL as needed (engine.stored.StoredVault): its newest values, and the values
-- and fakes a text holds, found by the keyed hash of each one's anchor word (engine.anchors). NULL: a value or a fake
-- without one, which the space keeps in memory. version: the anchors' (engine.anchors.ANCHORS_VERSION) a word was made
-- with; one of an older version is made again.
CREATE INDEX vault_entities_recent ON vault_entities (identity_id, seq DESC);
CREATE TABLE vault_words (
    identity_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('original', 'surrogate')),
    word BYTEA,
    version SMALLINT NOT NULL,
    PRIMARY KEY (identity_id, entity_id, role),
    FOREIGN KEY (identity_id, entity_id) REFERENCES vault_entities (identity_id, id) ON DELETE CASCADE
);
CREATE INDEX vault_words_word ON vault_words (identity_id, word);
-- An API key's own limit of values (NULL: the server's vault_limit).
ALTER TABLE identities ADD COLUMN vault_limit INTEGER
    CHECK (vault_limit IS NULL OR vault_limit BETWEEN 1000 AND 100000000);
