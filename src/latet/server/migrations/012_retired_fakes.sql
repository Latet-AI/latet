-- A fake its entry no longer has (its value forgotten, by itself or by its entry, or its fake given anew) is retired,
-- never given to another value (the final security review's H2: a forget freed the fake's string, a new value drew it
-- again, and an unmask of a text masked before gave that other value). Its string's row stays, and so does the row of
-- how it reads: of the role `retired` and of no entry (entity_id NULL), its keyed hash alone, as every row of the table
-- is, for as long as the identity's vault (R7), so that a fake drawn equal to it is refused, whichever instance draws
-- it, and the vault draws again. A string that is another entry's value as well names that entry in `also`, as before.
-- Fakes freed before this migration are not known, and are not retired.
ALTER TABLE vault_strings DROP CONSTRAINT vault_strings_role_check;
ALTER TABLE vault_strings ADD CONSTRAINT vault_strings_role_check CHECK (role IN ('original', 'surrogate', 'retired'));
ALTER TABLE vault_strings ALTER COLUMN entity_id DROP NOT NULL;
ALTER TABLE vault_strings ADD CONSTRAINT vault_strings_retired_check CHECK ((role = 'retired') = (entity_id IS NULL));
-- They take no room in the vault (its limit counts values), but they stay: the status and latet_vault_near_limit weigh
-- them with the values (latet.server.vault.vault_usage), counted through this index rather than the whole table.
CREATE INDEX vault_strings_retired ON vault_strings (identity_id) WHERE role = 'retired';
