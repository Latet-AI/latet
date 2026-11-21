-- E3: groups and their members, roles, the control of API keys, accounts at the identity provider, SCIM's tokens.
-- A subject (roles, key_controllers) is a person ("u_…") or a group ("g_…").
CREATE TABLE groups (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    external_id TEXT,
    source TEXT NOT NULL CHECK (source IN ('local', 'scim', 'oidc')),
    created DOUBLE PRECISION NOT NULL,
    modified DOUBLE PRECISION NOT NULL
);
CREATE UNIQUE INDEX groups_name ON groups (lower(name));
CREATE UNIQUE INDEX groups_external ON groups (source, external_id) WHERE external_id IS NOT NULL;

CREATE TABLE members (
    group_id TEXT NOT NULL REFERENCES groups (id) ON DELETE CASCADE,
    identity_id TEXT NOT NULL REFERENCES identities (id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (source IN ('local', 'scim', 'oidc')),
    PRIMARY KEY (group_id, identity_id, source)
);
CREATE INDEX members_identity ON members (identity_id);

CREATE TABLE roles (
    subject TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('group-lead', 'security', 'auditor', 'admin')),
    PRIMARY KEY (subject, role)
);

CREATE TABLE key_controllers (
    key_id TEXT NOT NULL REFERENCES identities (id) ON DELETE CASCADE,
    subject TEXT NOT NULL,
    PRIMARY KEY (key_id, subject)
);
CREATE INDEX key_controllers_subject ON key_controllers (subject);

-- scim_managed: SCIM made the account, or an administrator handed the person to it (`latet server people link … --scim`):
-- SCIM sees and changes no one else. linked_by: who named the account's user name for a first sign-in to find, SCIM
-- ('scim') or an administrator ('admin'); NULL once someone signed in (a subject stays), or the user name was changed
-- otherwise: only such an account is claimable. disabled_by: who disabled the person last, an administrator ('local')
-- or SCIM ('scim'), which lifts only a disable of its own.
CREATE TABLE accounts (
    identity_id TEXT PRIMARY KEY REFERENCES identities (id) ON DELETE CASCADE,
    user_name TEXT,
    subject TEXT UNIQUE,
    external_id TEXT UNIQUE,
    scim JSONB,
    deleted BOOLEAN NOT NULL DEFAULT FALSE,
    claimed JSONB NOT NULL DEFAULT '[]',
    modified DOUBLE PRECISION NOT NULL,
    scim_managed BOOLEAN NOT NULL DEFAULT FALSE,
    linked_by TEXT CHECK (linked_by IN ('scim', 'admin')),
    disabled_by TEXT CHECK (disabled_by IN ('local', 'scim'))
);
-- A user name is unique with A to Z's case aside and nothing else folded (the C collation's lower): never the
-- database's locale, which folds look-alikes onto ASCII letters (the Kelvin sign onto k), so that a name written with
-- one is another person's. Every query that finds an account by its user name compares it so (ee/people.py).
CREATE UNIQUE INDEX accounts_user_name ON accounts (lower(user_name COLLATE "C")) WHERE user_name IS NOT NULL;

-- A person removed takes the roles given to them and their control of API keys along (their memberships and their
-- account go by their foreign keys): in the removal's own transaction, whoever removes them. Its search path is the
-- schema's, as the migration's is.
CREATE FUNCTION people_identity_removed() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
BEGIN
    DELETE FROM roles WHERE subject = OLD.id;
    DELETE FROM key_controllers WHERE subject = OLD.id;
    RETURN OLD;
END
$$;
CREATE TRIGGER people_identity_removed AFTER DELETE ON identities
    FOR EACH ROW EXECUTE FUNCTION people_identity_removed();

-- SCIM's bearer tokens (R10): kept by their keyed hash, each row with a keyed tag over its id, hash and end, which a row
-- this server's key did not make (or whose end was moved) fails. made_by: whom the administration log names for it.
CREATE TABLE scim_tokens (
    id TEXT PRIMARY KEY,
    hash TEXT NOT NULL UNIQUE,
    tag TEXT NOT NULL,
    created DOUBLE PRECISION NOT NULL,
    expires DOUBLE PRECISION,
    last_used DOUBLE PRECISION,
    made_by TEXT NOT NULL
);
