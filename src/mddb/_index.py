"""SQLite cache for an mddb directory: schema, open/rebuild, per-card field indexing."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

import yaml

from .card import Card

SCHEMA_VERSION = "3"
_SCHEMA = (Path(__file__).parent / "schema.sql").read_text()


def cache_path(root: Path) -> Path:
    """Return the SQLite cache path for the mddb at ``root``.

    Combines ``$XDG_CACHE_HOME`` (or ``~/.cache``) with the SHA1 of ``root``'s
    absolute path so each mddb directory gets its own cache.
    """
    digest = hashlib.sha1(str(root.resolve()).encode()).hexdigest()
    base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return base / "mddb" / digest / "index.sqlite"


def open_index(root: Path) -> sqlite3.Connection:
    """Open the cache for ``root`` or rebuild it from disk if the schema version mismatches.

    The version-match fast path returns immediately; any other state (missing
    cache, schema mismatch, missing ``meta.schema_version`` row) falls through
    to :func:`rebuild_index`. Other SQLite errors (corruption, unreadable
    file) propagate.

    Args:
        root: Absolute path to the mddb directory.

    Returns:
        A live ``sqlite3.Connection`` with foreign keys enabled.
    """
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
    """Delete any existing cache and build a fresh one from the ``.md`` files under ``root``.

    Walks the directory in sorted order, parses every card via
    :meth:`Card.from_file`, and inserts an ``entries`` row plus
    ``entry_fields`` rows for each. Files under ``.git/`` are skipped.

    Args:
        root: Absolute path to the mddb directory.

    Returns:
        A live ``sqlite3.Connection`` to the new cache.

    Raises:
        ValueError: A ``.md`` file has malformed frontmatter.
        sqlite3.IntegrityError: Two cards share the same ``id``.
    """
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
            blob = blob_on_disk(md_path)
            blob_relpath = str(blob.relative_to(root)) if blob else None
            insert(
                conn,
                Card.from_file(md_path),
                str(md_path.relative_to(root)),
                blob_relpath,
            )
    return conn


def relpath_of(conn: sqlite3.Connection, card_id: str) -> str:
    """Return the cached relpath for ``card_id``.

    Raises:
        KeyError: ``card_id`` is not in the cache.
    """
    row = conn.execute(
        "SELECT relpath FROM entries WHERE id = ?", (card_id,)
    ).fetchone()
    if row is None:
        raise KeyError(card_id)
    return row[0]


def blob_on_disk(
    card_abs_path: Path, ignore: frozenset[Path] = frozenset()
) -> Path | None:
    """Return the absolute path of the card's blob, or ``None``.

    The blob is the single sibling of ``card_abs_path`` whose suffix is
    neither ``""`` nor ``.md`` and whose stem equals the card's stem,
    excluding any path in ``ignore`` (used to filter staged-deleted blobs
    during a batch). Returns ``None`` if the parent directory does not exist
    (a card staged into a new subdir has no sibling blob).

    Raises:
        ValueError: more than one qualifying file remains — drift the cache
            cannot represent.
    """
    parent = card_abs_path.parent
    if not parent.is_dir():
        return None
    stem = card_abs_path.stem
    hits = [
        p
        for p in parent.iterdir()
        if p.is_file()
        and p not in ignore
        and p.suffix not in ("", ".md")
        and p.stem == stem
    ]
    if len(hits) > 1:
        raise ValueError(
            f"multiple blobs for {card_abs_path.name}: {sorted(p.name for p in hits)}"
        )
    return hits[0] if hits else None


def insert(
    conn: sqlite3.Connection,
    card: Card,
    relpath: str,
    blob_relpath: str | None = None,
) -> None:
    """Cache a new card. Caller must already have written the file + committed.

    ``blob_relpath`` is the relpath of the card's blob, or ``None`` when it
    has none (a real state, not an omitted argument).
    """
    cur = conn.execute(
        "INSERT INTO entries(id, relpath, title, summary, blob_relpath, yaml_text, body) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            card.id,
            relpath,
            card.yaml.get("title"),
            card.yaml.get("summary"),
            blob_relpath,
            _yaml_text(card),
            card.body,
        ),
    )
    index_fields(conn, cur.lastrowid, card.yaml)


def update_content(conn: sqlite3.Connection, card: Card) -> None:
    """Refresh title/summary/yaml_text/body and rebuild entry_fields for ``card``."""
    rowid = conn.execute(
        "SELECT rowid FROM entries WHERE id = ?", (card.id,)
    ).fetchone()[0]
    conn.execute(
        "UPDATE entries SET title = ?, summary = ?, yaml_text = ?, body = ? "
        "WHERE rowid = ?",
        (
            card.yaml.get("title"),
            card.yaml.get("summary"),
            _yaml_text(card),
            card.body,
            rowid,
        ),
    )
    conn.execute("DELETE FROM entry_fields WHERE entry_rowid = ?", (rowid,))
    index_fields(conn, rowid, card.yaml)


def update_paths(
    conn: sqlite3.Connection, card_id: str, relpath: str, blob_relpath: str | None
) -> None:
    """Point the cache at a new on-disk relpath and blob for ``card_id``.

    ``blob_relpath`` is the relpath of the card's blob at its (possibly new)
    location, or ``None`` when it has none — always the computed truth, so a
    move/update never leaves a stale blob path.
    """
    conn.execute(
        "UPDATE entries SET relpath = ?, blob_relpath = ? WHERE id = ?",
        (relpath, blob_relpath, card_id),
    )


def delete(conn: sqlite3.Connection, card_id: str) -> None:
    """Drop ``card_id`` from the cache. entry_fields cascade via foreign key."""
    conn.execute("DELETE FROM entries WHERE id = ?", (card_id,))


def list_progressive(conn: sqlite3.Connection) -> list[dict]:
    """Return ``[{id, title, summary, blob_relpath}, ...]`` for every cached card."""
    rows = conn.execute(
        "SELECT id, title, summary, blob_relpath FROM entries"
    ).fetchall()
    return [
        {"id": cid, "title": title, "summary": summary, "blob_relpath": blob_relpath}
        for cid, title, summary, blob_relpath in rows
    ]


def _yaml_text(card: Card) -> str:
    return yaml.safe_dump(card.yaml, sort_keys=False, allow_unicode=True)


def index_fields(conn: sqlite3.Connection, rowid: int, data: dict) -> None:
    """Insert ``entry_fields`` rows for the top-level scalar and list-of-scalar values in ``data``.

    Nested dicts and dict/list items inside lists are intentionally not
    indexed — they remain in ``entries.yaml_text`` for FTS search but are
    not queryable via ``entry_fields``. Callers wanting nested-path queries
    should project the nested data up to top-level keys before insertion.

    Args:
        conn: Live SQLite connection.
        rowid: The ``entries.rowid`` of the parent row.
        data: The card's YAML frontmatter as a Python dict.
    """
    rows = []
    for key, value in data.items():
        if key in ("title", "summary"):
            continue
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
