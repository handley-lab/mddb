from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

import yaml

from .card import Card

SCHEMA_VERSION = "1"
_SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")


def cache_path(root: Path) -> Path:
    digest = hashlib.sha1(str(root.resolve()).encode("utf-8")).hexdigest()
    base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return base / "mddb" / digest / "index.sqlite"


def open_index(root: Path) -> sqlite3.Connection:
    db_path = cache_path(root)
    if db_path.exists():
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if row and row[0] == SCHEMA_VERSION:
            return conn
        conn.close()
        db_path.unlink()
    return rebuild_index(root)


def rebuild_index(root: Path) -> sqlite3.Connection:
    db_path = cache_path(root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    with conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        for md_path in sorted(root.rglob("*.md")):
            if ".git" in md_path.relative_to(root).parts:
                continue
            card = Card.from_file(md_path)
            yaml_text = yaml.safe_dump(card.yaml, sort_keys=False, allow_unicode=True)
            cur = conn.execute(
                "INSERT INTO entries(id, relpath, yaml_text, body) VALUES (?, ?, ?, ?)",
                (card.id, str(md_path.relative_to(root)), yaml_text, card.body),
            )
            index_fields(conn, cur.lastrowid, card.yaml)
    return conn


def index_fields(conn: sqlite3.Connection, rowid: int, data: dict) -> None:
    rows = []
    for key, value in data.items():
        if isinstance(value, dict):
            continue
        if isinstance(value, list):
            for item in value:
                if not isinstance(item, (dict, list)):
                    rows.append(_row(rowid, key, item))
        else:
            rows.append(_row(rowid, key, value))
    conn.executemany(
        "INSERT INTO entry_fields(entry_rowid, key, value_str, value_num) VALUES (?, ?, ?, ?)",
        rows,
    )


def _row(rowid: int, key: str, value) -> tuple:
    num = (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )
    return (rowid, key, str(value), num)
