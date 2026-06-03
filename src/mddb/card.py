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
    :meth:`MDDB.update` to persist.

    The three substrate-privileged YAML keys are ``id``, ``title``, and
    ``summary`` (the progressive-disclosure levels). The :attr:`id`,
    :attr:`title`, and :attr:`summary` properties access them directly and
    raise ``KeyError`` if missing.

    Attributes:
        yaml: Frontmatter as a Python dict. Must contain ``"id"``;
            ``"title"`` and ``"summary"`` are strongly expected.
        body: Markdown body text.
    """

    yaml: dict = field(default_factory=dict)
    body: str = ""

    @property
    def id(self) -> str:
        """Return the card's id (the ``"id"`` key of ``self.yaml``)."""
        return self.yaml["id"]

    @property
    def title(self) -> str:
        """Return the card's title (substrate-privileged for progressive disclosure)."""
        return self.yaml["title"]

    @property
    def summary(self) -> str:
        """Return the card's summary (substrate-privileged for progressive disclosure)."""
        return self.yaml["summary"]

    def copy(self) -> Card:
        """Return a deep copy of this card.

        ``yaml`` is deep-copied (nested lists/dicts are independent);
        ``body`` is a string and shared.
        """
        return Card(yaml=deepcopy(self.yaml), body=self.body)

    @classmethod
    def from_file(cls, path: Path) -> Card:
        """Read a card from disk."""
        return cls.from_text(path.read_text())

    @classmethod
    def from_text(cls, text: str) -> Card:
        r"""Parse a card from its on-disk string form.

        The text must begin with ``---\n`` and contain a closing ``\n---\n``
        separator before the body.
        """
        fm_text, body = _FRONTMATTER.match(text).groups()
        return cls(yaml=yaml.safe_load(fm_text), body=body)

    def __str__(self) -> str:
        """Serialise the card to its on-disk file form (frontmatter + body)."""
        fm = yaml.safe_dump(self.yaml, sort_keys=False, allow_unicode=True)
        return f"---\n{fm}---\n{self.body}"
