-- A computer's key (E0c, R16): the second secret a computer gets at enrolment with its token, kept by its CLI alone
-- (the system's credential store, or the private token file), never by an agent. The server keeps its keyed hash, an
-- HMAC under the credentials' key over the token's id and the key (`identities._COMPUTER`): bound to its own row, so
-- that a hash copied onto another token's row matches no key; and asks for the key on a sign-in link to the dashboard
-- and on a change of the person's identity, which the token alone no longer makes. NULL for a token made before this
-- column (its computer connects again to get one) and for an API key, which has no computer. The row's tag does not
-- cover it: whoever writes the database without the master key can make no hash that holds, only take one away.
ALTER TABLE tokens ADD COLUMN computer_key TEXT CHECK (computer_key IS NULL OR computer_key ~ '^[0-9a-f]{64}$');
