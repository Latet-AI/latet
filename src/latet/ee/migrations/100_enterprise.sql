-- Latet Enterprise Edition (E1): the server's license, its Enterprise settings and their secrets. Unqualified names,
-- as the server's own migrations: the schema is the connection's search path.

-- The license as the licensor made it: one row at most. document is NULL once the license is removed, and the row
-- keeps over_since (since when more users than bought are counted), so that removing the license and installing it
-- again starts no new grace, and trials_ended (the ids of the trials whose end this server weighed, kept when
-- PostgreSQL's own clock agreed), so that a clock moved back revives none and an ended trial's file is not installed
-- again. features are those the installing instance verified, written with the document and NULL with it: an instance
-- that cannot verify the document (a key of a newer release, during a rolling upgrade) protects as they say. version
-- grows with each change of the license: an instance drops its copy when it changes.
CREATE TABLE ee_license (
    id smallint PRIMARY KEY CHECK (id = 1),
    document bytea,
    features text[],
    version bigint NOT NULL,
    installed double precision NOT NULL,
    over_since double precision,
    trials_ended text[] NOT NULL DEFAULT '{}'
);

-- A row for each section an administrator set; the others have their defaults.
CREATE TABLE ee_settings (
    section text PRIMARY KEY,
    value jsonb NOT NULL,
    changed double precision NOT NULL,
    changed_by text NOT NULL
);

-- The secrets the settings name, sealed with the master key: never in clear here.
CREATE TABLE ee_secrets (
    name text PRIMARY KEY,
    sealed bytea NOT NULL,
    changed double precision NOT NULL
);
