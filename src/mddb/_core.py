"""The :class:`MDDB` class: filesystem + git + SQLite orchestration for the public verbs."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import yaml as pyyaml

from . import index
from .card import Card


class MDDB:
    def __init__(self, path: Path | str):
        """Open an existing mddb at ``path`` or initialise a fresh one in place.

        If ``path/.git`` exists the directory is treated as an existing mddb
        root and opened. Otherwise ``path`` is created (``mkdir -p``), ``git
        init`` is run, a ``.gitignore`` containing ``*.tmp`` is committed,
        and a one-line notice is printed to stderr so a typoed path is
        visible before any data is written.

        Args:
            path: Filesystem path to the mddb directory. ``~`` is expanded
                and the path is resolved to an absolute form.

        Raises:
            subprocess.CalledProcessError: ``git`` is not installed or fails
                during initialisation.
        """
        self.root = Path(path).expanduser().resolve()
        if not (self.root / ".git").exists():
            self.root.mkdir(parents=True, exist_ok=True)
            self._init_git()
            print(f"mddb: initialised new mddb at {self.root}", file=sys.stderr)
        self.conn = index.open_index(self.root)

    def create(
        self, yaml: dict, body: str = "", *, relpath: str | None = None, rationale: str
    ) -> Card:
        """Create a new card on disk, in git, and in the index.

        Generates a UUIDv4 ``id`` if ``yaml`` does not already contain one.
        Writes the card to ``relpath`` (or ``<id>.md`` if not given), stages
        it, commits with ``rationale`` as the message, then inserts a row
        into the SQLite index.

        Args:
            yaml: Frontmatter as a Python dict. If ``"id"`` is absent a
                UUIDv4 is generated and added in-place.
            body: Markdown body. Empty by default.
            relpath: Path relative to the mddb root, e.g.
                ``"inventory/fridge.md"``. Defaults to ``<id>.md`` for a flat
                UUID-named layout. Must be relative, end in ``.md``, and not
                contain ``..``.
            rationale: Git commit message. Required, non-empty.

        Returns:
            The created :class:`Card` with ``id`` populated.

        Raises:
            FileExistsError: ``relpath`` already exists in the working tree.
            ValueError: ``relpath`` is absolute, contains ``..``, or does not
                end in ``.md``.
            subprocess.CalledProcessError: ``git add`` or ``git commit``
                failed.
        """
        yaml = dict(yaml)
        if "id" not in yaml:
            yaml["id"] = str(uuid.uuid4())
        card = Card(yaml=yaml, body=body)
        if relpath is None:
            relpath = f"{card.id}.md"
        self._validate_relpath(relpath)
        target = self.root / relpath
        if target.exists():
            raise FileExistsError(relpath)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._write_atomic(target, str(card))
        self._git("add", "--", relpath)
        self._git("commit", "-m", rationale)
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO entries(id, relpath, yaml_text, body) VALUES (?, ?, ?, ?)",
                (
                    card.id,
                    relpath,
                    pyyaml.safe_dump(card.yaml, sort_keys=False, allow_unicode=True),
                    card.body,
                ),
            )
            index.index_fields(self.conn, cur.lastrowid, card.yaml)
        return card

    def read(self, card_id: str) -> Card:
        """Read a card by id, returning a fresh :class:`Card` from disk.

        Resolves the card's relpath via the index, then loads the file
        directly (the SQLite copy is not used for reads). Mutate the
        returned card and pass it to :meth:`update` to persist changes.

        Args:
            card_id: The card's ``id`` (the value at ``yaml["id"]``).

        Returns:
            The :class:`Card` as parsed from its current file.

        Raises:
            KeyError: No card with that ``id`` is known to the index.
            FileNotFoundError: The index points at a path that no longer
                exists on disk.
        """
        return Card.from_file(self.root / self._relpath(card_id))

    def update(self, card: Card, *, rationale: str) -> Card:
        """Persist mutations to ``card`` back to disk, git, and the index.

        Resolves the file via ``card.id`` (the value at
        ``card.yaml["id"]``), rewrites the file from ``str(card)``, commits
        with ``rationale``, then refreshes the index row + entry_fields.

        The id is effectively immutable: do not mutate ``card.yaml["id"]``
        between read and update — the lookup will fail with ``KeyError``
        because the new id is not in the index. To change a card's
        location, use :meth:`move`.

        Args:
            card: The :class:`Card` to write back. Its ``yaml`` and ``body``
                are persisted; ``yaml["id"]`` must match the value used at
                read time.
            rationale: Git commit message. Required, non-empty.

        Returns:
            The same ``card`` (for chaining).

        Raises:
            KeyError: ``card.yaml["id"]`` is not in the index.
            subprocess.CalledProcessError: ``git add`` or ``git commit``
                failed.
        """
        relpath = self._relpath(card.id)
        self._write_atomic(self.root / relpath, str(card))
        self._git("add", "--", relpath)
        self._git("commit", "-m", rationale)
        with self.conn:
            rowid = self.conn.execute(
                "SELECT rowid FROM entries WHERE id = ?", (card.id,)
            ).fetchone()[0]
            self.conn.execute(
                "UPDATE entries SET yaml_text = ?, body = ? WHERE rowid = ?",
                (
                    pyyaml.safe_dump(card.yaml, sort_keys=False, allow_unicode=True),
                    card.body,
                    rowid,
                ),
            )
            self.conn.execute(
                "DELETE FROM entry_fields WHERE entry_rowid = ?", (rowid,)
            )
            index.index_fields(self.conn, rowid, card.yaml)
        return card

    def delete(self, card_id: str, *, rationale: str) -> None:
        """Remove a card from the working tree, git, and the index.

        The deletion is committed (``git rm`` + ``git commit``) so the
        card's history is recoverable via ``git show <sha>:<relpath>`` for
        any commit before this one.

        Args:
            card_id: The card's ``id``.
            rationale: Git commit message. Required, non-empty.

        Raises:
            KeyError: No card with that ``id`` is known to the index.
            subprocess.CalledProcessError: ``git rm`` or ``git commit``
                failed.
        """
        relpath = self._relpath(card_id)
        self._git("rm", "--", relpath)
        self._git("commit", "-m", rationale)
        with self.conn:
            self.conn.execute("DELETE FROM entries WHERE id = ?", (card_id,))

    def move(self, card_id: str, new_relpath: str, *, rationale: str) -> None:
        """Rename a card's file. The id is preserved so history follows.

        Uses ``git mv`` so a single commit records the rename (and
        :meth:`history` can follow it via ``git log --follow``). The card's
        ``id`` and contents are unchanged; only ``new_relpath`` differs.

        Args:
            card_id: The card's ``id``.
            new_relpath: Target path relative to the mddb root. Must be
                relative, end in ``.md``, and not contain ``..``.
            rationale: Git commit message. Required, non-empty.

        Raises:
            KeyError: No card with that ``id`` is known to the index.
            ValueError: ``new_relpath`` is invalid.
            subprocess.CalledProcessError: ``git mv`` or ``git commit``
                failed (e.g. target path already exists).
        """
        old = self._relpath(card_id)
        self._validate_relpath(new_relpath)
        (self.root / new_relpath).parent.mkdir(parents=True, exist_ok=True)
        self._git("mv", "--", old, new_relpath)
        self._git("commit", "-m", rationale)
        with self.conn:
            self.conn.execute(
                "UPDATE entries SET relpath = ? WHERE id = ?", (new_relpath, card_id)
            )

    def history(self, card_id: str) -> list[dict]:
        """Return the commit history of a card, newest first.

        Uses ``git log --follow`` so commits from before any :meth:`move`
        are included.

        Args:
            card_id: The card's ``id``.

        Returns:
            A list of dicts, newest first. Each dict has:

            - ``sha`` (str): full commit SHA.
            - ``author`` (str): commit author name.
            - ``timestamp`` (str): ISO-8601 author date.
            - ``message`` (str): full commit message body (the
              ``rationale`` passed to the mutating verb).

        Raises:
            KeyError: No card with that ``id`` is known to the index.
            subprocess.CalledProcessError: ``git log`` failed.
        """
        relpath = self._relpath(card_id)
        out = self._git(
            "log",
            "--follow",
            "--pretty=format:%H%x00%an%x00%aI%x00%B%x1e",
            "--",
            relpath,
        ).stdout
        commits = []
        for chunk in out.split("\x1e"):
            chunk = chunk.strip("\n")
            if not chunk:
                continue
            sha, author, ts, message = chunk.split("\x00", 3)
            commits.append(
                {"sha": sha, "author": author, "timestamp": ts, "message": message}
            )
        return commits

    def _relpath(self, card_id: str) -> str:
        row = self.conn.execute(
            "SELECT relpath FROM entries WHERE id = ?", (card_id,)
        ).fetchone()
        if row is None:
            raise KeyError(card_id)
        return row[0]

    def _write_atomic(self, target: Path, text: str) -> None:
        tmp = target.with_suffix(target.suffix + f".{uuid.uuid4().hex}.tmp")
        tmp.write_text(text)
        os.replace(tmp, target)

    def _init_git(self) -> None:
        subprocess.run(["git", "init", "-q", "-b", "master"], cwd=self.root, check=True)
        (self.root / ".gitignore").write_text("*.tmp\n")
        self._git("add", "--", ".gitignore")
        self._git("commit", "-m", "initial commit")

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True
        )

    def _validate_relpath(self, relpath: str) -> None:
        if not relpath.endswith(".md"):
            raise ValueError(f"relpath must end in .md: {relpath}")
        p = Path(relpath)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError(f"invalid relpath: {relpath}")
