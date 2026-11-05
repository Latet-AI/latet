-- Each word row of a vault has a print (latet.server.prints): a cheap keyed fingerprint of its word, which each
-- instance keeps in memory to find which words of a text are any value's or fake's (latet.server.index), NULL where the
-- word is; and the change count of the transaction that wrote it last (counted), by which an instance catches up.
ALTER TABLE vault_words ADD COLUMN print BIGINT, ADD COLUMN counted BIGINT;
CREATE INDEX vault_words_print ON vault_words (identity_id, print) WHERE print IS NOT NULL;
CREATE INDEX vault_words_counted ON vault_words (identity_id, counted) WHERE counted IS NOT NULL;
CREATE INDEX vault_words_printless ON vault_words (identity_id) WHERE word IS NOT NULL AND print IS NULL;
