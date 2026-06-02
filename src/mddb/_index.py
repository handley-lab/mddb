from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

import yaml

from .card import Card

SCHEMA_VERSION = "1"

_SCHEMA = """
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
    INSERT INTO entries_fts(rowid, yaml_text, body) VALUES (new.rowid, new.yaml_text, new.body);
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
"""


def cache_path(root: Path) -> Path:
    digest = hashlib.sha1(str(root.resolve()).encode("utf-8")).hexdigest()
    base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return base / "mddb" / digest / "index.sqlite"


def open_index(root: Path) -> sqlite3.Connection:
    db_path = cache_path(root)
    if db_path.exists():
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row and row[0] == SCHEMA_VERSION:
            return conn
        conn.close()
        db_path.unlink()
    return rebuild_index(root)


def rebuild_index(root: Path) -> sqlite3.Connection:
    db_path = cache_path(root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
    for md_path in sorted(root.rglob("*.md")):
        if ".git" in md_path.relative_to(root).parts:
            continue
        card = Card.from_file(md_path)
        relpath = str(md_path.relative_to(root))
        _insert(conn, card, relpath)
    conn.commit()
    return conn


def insert_card(conn: sqlite3.Connection, card: Card, relpath: str) -> None:
    _insert(conn, card, relpath)
    conn.commit()


def update_card(conn: sqlite3.Connection, card: Card, relpath: str) -> None:
    yaml_text = yaml.safe_dump(card.yaml, sort_keys=False, allow_unicode=True, default_flow_style=False)
    rowid = conn.execute("SELECT rowid FROM entries WHERE id = ?", (card.id,)).fetchone()[0]
    conn.execute(
        "UPDATE entries SET relpath = ?, yaml_text = ?, body = ? WHERE rowid = ?",
        (relpath, yaml_text, card.body, rowid),
    )
    conn.execute("DELETE FROM entry_fields WHERE entry_rowid = ?", (rowid,))
    _insert_fields(conn, rowid, card.yaml)
    conn.commit()


def delete_card(conn: sqlite3.Connection, card_id: str) -> None:
    conn.execute("DELETE FROM entries WHERE id = ?", (card_id,))
    conn.commit()


def move_card(conn: sqlite3.Connection, card_id: str, new_relpath: str) -> None:
    conn.execute("UPDATE entries SET relpath = ? WHERE id = ?", (new_relpath, card_id))
    conn.commit()


def lookup_relpath(conn: sqlite3.Connection, card_id: str) -> str:
    row = conn.execute("SELECT relpath FROM entries WHERE id = ?", (card_id,)).fetchone()
    if row is None:
        raise KeyError(card_id)
    return row[0]


def _insert(conn: sqlite3.Connection, card: Card, relpath: str) -> None:
    yaml_text = yaml.safe_dump(card.yaml, sort_keys=False, allow_unicode=True, default_flow_style=False)
    cur = conn.execute(
        "INSERT INTO entries(id, relpath, yaml_text, body) VALUES (?, ?, ?, ?)",
        (card.id, relpath, yaml_text, card.body),
    )
    _insert_fields(conn, cur.lastrowid, card.yaml)


def _insert_fields(conn: sqlite3.Connection, rowid: int, data: dict) -> None:
    for key, value in data.items():
        if isinstance(value, dict):
            continue
        if isinstance(value, list):
            for item in value:
                if not isinstance(item, (dict, list)):
                    _insert_field(conn, rowid, key, item)
        else:
            _insert_field(conn, rowid, key, value)


def _insert_field(conn: sqlite3.Connection, rowid: int, key: str, value) -> None:
    num = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    conn.execute(
        "INSERT INTO entry_fields(entry_rowid, key, value_str, value_num) VALUES (?, ?, ?, ?)",
        (rowid, key, str(value), num),
    )
