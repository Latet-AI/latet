-- What a server's administration log is told once, whichever instance starts first with the release that brings it,
-- and never again whatever restarts follow: each note by its name, and when it was written (in the transaction that
-- writes its entry of the log, so that both are kept or neither). Latet 0.14.0's first: a request member no request
-- profile names is read and checked by default (`unknown_members: check`), which every server moves to with no
-- approval, so that an administrator who relied on the refusal finds when it changed.
CREATE TABLE server_notes (
    name TEXT PRIMARY KEY,
    written DOUBLE PRECISION NOT NULL
);
