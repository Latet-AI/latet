-- A token of an API key may forget values of its vault (latet.server.grants, Task 11b): a permission of its own, never
-- implied, so never NULL's (scan, mask and unmask, as before). The column takes the forms grants.stored makes with it
-- too, sorted and joined by commas as the others are, and no other.
ALTER TABLE tokens DROP CONSTRAINT tokens_permissions_check;
ALTER TABLE tokens ADD CONSTRAINT tokens_permissions_check CHECK (
    permissions IS NULL OR permissions IN (
        'scan', 'unmask', 'scan,unmask', 'mask,scan',
        'forget', 'forget,scan', 'forget,unmask', 'forget,scan,unmask', 'forget,mask,scan', 'forget,mask,scan,unmask'
    )
);

-- The local reviewer's remembered answers, each with the keyed hashes of the values it found (its findings, and those
-- it could not place), so that a forget finds the answers that hold a value through an index, never by opening every
-- answer (review 11b's I2). NULL: an answer kept before, which the key's next forget gives its hashes first, a part at a
-- time, outside its own transaction (PgVaultStore.index_answers); an answer that does not open gets none ('{}').
ALTER TABLE reviews ADD COLUMN value_keys BYTEA[];
CREATE INDEX reviews_values ON reviews USING gin (value_keys);
CREATE INDEX reviews_unindexed ON reviews (identity_id, key) WHERE value_keys IS NULL;

-- The history's rows, each with the keyed hashes of the vault's entries it names (a block's replacements, an exchange's
-- answer and sources, a reviewer's record's findings), so that the values of an API key forgotten by themselves take
-- the rows that name them, with their old fakes, found through an index (Ruling 11b-R1). NULL: a row kept before, which
-- the key's next forget gives its hashes first, outside its own transaction (PgHistoryStore.index_entities).
ALTER TABLE blocks ADD COLUMN entity_keys BYTEA[];
ALTER TABLE exchanges ADD COLUMN entity_keys BYTEA[];
ALTER TABLE review_records ADD COLUMN entity_keys BYTEA[];
CREATE INDEX blocks_entities ON blocks USING gin (entity_keys);
CREATE INDEX exchanges_entities ON exchanges USING gin (entity_keys);
CREATE INDEX review_records_entities ON review_records USING gin (entity_keys);
CREATE INDEX blocks_unnamed ON blocks (identity_id, id) WHERE entity_keys IS NULL;
CREATE INDEX exchanges_unnamed ON exchanges (identity_id, id) WHERE entity_keys IS NULL;
CREATE INDEX review_records_unnamed ON review_records (identity_id, id) WHERE entity_keys IS NULL;
