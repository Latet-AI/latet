-- How a vault's value was found (0.9.10): TRUE where the text's escapes read as what they stand for (`Mario%20Rossi`
-- found as `Mario Rossi`, the decoded reading), FALSE as it is written. The reading of its fake (`Nicola Greco` for
-- `Nicola%20Greco`), which keeps that reading apart from every other value and gives it back as the value reads, is
-- made for the first alone. NULL for an entry kept before this column, or by an instance that names the columns it
-- writes as 0.9.9's did: its reading is made by 0.9.9's rule, from how the value is written. In clear, as the entry's
-- rule is, and bound to nothing: whoever writes the database without the master key can make an entry's reading come
-- and go, a restore of its fake's reading, never a value in clear.
ALTER TABLE vault_entities ADD COLUMN decoded BOOLEAN;
