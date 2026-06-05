"""The :class:`Card` dataclass and its YAML+markdown file roundtrip."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)", re.DOTALL)


@dataclass
class Card:
    """A markdown card: YAML frontmatter (as a dict) + markdown body (as a string).

    Mutate ``yaml`` or ``body`` in place and pass the card to
    :meth:`_Editor.update` (inside an :meth:`MDDB.editor` block) to persist.

    The substrate filing keys are ``id``, ``title``, ``summary``, and ``tags``.
    The :attr:`id`, :attr:`title`, :attr:`summary`, and :attr:`tags` properties
    access them directly and raise ``KeyError`` if missing. ``title`` and
    ``summary`` are written by ``editor.create`` unconditionally — a missing
    key signals drift. ``tags`` is optional — untagged cards routinely omit
    the key, so ``card.tags`` raising is a *normal* signal, not drift.
    Callers who treat tags as optional use ``card.yaml.get("tags", [])``.

    A card may have a **blob**: one binary file sharing its filename stem
    (``floorplan.md`` + ``floorplan.png``). :attr:`blob` is the absolute path
    to that file, or ``None``. It is populated by :meth:`MDDB.read` from disk;
    cards built via :meth:`from_text`/:meth:`from_file` have ``blob=None``
    (text alone has no filesystem location). The blob is a separate file, not
    part of the markdown — :meth:`__str__` does not include it, and it is
    excluded from equality so the same card read from two checkouts (different
    absolute roots) compares equal.

    Attributes:
        yaml: Frontmatter as a Python dict. Must contain ``"id"``;
            ``"title"`` and ``"summary"`` are strongly expected; ``"tags"``
            may be absent.
        body: Markdown body text.
        blob: Absolute path to the card's binary blob, or ``None``. Stamped
            by :meth:`MDDB.read`; not serialised; not part of equality.
    """

    yaml: dict = field(default_factory=dict)
    body: str = ""
    blob: Path | None = field(default=None, compare=False)

    @property
    def id(self) -> str:
        """Return the card's id (the ``"id"`` key of ``self.yaml``)."""
        return self.yaml["id"]

    @property
    def title(self) -> str:
        """Return the card's title (substrate filing key, progressive disclosure)."""
        return self.yaml["title"]

    @property
    def summary(self) -> str:
        """Return the card's summary (substrate filing key, progressive disclosure)."""
        return self.yaml["summary"]

    @property
    def tags(self) -> list:
        """Return the card's tags (substrate filing key).

        Raises ``KeyError`` for untagged cards. Use ``card.yaml.get("tags", [])``
        if absence should be treated as the empty list.
        """
        return self.yaml["tags"]

    def copy(self) -> Card:
        """Return a deep copy of this card.

        ``yaml`` is deep-copied (nested lists/dicts are independent);
        ``body`` is a string and shared; ``blob`` is an immutable ``Path``
        and shared.
        """
        return Card(yaml=deepcopy(self.yaml), body=self.body, blob=self.blob)

    @classmethod
    def from_file(cls, path: Path) -> Card:
        """Read a card from disk."""
        return cls.from_text(path.read_text())

    @classmethod
    def from_text(cls, text: str) -> Card:
        r"""Parse a card from its on-disk string form.

        The text must begin with ``---\n`` and contain a closing ``\n---\n``
        separator before the body.

        Raises:
            ValueError: ``text`` does not have a YAML frontmatter delimited by
                ``---\n`` ... ``\n---\n``.
        """
        match = _FRONTMATTER.match(text)
        if match is None:
            raise ValueError(
                "malformed frontmatter (expected '---\\n...\\n---\\n' prefix)"
            )
        fm_text, body = match.groups()
        return cls(yaml=yaml.safe_load(fm_text), body=body)

    def __str__(self) -> str:
        """Serialise the card to its on-disk ``.md`` form (frontmatter + body).

        The :attr:`blob` is a separate filesystem file and is not included.
        """
        fm = yaml.safe_dump(self.yaml, sort_keys=False, allow_unicode=True)
        return f"---\n{fm}---\n{self.body}"
