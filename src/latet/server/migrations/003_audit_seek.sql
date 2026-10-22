-- HTTP log pagination seeks in time/id order. Existing id-ordered chain queries keep the primary key.
CREATE INDEX audit_ts_id ON audit (ts DESC, id DESC);
