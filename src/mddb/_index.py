"""SQLite cache for an mddb directory: schema, open/rebuild, per-card field indexing."""

from __future__ import annotations

import fcntl
import hashlib
import os
import sqlite3
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .card import Card

SCHEMA_VERSION = "5"
_SCHEMA = (Path(__file__).parent / "schema.sql").read_text()

SCHEMA_DOC = """\
entries(rowid, id, relpath, title, summary, blob_relpath, first_commit, yaml_text, body)
  one row per card. id/relpath UNIQUE NOT NULL; title/summary/blob_relpath nullable.
  first_commit is the author date of the first commit in the card's surviving
  lineage (renames followed), canonical UTC ISO 'YYYY-MM-DDTHH:MM:SS+00:00' —
  lexicographic order is chronological order. Derived from the deck's git
  history at rebuild; never stored in the card's YAML.
  yaml_text is the serialised frontmatter; body is the markdown body.
entry_fields(entry_rowid -> entries.rowid, key, value_str, value_num)
  one row per top-level scalar (and per item of a list-of-scalars) in a card's
  yaml, EXCEPT title/summary (those are columns on entries). Numeric values also
  land in value_num. This is where tags live (key='tags', one row per tag).
entries_fts(yaml_text, body)
  FTS5 full-text index over entries; query as:
  SELECT id FROM entries WHERE rowid IN
    (SELECT rowid FROM entries_fts WHERE entries_fts MATCH ?)
  MATCH is FTS5 syntax: phrases need quotes ("foo bar"), prefix is foo*,
  and punctuation is operators (wind-up parses as wind NOT up) — bind the
  user's words as a quoted phrase, or strip punctuation, before matching."""


def open_index_readonly(root: Path) -> sqlite3.Connection:
    """Open the cache at ``root`` read-only (``mode=ro``) for raw SQL queries.

    ``mode=ro`` is what stops query SQL from writing the cache file. Open the
    deck (:func:`open_index`) first so the cache exists and is current — this
    opener does not rebuild.

    Args:
        root: Absolute path to the mddb directory.

    Returns:
        A read-only ``sqlite3.Connection``.
    """
    return sqlite3.connect(f"file:{cache_path(root)}?mode=ro", uri=True)


def cache_path(root: Path) -> Path:
    """Return the SQLite cache path for the mddb at ``root``.

    Combines ``$XDG_CACHE_HOME`` (or ``~/.cache``) with the SHA1 of ``root``'s
    absolute path so each mddb directory gets its own cache.
    """
    digest = hashlib.sha1(str(root.resolve()).encode()).hexdigest()
    base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return base / "mddb" / digest / "index.sqlite"


@contextmanager
def deck_lock(root: Path):
    """Serialise mddb writers (and stale-cache rebuilds) for the deck at ``root``.

    An advisory ``fcntl.flock`` on ``<root>/.git/mddb.lock`` — held across a
    commit's materialise and across a rebuild-on-mismatch. Only mddb processes
    take it, so it never collides with git's own ``index.lock``; the OS releases
    it on process death.
    """
    with open(root / ".git" / "mddb.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def git_head(conn: sqlite3.Connection) -> str:
    """Return the git HEAD the cache reflects (``meta.git_head``), or ``""`` if unset."""
    row = conn.execute("SELECT value FROM meta WHERE key='git_head'").fetchone()
    return row[0] if row else ""


def set_git_head(conn: sqlite3.Connection, sha: str) -> None:
    """Record the git HEAD the cache now reflects. Caller wraps in ``with conn:``."""
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('git_head', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (sha,),
    )


def analyze(conn: sqlite3.Connection) -> None:
    """Refresh the planner statistics the cache's join indexes rely on.

    Without ``sqlite_stat1`` the planner mis-drives range-predicate joins over
    ``entry_fields`` (measured 118ms vs 0.1ms on a 9,813-card deck). Runs
    after every rebuild AND every editor commit — a deck born via
    :meth:`MDDB.init` and grown incrementally never rebuilds, so commit-time
    refresh is what keeps its statistics existing and current. Caller wraps
    in ``with conn:``.
    """
    conn.execute("ANALYZE")


def open_index(root: Path, head: str = "") -> sqlite3.Connection:
    """Open the cache for ``root``, rebuilding it if the schema version or git HEAD drifted.

    The whole decision — fast-path probe and any rebuild (missing cache,
    schema mismatch, git-HEAD mismatch) — runs under :func:`deck_lock`, so a
    concurrent opener never probes or unlinks the shared cache while another
    is mid-rebuild (which surfaced as SQLITE_READONLY / disk I/O errors on the
    loser's connection). The flock is uncontended in the common case and
    released as soon as the connection is returned. ``head == ""`` (no commits
    yet) is the :meth:`MDDB.init` bootstrap, which constructs the instance
    before ``git init`` — no ``.git`` to lock, and single-process by the
    two-explicit-entry-points contract, so it opens unlocked.

    Args:
        root: Absolute path to the mddb directory.
        head: The current git HEAD sha, or ``""`` when HEAD does not resolve.

    Returns:
        A live ``sqlite3.Connection`` with foreign keys enabled.
    """
    if not head:
        conn = _open_fresh(root, head)
        return conn if conn is not None else _rebuild_at(root, head)
    with deck_lock(root):
        conn = _open_fresh(root, head)
        if conn is not None:
            return conn
        return _rebuild_at(root, head)


def _open_fresh(root: Path, head: str) -> sqlite3.Connection | None:
    """The fast path: the existing cache, iff schema and git HEAD both match."""
    db_path = cache_path(root)
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row and row[0] == SCHEMA_VERSION and (not head or git_head(conn) == head):
        return conn
    conn.close()
    return None


def _rebuild_at(root: Path, head: str) -> sqlite3.Connection:
    conn = rebuild_index(root)
    if head:
        with conn:
            set_git_head(conn, head)
    return conn


def rebuild_index(root: Path) -> sqlite3.Connection:
    """Delete any existing cache and build a fresh one from the ``.md`` files under ``root``.

    Walks the directory in sorted order, parses every card via
    :meth:`Card.from_file`, and inserts an ``entries`` row plus
    ``entry_fields`` rows for each, with ``first_commit`` derived from the
    deck's git history (:func:`first_commits`). Files under ``.git/`` are
    skipped.

    Args:
        root: Absolute path to the mddb directory.

    Returns:
        A live ``sqlite3.Connection`` to the new cache.

    Raises:
        ValueError: A ``.md`` file has malformed frontmatter, or a tracked
            card has no git lineage (an uncommitted file is out of contract).
        sqlite3.IntegrityError: Two cards share the same ``id``.
    """
    db_path = cache_path(root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    md_paths = [
        p for p in sorted(root.rglob("*.md")) if ".git" not in p.relative_to(root).parts
    ]
    relpaths = [str(p.relative_to(root)) for p in md_paths]
    lineage = first_commits(root, relpaths)
    blobs_by_stem: dict[Path, dict[str, list[Path]]] = {}
    for parent in {p.parent for p in md_paths}:
        stems: dict[str, list[Path]] = {}
        for sibling in parent.iterdir():
            if sibling.is_file() and sibling.suffix not in ("", ".md"):
                stems.setdefault(sibling.stem, []).append(sibling)
        blobs_by_stem[parent] = stems
    with conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        for md_path, relpath in zip(md_paths, relpaths):
            hits = blobs_by_stem[md_path.parent].get(md_path.stem, [])
            if len(hits) > 1:
                raise ValueError(
                    f"multiple blobs for {md_path.name}: {sorted(p.name for p in hits)}"
                )
            blob = hits[0] if hits else None
            blob_relpath = str(blob.relative_to(root)) if blob else None
            insert(
                conn,
                Card.from_file(md_path),
                relpath,
                lineage[relpath],
                blob_relpath,
            )
        analyze(conn)
    return conn


def utc_iso(git_date: str) -> str:
    """Normalise a git ``%aI`` author date to canonical UTC ISO.

    The canonical form is ``YYYY-MM-DDTHH:MM:SS+00:00``, so lexicographic
    order equals chronological order for every consumer.

    Args:
        git_date: An ISO-8601 date string as emitted by ``%aI`` (any offset).

    Returns:
        The same instant rendered in UTC.
    """
    return datetime.fromisoformat(git_date).astimezone(timezone.utc).isoformat()


def first_commits(root: Path, relpaths: list[str]) -> dict[str, str]:
    """Return each relpath's lineage-origin author date, canonical UTC ISO.

    Two tiers, correct on arbitrary merge DAGs (decks sync by merge, so a
    linearised-log replay is unsound — a side-branch delete not selected by a
    merge would poison a global path map):

    1. One full-DAG ``git log --name-status`` pass collects, per path, every
       ``A`` date plus flags for any ``D``/``R`` involvement. A path with
       exactly one ``A`` ever and no ``D``/``R`` involvement is unambiguous
       regardless of topology — its single origin commit is its lineage
       start. This covers the overwhelming majority of cards.
    2. Every other requested path (re-adds, renames, D-touched, multi-A)
       resolves exactly via ``git log --follow --diff-filter=A``, taking the
       most recent add — the surviving lineage's origin.

    A deck with no ``.git`` or no commits yields an empty map (the
    :meth:`MDDB.init` bootstrap).

    Args:
        root: Absolute path to the mddb directory.
        relpaths: The HEAD card relpaths needing lineage dates.

    Returns:
        ``{relpath: utc_iso_date}`` covering every requested relpath.

    Raises:
        ValueError: A requested path has no git lineage (uncommitted file —
            out of contract), or the log stream contains an unknown status.
    """
    if not (root / ".git").is_dir():
        return {}
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    if head.returncode != 0:
        return {}
    log = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "log",
            "--name-status",
            "-M",
            "-z",
            "--pretty=format:%x01%H%x00%aI%x00",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    adds: dict[str, list[str]] = {}
    touched: set[str] = set()
    for chunk in log.split("\x01"):
        tokens = [t for t in chunk.split("\x00") if t]
        if not tokens:
            continue
        date = tokens[1]
        i = 2
        while i < len(tokens):
            status = tokens[i].lstrip("\n")
            if not status:
                i += 1
                continue
            if status == "A":
                adds.setdefault(tokens[i + 1], []).append(date)
                i += 2
            elif status in ("M", "D"):
                if status == "D":
                    touched.add(tokens[i + 1])
                i += 2
            elif status.startswith("R"):
                touched.add(tokens[i + 1])
                touched.add(tokens[i + 2])
                i += 3
            else:
                raise ValueError(f"unknown git log status token: {status!r}")
    lineage = {}
    for relpath in relpaths:
        dates = adds.get(relpath, [])
        if len(dates) == 1 and relpath not in touched:
            lineage[relpath] = utc_iso(dates[0])
            continue
        follow = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "log",
                "--follow",
                "--diff-filter=A",
                "--format=%aI",
                "--",
                relpath,
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        if not follow:
            raise ValueError(f"no git lineage for {relpath}")
        lineage[relpath] = utc_iso(follow[0])
    return lineage


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
    first_commit: str,
    blob_relpath: str | None = None,
) -> None:
    """Cache a new card. Caller must already have written the file + committed.

    ``first_commit`` is the card's lineage-origin author date in canonical
    UTC ISO (see :func:`first_commits`); the schema rejects a missing value.
    ``blob_relpath`` is the relpath of the card's blob, or ``None`` when it
    has none (a real state, not an omitted argument).
    """
    cur = conn.execute(
        "INSERT INTO entries(id, relpath, title, summary, blob_relpath, first_commit, yaml_text, body) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            card.id,
            relpath,
            card.yaml.get("title"),
            card.yaml.get("summary"),
            blob_relpath,
            first_commit,
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
