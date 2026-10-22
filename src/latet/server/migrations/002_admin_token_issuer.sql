-- What issued each administration token (E0c): the id of the token that issued it, or `server` for one that
-- `latet server init` made on the server's machine; NULL for the tokens issued before this column, and for those a
-- program of the server's own issued without saying. It is not in the row's tag: whoever writes the database can change
-- it, as the label, and what the administration log recorded of the issue (`admin-token.issued`, its actor) is what
-- holds, while the log keeps it.
ALTER TABLE admin_tokens ADD COLUMN issued_by TEXT;
