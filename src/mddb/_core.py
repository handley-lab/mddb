"""The :class:`MDDB` class: filesystem + git + SQLite orchestration for the public verbs."""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from slugify import slugify

from . import _index
from .card import Card


class MDDB:
    """A YAML+markdown card substrate over a git directory + SQLite cache.

    Attributes:
        root: Absolute path to the mddb directory. Cards live here as ``.md``
            files; git records rationale and history.
        conn: Live ``sqlite3.Connection`` to the derived index at
            ``~/.cache/mddb/<sha1(abs-path)>/index.sqlite``. Write SQL against
            it directly — there is no query DSL. See ``CLAUDE.md`` for the
            schema.
    """

    def __init__(self, path: Path | str):
        """Open the mddb at ``path``. Use :meth:`init` to bootstrap a fresh one."""
        self.root = Path(path).expanduser().resolve()
        self.conn = _index.open_index(self.root)
        self._active_editor: _Editor | None = None

    @classmethod
    def init(cls, path: Path | str) -> MDDB:
        """Bootstrap a fresh mddb at ``path``.

        Creates the directory (``mkdir -p``), runs ``git init``, commits a
        ``.gitignore`` containing ``*.tmp``, and returns the opened handle.
        """
        root = Path(path).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        db = cls(root)
        db._git("init", "-q", "-b", "master")
        (root / ".gitignore").write_text("*.tmp\n")
        db._git("add", "--", ".gitignore")
        db._git("commit", "-q", "-m", "initial commit")
        return db

    def read(self, card_id: str) -> Card:
        """Read a card by id, returning a fresh :class:`Card` from disk.

        Resolves the card's relpath via the index, then loads the file
        directly (the SQLite copy is not used for reads). Mutate the
        returned card and pass it to :meth:`_Editor.update` (inside a
        :meth:`editor` block) to persist changes.

        Args:
            card_id: The card's ``id`` (the value at ``yaml["id"]``).

        Returns:
            The :class:`Card` as parsed from its current file.

        Raises:
            KeyError: No card with that ``id`` is known to the _index.
            FileNotFoundError: The index points at a path that no longer
                exists on disk.
        """
        return Card.from_file(self.root / _index.relpath_of(self.conn, card_id))

    def list(self) -> list[dict]:
        """Return every card's id, title, and summary — the progressive-disclosure summary view.

        Cheap to call: pulls only the three substrate-privileged keys from
        the SQLite cache (no body, no YAML parse). Use :meth:`read` for the
        full card once a caller has decided which one to open.

        Returns:
            A list of ``{"id": str, "title": str | None, "summary": str | None,
            "blob_relpath": str | None}`` dicts, one per card. ``title`` and
            ``summary`` come back as ``None`` for cards missing those keys;
            ``blob_relpath`` is the relpath of the card's binary blob (see
            :attr:`Card.blob`), or ``None`` when it has none.
        """
        return _index.list_progressive(self.conn)

    def history(self, card_id: str) -> list[dict]:
        """Return the commit history of a card, newest first.

        Uses ``git log --follow`` so commits from before any ``editor.move(...)``
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
            KeyError: No card with that ``id`` is known to the _index.
            subprocess.CalledProcessError: ``git log`` failed.
        """
        relpath = _index.relpath_of(self.conn, card_id)
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

    def editor(self, *, rationale: str) -> _Editor:
        """Open a context manager for a batch of mutations.

        The returned object's ``create``/``read``/``update``/``delete``/``move``/``edit``
        methods are buffered until the ``with`` block exits cleanly, then
        materialised as one git commit + one SQLite transaction.
        """
        return _Editor(self, rationale)

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True
        )


def _validate_in_root(root: Path, relpath: str) -> None:
    """Reject relpaths that the cache rebuild cannot reproduce.

    Three checks (in order): non-canonical text (absolute or ``.``/``..``
    parts), resolved location outside ``root``, or in-root symlink alias
    whose resolved relative path differs from the textual relpath.
    """
    path = Path(relpath)
    if path.is_absolute() or any(part in (".", "..") for part in path.parts):
        raise ValueError(f"relpath must be relative and canonical: {relpath}")
    root_resolved = root.resolve()
    resolved = (root / relpath).resolve()
    if not resolved.is_relative_to(root_resolved):
        raise ValueError(f"relpath escapes root: {relpath}")
    if str(resolved.relative_to(root_resolved)) != relpath:
        raise ValueError(f"relpath must be relative and canonical: {relpath}")


@dataclass
class _Create:
    card: Card
    relpath: str


@dataclass
class _Update:
    card: Card
    original_relpath: str
    relpath: str


@dataclass
class _Move:
    original_relpath: str
    relpath: str


@dataclass
class _Delete:
    original_relpath: str


_Staged = _Create | _Update | _Move | _Delete


class _Editor:
    """Buffer create/read/update/delete/move/edit and materialise them as one commit on clean exit.

    Construct via :meth:`MDDB.editor`. Operations are staged in memory until
    the ``with`` block exits cleanly; on body exception, nothing is written.
    On clean exit with a non-empty buffer, the entire batch is materialised
    as a single git commit and a single SQLite transaction.

    Single-shot — after ``__exit__`` (clean or not), further verb calls
    and re-entry both raise ``RuntimeError``.
    """

    def __init__(self, db: MDDB, rationale: str):
        self._db = db
        self._rationale = rationale
        self._staged: dict[str, _Staged] = {}
        self._closed = False

    def __enter__(self) -> _Editor:
        if self._closed:
            raise RuntimeError("editor already closed")
        if self._db._active_editor is not None:
            raise RuntimeError("nested editors are not supported")
        self._db._active_editor = self
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None and self._staged:
                self._materialise()
        finally:
            self._db._active_editor = None
            self._closed = True

    def _materialise(self) -> None:
        """Apply the staged batch as one git commit + one SQLite transaction.

        Phases: filesystem deletes → moves → writes/adds → git commit →
        SQLite cache sync (insert/update/delete inside ``with self._db.conn:``).
        The git commit is path-restricted to the relpaths this editor touched
        so unrelated pre-staged changes in the working tree stay staged.
        """
        for staged in self._staged.values():
            if not isinstance(staged, (_Create, _Update, _Move, _Delete)):
                raise TypeError(f"unknown staged variant: {type(staged).__name__}")
        touched: set[str] = set()
        for staged in self._staged.values():
            if isinstance(staged, _Delete):
                self._db._git("rm", "--", staged.original_relpath)
                touched.add(staged.original_relpath)
        for staged in self._staged.values():
            if isinstance(staged, (_Move, _Update)) and (
                staged.relpath != staged.original_relpath
            ):
                (self._db.root / staged.relpath).parent.mkdir(
                    parents=True, exist_ok=True
                )
                self._db._git("mv", "--", staged.original_relpath, staged.relpath)
                touched.add(staged.original_relpath)
                touched.add(staged.relpath)
        for staged in self._staged.values():
            if isinstance(staged, (_Create, _Update)):
                target = self._db.root / staged.relpath
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_suffix(target.suffix + f".{uuid.uuid4().hex}.tmp")
                tmp.write_text(str(staged.card))
                os.replace(tmp, target)
                self._db._git("add", "--", staged.relpath)
                touched.add(staged.relpath)
        self._db._git("commit", "-m", self._rationale, "--", *sorted(touched))
        with self._db.conn:
            for card_id, staged in self._staged.items():
                if isinstance(staged, _Delete):
                    _index.delete(self._db.conn, card_id)
                    continue
                blob = _index.blob_on_disk(self._db.root / staged.relpath)
                blob_relpath = str(blob.relative_to(self._db.root)) if blob else None
                if isinstance(staged, _Create):
                    _index.insert(
                        self._db.conn, staged.card, staged.relpath, blob_relpath
                    )
                elif isinstance(staged, _Move):
                    _index.update_paths(
                        self._db.conn, card_id, staged.relpath, blob_relpath
                    )
                elif isinstance(staged, _Update):
                    _index.update_paths(
                        self._db.conn, card_id, staged.relpath, blob_relpath
                    )
                    _index.update_content(self._db.conn, staged.card)
                else:
                    raise TypeError(f"unknown staged variant: {type(staged).__name__}")

    def create(
        self,
        *,
        title: str,
        summary: str,
        yaml: dict | None = None,
        body: str = "",
        relpath: str = "",
        tags: Sequence[str] | None = None,
    ) -> Card:
        """Stage a new card for creation. Materialises on clean ``__exit__``.

        On-disk YAML keys are written in canonical order: ``id``, ``title``,
        ``summary``, ``tags`` (when present), then the caller's remaining
        ``yaml=`` keys in their original relative order.

        ``tags`` is a three-state kwarg:
            - ``None`` (default): no override. Caller's ``yaml["tags"]`` is
              preserved if present.
            - empty sequence (``()``/``[]``): explicit clear; no ``tags``
              key on the card even if ``yaml`` supplied one.
            - non-empty sequence: replace; ``tags=`` wins over any
              ``yaml["tags"]`` value.

        Required kwargs (``title``, ``summary``) win over any matching keys
        in ``yaml=``. ``id`` from ``yaml=`` is preserved verbatim (any
        value); a UUIDv4 is generated only when ``id`` is absent.
        """
        if self._closed:
            raise RuntimeError("editor already closed")
        caller_yaml = {} if yaml is None else dict(yaml)
        caller_yaml.pop("title", None)
        caller_yaml.pop("summary", None)
        yaml_d = {}
        if "id" in caller_yaml:
            yaml_d["id"] = caller_yaml.pop("id")
        else:
            yaml_d["id"] = str(uuid.uuid4())
        yaml_d["title"] = title
        yaml_d["summary"] = summary
        if tags is None:
            if "tags" in caller_yaml:
                yaml_d["tags"] = caller_yaml.pop("tags")
        elif tags:
            yaml_d["tags"] = list(tags)
            caller_yaml.pop("tags", None)
        else:
            caller_yaml.pop("tags", None)
        yaml_d.update(caller_yaml)
        new_card = Card(yaml=yaml_d, body=body)
        resolved = (
            relpath
            if relpath.endswith(".md")
            else os.path.join(relpath, f"{slugify(title)}.md")
        )
        _validate_in_root(self._db.root, resolved)
        if new_card.id in self._staged:
            raise RuntimeError(f"duplicate id in editor: {new_card.id}")
        if self._claim_for(resolved) is not None:
            raise FileExistsError(resolved)
        if (self._db.root / resolved).exists() and not any(
            isinstance(s, _Delete) and s.original_relpath == resolved
            for s in self._staged.values()
        ):
            raise FileExistsError(resolved)
        self._staged[new_card.id] = _Create(card=new_card, relpath=resolved)
        return new_card.copy()

    def read(self, card_id: str) -> Card:
        """Read a card with staged-state visibility."""
        if self._closed:
            raise RuntimeError("editor already closed")
        staged = self._staged.get(card_id)
        if staged is None:
            return self._db.read(card_id)
        if isinstance(staged, _Delete):
            raise KeyError(card_id)
        if isinstance(staged, (_Create, _Update)):
            return staged.card.copy()
        if isinstance(staged, _Move):
            return self._db.read(card_id).copy()
        raise TypeError(f"unknown staged variant: {type(staged).__name__}")

    def update(
        self,
        card: Card,
        *,
        summary: str,
        tags: Sequence[str] | None = None,
    ) -> Card:
        """Stage an update to ``card``. Caller's ``Card`` is deep-copied before staging.

        ``tags`` is a three-state kwarg with the same semantics as in
        :meth:`create`:
            - ``None`` (default): leave ``card.yaml["tags"]`` as-is. In-place
              mutations made by the caller before calling ``update`` persist.
            - empty sequence: remove the ``tags`` key from ``card.yaml`` (if
              present).
            - non-empty sequence: replace ``card.yaml["tags"]``.

        Does NOT re-canonicalise existing YAML key order.
        """
        if self._closed:
            raise RuntimeError("editor already closed")
        if isinstance(self._staged.get(card.id), _Delete):
            raise KeyError(card.id)
        card.yaml["summary"] = summary
        if tags is not None:
            if tags:
                card.yaml["tags"] = list(tags)
            elif "tags" in card.yaml:
                del card.yaml["tags"]
        self._stage_content_update(card)
        return card.copy()

    def edit(
        self,
        card_id: str,
        old: str,
        new: str,
        *,
        replace_all: bool = False,
    ) -> int:
        """Find/replace text in the card's body. Mirrors :func:`loop.edit`.

        Body-only — preserves title, summary, tags, relpath, and body content
        outside the match region. For structured updates use :meth:`update`.

        Args:
            card_id: The card's id.
            old: Substring to find. Must be non-empty.
            new: Replacement substring. May be empty (removes the match).
            replace_all: If True, replace every occurrence; otherwise require
                exactly one match.

        Returns:
            Number of replacements made (or matched, if ``old == new``).

        Raises:
            ValueError: ``old`` is empty; ``old`` not found in body; or
                ``old`` occurs multiple times and ``replace_all`` is False
                (unless ``old == new``, which short-circuits as a no-op).
            KeyError: ``card_id`` is staged-deleted, or unknown.
            RuntimeError: editor already closed.
        """
        if self._closed:
            raise RuntimeError("editor already closed")
        if not old:
            raise ValueError("old must not be empty")
        card = self.read(card_id)
        count = card.body.count(old)
        if count == 0:
            raise ValueError(f"not found: {old!r}")
        if old == new:
            return count
        if count > 1 and not replace_all:
            raise ValueError(
                f"not unique: {count} occurrences (pass replace_all=True to replace all)"
            )
        card.body = card.body.replace(old, new)
        self._stage_content_update(card)
        return count

    def _stage_content_update(self, card: Card) -> None:
        staged_card = card.copy()
        staged = self._staged.get(card.id)
        if staged is None:
            original = _index.relpath_of(self._db.conn, card.id)
            self._staged[card.id] = _Update(
                card=staged_card, original_relpath=original, relpath=original
            )
        elif isinstance(staged, _Create):
            self._staged[card.id] = _Create(card=staged_card, relpath=staged.relpath)
        elif isinstance(staged, (_Update, _Move)):
            self._staged[card.id] = _Update(
                card=staged_card,
                original_relpath=staged.original_relpath,
                relpath=staged.relpath,
            )
        else:
            raise TypeError(f"unknown staged variant: {type(staged).__name__}")

    def delete(self, card_id: str) -> None:
        """Stage a delete of ``card_id``."""
        if self._closed:
            raise RuntimeError("editor already closed")
        staged = self._staged.get(card_id)
        if isinstance(staged, _Delete):
            raise KeyError(card_id)
        if isinstance(staged, _Create):
            del self._staged[card_id]
            return
        if isinstance(staged, (_Update, _Move)):
            self._staged[card_id] = _Delete(original_relpath=staged.original_relpath)
            return
        if staged is None:
            self._staged[card_id] = _Delete(
                original_relpath=_index.relpath_of(self._db.conn, card_id),
            )
            return
        raise TypeError(f"unknown staged variant: {type(staged).__name__}")

    def move(self, card_id: str, new_relpath: str) -> None:
        """Stage a relpath change for ``card_id``.

        ``new_relpath`` must end in ``.md`` — the cache rebuild only indexes
        ``*.md`` files, so a non-``.md`` target would silently disappear from
        the cache on the next rebuild. ``move`` does not apply suffix-decides
        (unlike ``relpath=`` on ``create``); callers pass an exact filename.
        """
        if self._closed:
            raise RuntimeError("editor already closed")
        if not new_relpath.endswith(".md"):
            raise ValueError(f"relpath must end in .md: {new_relpath}")
        _validate_in_root(self._db.root, new_relpath)
        staged = self._staged.get(card_id)
        if isinstance(staged, _Delete):
            raise KeyError(card_id)
        if staged is None:
            current = _index.relpath_of(self._db.conn, card_id)
        elif isinstance(staged, _Create):
            current = staged.relpath
        elif isinstance(staged, (_Update, _Move)):
            current = staged.relpath
        else:
            raise TypeError(f"unknown staged variant: {type(staged).__name__}")
        if new_relpath == current:
            return
        if self._claim_for(new_relpath) is not None:
            raise FileExistsError(new_relpath)
        if (self._db.root / new_relpath).exists():
            original_relpath = (
                staged.original_relpath
                if isinstance(staged, (_Update, _Move))
                else None
            )
            if original_relpath != new_relpath:
                raise FileExistsError(new_relpath)
        if staged is None:
            self._staged[card_id] = _Move(
                original_relpath=_index.relpath_of(self._db.conn, card_id),
                relpath=new_relpath,
            )
            return
        if isinstance(staged, _Create):
            self._staged[card_id] = _Create(card=staged.card, relpath=new_relpath)
            return
        if isinstance(staged, _Update):
            self._staged[card_id] = _Update(
                card=staged.card,
                original_relpath=staged.original_relpath,
                relpath=new_relpath,
            )
            return
        if isinstance(staged, _Move):
            if new_relpath == staged.original_relpath:
                del self._staged[card_id]
                return
            self._staged[card_id] = _Move(
                original_relpath=staged.original_relpath, relpath=new_relpath
            )
            return
        raise TypeError(f"unknown staged variant: {type(staged).__name__}")

    def _claim_for(self, relpath: str) -> str | None:
        for card_id, staged in self._staged.items():
            if isinstance(staged, (_Create, _Update, _Move)):
                if staged.relpath == relpath:
                    return card_id
            elif isinstance(staged, _Delete):
                continue
            else:
                raise TypeError(f"unknown staged variant: {type(staged).__name__}")
        return None
