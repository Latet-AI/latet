-- An API key's value or fake that its vault finds inside a word (a secret; a fake that is no word, as an IBAN's) is kept
-- with its edges as well as its anchor (engine.anchors.edges, version 5 of the anchors): the head, the anchor's first
-- letters where it ends the string, and the tail, its last where it begins it, each a row of its own, so that a text
-- that glues a letter or a digit to it finds it as a vault holding it does (Ruling T10h). Their word is a keyed hash
-- under the label of its string's anchor (a value's or a fake's), as an anchor's.
ALTER TABLE vault_words DROP CONSTRAINT vault_words_role_check;
ALTER TABLE vault_words ADD CONSTRAINT vault_words_role_check CHECK (
    role IN ('original', 'surrogate', 'original-head', 'original-tail', 'surrogate-head', 'surrogate-tail')
);
