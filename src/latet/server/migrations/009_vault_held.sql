-- An API key's secret that no word of a text finds glued to letters or digits (engine.stored.held_glued: one run of 16
-- letters or fewer, or an anchor of six or fewer at its edge) is held in memory for good by its space, the newest of
-- them alone (key_held_secrets, Ruling 10b-R3): it is kept with a row of the role `held` (its word NULL), beside its
-- anchor and its edges, so that a space loads the newest of them apart from the values without anchor (version 9 of the
-- anchors). Edges are hashed under labels of their own since version 8.
ALTER TABLE vault_words DROP CONSTRAINT vault_words_role_check;
ALTER TABLE vault_words ADD CONSTRAINT vault_words_role_check CHECK (
    role IN ('original', 'surrogate', 'original-head', 'original-tail', 'surrogate-head', 'surrogate-tail', 'held')
);
