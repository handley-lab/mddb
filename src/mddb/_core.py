from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import yaml

from . import _index
from .card import Card


def _dump(data: dict) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)


class MDDB:
    def __init__(self, path: Path | str):
        self.root = Path(path).expanduser().resolve()
        if not (self.root / ".git").exists():
            self.root.mkdir(parents=True, exist_ok=True)
            self._init_git()
            print(f"mddb: initialised new mddb at {self.root}", file=sys.stderr)
        self.conn = _index.open_index(self.root)

    def create(self, yaml: dict, body: str = "", *, relpath: str | None = None, rationale: str) -> Card:
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
                (card.id, relpath, _dump(card.yaml), card.body),
            )
            _index.index_fields(self.conn, cur.lastrowid, card.yaml)
        return card

    def read(self, card_id: str) -> Card:
        return Card.from_file(self.root / self._relpath(card_id))

    def update(self, card: Card, *, rationale: str) -> Card:
        relpath = self._relpath(card.id)
        self._write_atomic(self.root / relpath, str(card))
        self._git("add", "--", relpath)
        self._git("commit", "-m", rationale)
        with self.conn:
            rowid = self.conn.execute("SELECT rowid FROM entries WHERE id = ?", (card.id,)).fetchone()[0]
            self.conn.execute(
                "UPDATE entries SET yaml_text = ?, body = ? WHERE rowid = ?",
                (_dump(card.yaml), card.body, rowid),
            )
            self.conn.execute("DELETE FROM entry_fields WHERE entry_rowid = ?", (rowid,))
            _index.index_fields(self.conn, rowid, card.yaml)
        return card

    def delete(self, card_id: str, *, rationale: str) -> None:
        relpath = self._relpath(card_id)
        self._git("rm", "--", relpath)
        self._git("commit", "-m", rationale)
        with self.conn:
            self.conn.execute("DELETE FROM entries WHERE id = ?", (card_id,))

    def move(self, card_id: str, new_relpath: str, *, rationale: str) -> None:
        old = self._relpath(card_id)
        self._validate_relpath(new_relpath)
        (self.root / new_relpath).parent.mkdir(parents=True, exist_ok=True)
        self._git("mv", "--", old, new_relpath)
        self._git("commit", "-m", rationale)
        with self.conn:
            self.conn.execute("UPDATE entries SET relpath = ? WHERE id = ?", (new_relpath, card_id))

    def history(self, card_id: str) -> list[dict]:
        relpath = self._relpath(card_id)
        out = self._git(
            "log", "--follow", "--pretty=format:%H%x00%an%x00%aI%x00%B%x1e", "--", relpath,
        ).stdout
        commits = []
        for chunk in out.split("\x1e"):
            chunk = chunk.strip("\n")
            if not chunk:
                continue
            sha, author, ts, message = chunk.split("\x00", 3)
            commits.append({"sha": sha, "author": author, "timestamp": ts, "message": message})
        return commits

    def _relpath(self, card_id: str) -> str:
        row = self.conn.execute("SELECT relpath FROM entries WHERE id = ?", (card_id,)).fetchone()
        if row is None:
            raise KeyError(card_id)
        return row[0]

    def _write_atomic(self, target: Path, text: str) -> None:
        tmp = target.with_suffix(target.suffix + f".{uuid.uuid4().hex}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)

    def _init_git(self) -> None:
        subprocess.run(["git", "init", "-q", "-b", "master"], cwd=self.root, check=True)
        (self.root / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
        self._git("add", "--", ".gitignore")
        self._git("commit", "-m", "initial commit")

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True)

    def _validate_relpath(self, relpath: str) -> None:
        if not relpath.endswith(".md"):
            raise ValueError(f"relpath must end in .md: {relpath}")
        p = Path(relpath)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError(f"invalid relpath: {relpath}")
