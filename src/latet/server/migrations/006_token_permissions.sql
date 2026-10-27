-- What a token may do (latet.server.grants): NULL for all three, as every token made before 0.10.0; otherwise the
-- sorted names. A row with permissions is tagged under its own label, over them too (identities._GRANT_TAG).
ALTER TABLE tokens ADD COLUMN permissions TEXT
    CHECK (permissions IS NULL OR permissions IN ('scan', 'unmask', 'scan,unmask', 'mask,scan'));
