CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE entries(
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE NOT NULL,
    relpath TEXT UNIQUE NOT NULL,
    yaml_text TEXT NOT NULL,
    body TEXT NOT NULL
);

CREATE TABLE entry_fields(
    entry_rowid INTEGER NOT NULL REFERENCES entries(rowid) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value_str TEXT,
    value_num REAL
);

CREATE INDEX entry_fields_key_str ON entry_fields(key, value_str);
CREATE INDEX entry_fields_key_num ON entry_fields(key, value_num);

CREATE VIRTUAL TABLE entries_fts USING fts5(
    yaml_text, body, content='entries', content_rowid='rowid'
);

CREATE TRIGGER entries_ai AFTER INSERT ON entries BEGIN
    INSERT INTO entries_fts(rowid, yaml_text, body)
        VALUES (new.rowid, new.yaml_text, new.body);
END;

CREATE TRIGGER entries_ad AFTER DELETE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, yaml_text, body)
        VALUES ('delete', old.rowid, old.yaml_text, old.body);
END;

CREATE TRIGGER entries_au AFTER UPDATE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, yaml_text, body)
        VALUES ('delete', old.rowid, old.yaml_text, old.body);
    INSERT INTO entries_fts(rowid, yaml_text, body)
        VALUES (new.rowid, new.yaml_text, new.body);
END;
