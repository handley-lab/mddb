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

SCHEMA_VERSION = "7"
_SCHEMA = (Path(__file__).parent / "schema.sql").read_text()

SCHEMA_DOC = """\
entries(rowid, id, relpath, title, summary, kind, blob_relpath, first_commit, yaml_text, body)
  one row per card. id/relpath UNIQUE NOT NULL; title/summary/kind/blob_relpath
  nullable. kind names the vocabulary whose verbs own the card; NULL means no
  layer owns it.
  first_commit is the author date of the first commit in the card's surviving
  lineage (renames followed), canonical UTC ISO 'YYYY-MM-DDTHH:MM:SS+00:00' —
  lexicographic order is chronological order. Derived from the deck's git
  history at rebuild; never stored in the card's YAML.
  yaml_text is the serialised frontmatter; body is the markdown body.
entry_fields(entry_rowid -> entries.rowid, key, value_str, value_num)
  one row per top-level scalar (and per item of a list-of-scalars) in a card's
  yaml, EXCEPT title/summary/kind (those are columns on entries). Numeric values also
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

    An advisory ``fcntl.flock`` on the read-only ``<root>/.git`` directory — held across a
    commit's materialise and across a rebuild-on-mismatch. Only mddb processes
    take it, so it never collides with git's own ``index.lock``; the OS releases
    it on process death.
    """
    fd = os.open(root / ".git", os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


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
    """Open the cache for ``root``, refreshing it if only git HEAD drifted.

    The whole decision — fast-path probe and any rebuild (missing cache,
    schema mismatch, git-HEAD mismatch) — runs under :func:`deck_lock`, so a
    concurrent opener never probes or unlinks the shared cache while another
    is mid-rebuild (which surfaced as SQLITE_READONLY / disk I/O errors on the
    loser's connection). The flock is uncontended in the common case and
    released as soon as the connection is returned. ``head == ""`` (no commits
    yet) is the :meth:`MDDB.init` bootstrap: ``.git`` exists but has no HEAD,
    and the two-explicit-entry-points contract makes that bootstrap
    single-process, so it opens unlocked.

    Args:
        root: Absolute path to the mddb directory.
        head: The current git HEAD sha, or ``""`` when HEAD does not resolve.

    Returns:
        A live ``sqlite3.Connection`` with foreign keys enabled.
    """
    if not head:
        return _rebuild_at(root, head)
    with deck_lock(root):
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        conn = _open_cache(root)
        if conn is None:
            return _rebuild_at(root, head)
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        cached_head = git_head(conn)
        if row and row[0] == SCHEMA_VERSION and cached_head == head:
            return conn
        if row and row[0] == SCHEMA_VERSION and cached_head:
            exists = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "rev-parse",
                    "--verify",
                    f"{cached_head}^{{commit}}",
                ],
                capture_output=True,
                check=False,
            )
            if exists.returncode == 0:
                ancestor = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(root),
                        "merge-base",
                        "--is-ancestor",
                        cached_head,
                        head,
                    ],
                    capture_output=True,
                    check=False,
                )
                if ancestor.returncode == 0:
                    return refresh_index(root, conn, cached_head, head)
                if ancestor.returncode != 1:
                    ancestor.check_returncode()
        conn.close()
        return _rebuild_at(root, head)


def _open_cache(root: Path) -> sqlite3.Connection | None:
    """Open the existing cache, or return ``None`` when it is absent."""
    db_path = cache_path(root)
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _rebuild_at(root: Path, head: str) -> sqlite3.Connection:
    conn = rebuild_index(root, head)
    if head:
        with conn:
            set_git_head(conn, head)
    return conn


def rebuild_index(root: Path, head: str = "") -> sqlite3.Connection:
    """Delete the cache and rebuild it from Markdown paths tracked at ``head``.

    Enumerates paths and reads card bytes from the captured Git commit, then
    inserts an ``entries`` row plus
    ``entry_fields`` rows for each, with ``first_commit`` derived from the
    deck's git history (:func:`first_commits`). Files under ``.git/`` are
    skipped.

    Args:
        root: Absolute path to the mddb directory.

    Returns:
        A live ``sqlite3.Connection`` to the new cache.

    Raises:
        ValueError: A tracked ``.md`` file has malformed frontmatter or no Git
            lineage.
        sqlite3.IntegrityError: Two cards share the same ``id``.
    """
    db_path = cache_path(root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    if not head:
        resolved = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        head = resolved.stdout.strip() if resolved.returncode == 0 else ""
    tree = _tree(root, head) if head else {}
    relpaths = sorted(path for path in tree if path.endswith(".md"))
    cards = _cards_from_tree(root, tree, relpaths)
    lineage = first_commits(root, relpaths, head)
    with conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        for relpath in relpaths:
            card, blob_relpath = cards[relpath]
            insert(
                conn,
                card,
                relpath,
                lineage[relpath],
                blob_relpath,
            )
        analyze(conn)
    return conn


def refresh_index(
    root: Path, conn: sqlite3.Connection, cached_head: str, head: str
) -> sqlite3.Connection:
    """Refresh a current-schema cache across an ancestor HEAD advance."""
    changed = _changed_paths(root, cached_head, head)
    tree = _tree(root, head)
    cached_paths = {row[0] for row in conn.execute("SELECT relpath FROM entries")}
    candidates = {
        path if path.endswith(".md") else str(Path(path).with_suffix(".md"))
        for path in changed
        if Path(path).suffix
    }
    candidates &= cached_paths | {path for path in tree if path.endswith(".md")}
    current = sorted(
        path for path in candidates if path in tree and path.endswith(".md")
    )
    cards = _cards_from_tree(root, tree, current)
    lineage = _refresh_lineage(root, conn, cached_head, head, current)
    with conn:
        for relpath in candidates:
            conn.execute("DELETE FROM entries WHERE relpath = ?", (relpath,))
        for relpath in current:
            card, blob_relpath = cards[relpath]
            insert(conn, card, relpath, lineage[relpath], blob_relpath)
        analyze(conn)
        set_git_head(conn, head)
    return conn


def _tree(root: Path, head: str) -> dict[str, str]:
    """Return ``{tracked path: blob object}`` for ``head``."""
    raw = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "-r", "-z", head],
        check=True,
        capture_output=True,
    ).stdout
    tree = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, path = record.split(b"\t", 1)
        _mode, kind, oid = metadata.split(b" ")
        if kind == b"blob":
            tree[os.fsdecode(path)] = oid.decode()
    return tree


def _cards_from_tree(
    root: Path, tree: dict[str, str], relpaths: list[str]
) -> dict[str, tuple[Card, str | None]]:
    """Read selected cards and their paired-blob paths from one tracked tree."""
    blobs: dict[tuple[Path, str], list[str]] = {}
    for path in tree:
        parsed = Path(path)
        if parsed.suffix not in ("", ".md"):
            blobs.setdefault((parsed.parent, parsed.stem), []).append(path)
    texts = _cat_blobs(root, [(path, tree[path]) for path in relpaths])
    cards = {}
    for relpath in relpaths:
        parsed = Path(relpath)
        hits = blobs.get((parsed.parent, parsed.stem), [])
        if len(hits) > 1:
            raise ValueError(
                f"multiple blobs for {parsed.name}: {sorted(Path(p).name for p in hits)}"
            )
        cards[relpath] = (
            Card.from_text(texts[relpath].decode()),
            hits[0] if hits else None,
        )
    return cards


def _cat_blobs(root: Path, objects: list[tuple[str, str]]) -> dict[str, bytes]:
    """Read blob objects through one ``git cat-file --batch`` process."""
    if not objects:
        return {}
    process = subprocess.Popen(
        ["git", "-C", str(root), "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    found = {}
    try:
        for relpath, oid in objects:
            process.stdin.write(f"{oid}\n".encode())
            process.stdin.flush()
            header = process.stdout.readline().split()
            if len(header) != 3 or header[1] != b"blob":
                raise ValueError(f"unexpected git object header: {header!r}")
            size = int(header[2])
            found[relpath] = process.stdout.read(size)
            if process.stdout.read(1) != b"\n":
                raise ValueError("unterminated git object")
        process.stdin.close()
        if process.wait() != 0:
            raise subprocess.CalledProcessError(process.returncode, process.args)
    except BaseException:
        process.kill()
        process.wait()
        raise
    return found


def _changed_paths(root: Path, cached_head: str, head: str) -> set[str]:
    """Return every path changed in commits after ``cached_head`` through ``head``."""
    log = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "log",
            "-m",
            "--name-status",
            "--no-renames",
            "-z",
            "--pretty=format:%x01%H%x00",
            f"{cached_head}..{head}",
        ],
        check=True,
        capture_output=True,
    ).stdout
    paths = set()
    for chunk in log.split(b"\x01"):
        tokens = [token for token in chunk.split(b"\0") if token]
        if not tokens:
            continue
        i = 1
        while i < len(tokens):
            status = tokens[i].lstrip(b"\n")
            if status not in (b"A", b"M", b"D"):
                raise ValueError(
                    f"unknown git log status token: {os.fsdecode(status)!r}"
                )
            paths.add(os.fsdecode(tokens[i + 1]))
            i += 2
    return paths


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


def first_commits(root: Path, relpaths: list[str], head: str = "") -> dict[str, str]:
    """Return each relpath's lineage-origin author date, canonical UTC ISO.

    Lineage follows **renames, not copies**: a card copied-and-edited from
    another is a new card with its own birth date (its source still exists as
    its own card), so ``git log --follow``'s copy-chasing is deliberately not
    reproduced.

    One ``git log --topo-order --reverse --name-status`` pass is replayed into
    an ``origin`` map: ``A`` starts a lineage, ``R`` carries it, ``D`` ends it.
    ``--topo-order`` gives a deterministic ancestor-before-descendant order over
    merges. The replay resolves the overwhelming majority in-process; only a
    HEAD path the linear replay cannot place — a delete linearised ahead of the
    merge that preserved the file, or a rename whose source the replay lost
    across a merge — falls back to the per-path ``git log --follow``, deferring
    the genuinely DAG-ambiguous case to git's own traversal.

    A missing rename source is left *unresolved* (routed to the fallback) rather
    than dated from the rename commit: inventing that date would fabricate
    provenance and mask the fallback.

    A deck with no ``.git`` or no commits yields an empty map (the
    :meth:`MDDB.init` bootstrap).

    Args:
        root: Absolute path to the mddb directory.
        relpaths: The HEAD card relpaths needing lineage dates.

    Returns:
        ``{relpath: utc_iso_date}`` covering every requested relpath.

    Raises:
        ValueError: A requested path has no git lineage (uncommitted file —
            out of contract), or the log stream contains an unknown status
            (a ``C`` copy record is unknown drift under ``-M``-only).
    """
    if not (root / ".git").is_dir():
        return {}
    if not head:
        resolved = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if resolved.returncode != 0:
            return {}
        head = resolved.stdout.strip()
    log = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "log",
            "--topo-order",
            "--reverse",
            "--name-status",
            "-M",
            "-z",
            "--pretty=format:%x01%H%x00%aI%x00",
            head,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    origin = _replay_lineage(log, {})
    lineage = {}
    for relpath in relpaths:
        if relpath in origin:
            lineage[relpath] = utc_iso(origin[relpath])
            continue
        lineage[relpath] = _first_commit(root, relpath, head)
    return lineage


def _refresh_lineage(
    root: Path,
    conn: sqlite3.Connection,
    cached_head: str,
    head: str,
    relpaths: list[str],
) -> dict[str, str]:
    origin = dict(conn.execute("SELECT relpath, first_commit FROM entries"))
    log = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "log",
            "--topo-order",
            "--reverse",
            "--name-status",
            "-M",
            "-z",
            "--pretty=format:%x01%H%x00%aI%x00",
            f"{cached_head}..{head}",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    origin = _replay_lineage(log, origin)
    return {
        relpath: utc_iso(origin[relpath])
        if relpath in origin
        else _first_commit(root, relpath, head)
        for relpath in relpaths
    }


def _replay_lineage(log: str, origin: dict[str, str]) -> dict[str, str]:
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
                origin[tokens[i + 1]] = date
                i += 2
            elif status == "M":
                i += 2
            elif status == "D":
                origin.pop(tokens[i + 1], None)
                i += 2
            elif status.startswith("R"):
                old, new = tokens[i + 1], tokens[i + 2]
                if old in origin:
                    origin[new] = origin.pop(old)
                else:
                    origin.pop(new, None)
                i += 3
            else:
                raise ValueError(f"unknown git log status token: {status!r}")
    return origin


def _first_commit(root: Path, relpath: str, head: str) -> str:
    """Return one path's surviving-lineage origin at the captured commit."""
    follow = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "log",
            "--follow",
            "--diff-filter=A",
            "--format=%aI",
            head,
            "--",
            relpath,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    if not follow:
        raise ValueError(f"no git lineage for {relpath}")
    return utc_iso(follow[0])


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
        "INSERT INTO entries(id, relpath, title, summary, kind, blob_relpath, first_commit, yaml_text, body) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            card.id,
            relpath,
            card.yaml.get("title"),
            card.yaml.get("summary"),
            card.yaml.get("kind"),
            blob_relpath,
            first_commit,
            _yaml_text(card),
            card.body,
        ),
    )
    index_fields(conn, cur.lastrowid, card.yaml)


def update_content(conn: sqlite3.Connection, card: Card) -> None:
    """Refresh title/summary/kind/yaml_text/body and rebuild entry_fields for ``card``."""
    rowid = conn.execute(
        "SELECT rowid FROM entries WHERE id = ?", (card.id,)
    ).fetchone()[0]
    conn.execute(
        "UPDATE entries SET title = ?, summary = ?, kind = ?, yaml_text = ?, body = ? "
        "WHERE rowid = ?",
        (
            card.yaml.get("title"),
            card.yaml.get("summary"),
            card.yaml.get("kind"),
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
    """Return ``[{id, title, summary, kind, blob_relpath}, ...]`` for every cached card."""
    rows = conn.execute(
        "SELECT id, title, summary, kind, blob_relpath FROM entries"
    ).fetchall()
    return [
        {
            "id": cid,
            "title": title,
            "summary": summary,
            "kind": kind,
            "blob_relpath": blob_relpath,
        }
        for cid, title, summary, kind, blob_relpath in rows
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
        if key in ("title", "summary", "kind"):
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
