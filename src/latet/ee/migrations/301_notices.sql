-- E3, O6: up to which entry of the administration log a person said they saw what their dashboard told them: the
-- computers connected as them (E0c's token.added) and the one-time codes made for them (E3's person.code.made, E0c's
-- code.issued), by an administrator or by themselves. /me lists the entries after it, for 14 days after each happened,
-- and writes nothing; a session the company sign-in opened says them seen (POST /_latet/api/v1/ee/notices/told). The
-- log's ids follow the order its entries were committed in (its chain is written one entry at a time, under a lock held
-- to the commit). The person's list of their computers (their tokens, always) finds there too who made the code each
-- was connected with: the last code made for them before its token.added, person.added's included (their first code).
CREATE TABLE notified (
    identity_id TEXT PRIMARY KEY REFERENCES identities (id) ON DELETE CASCADE,
    until BIGINT NOT NULL
);
CREATE INDEX audit_notices ON audit (target, id)
    WHERE action IN ('person.added', 'person.code.made', 'code.issued', 'token.added');
