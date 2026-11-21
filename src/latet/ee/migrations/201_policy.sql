-- Locked rules (Latet Enterprise, E2): the server's policy, one row. Its text holds values (the company's terms,
-- values never to mask), so it is sealed with the master key, its associated data naming the row's etag, when it
-- was set and by whom ("ee_policy:<etag>:<set_at>:<set_by>"): a sealed text moved to another row, or the row's etag,
-- time or author changed, does not open. The rest names no value.
CREATE TABLE ee_policy (
    id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    etag text NOT NULL,
    sealed bytea NOT NULL,
    set_at double precision NOT NULL,
    set_by text NOT NULL
);
