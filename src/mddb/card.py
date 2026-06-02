"""The :class:`Card` dataclass and its YAML+markdown file roundtrip."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Card:
    """A markdown card: YAML frontmatter (as a dict) + markdown body (as a string).

    Mutate ``yaml`` or ``body`` in place and pass the card to
    :meth:`MDDB.update` to persist.

    Attributes:
        yaml: Frontmatter as a Python dict. Must contain an ``"id"`` key.
        body: Markdown body text.
    """

    yaml: dict = field(default_factory=dict)
    body: str = ""

    @property
    def id(self) -> str:
        """Return the card's id (the ``"id"`` key of ``self.yaml``)."""
        return self.yaml["id"]

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
            ValueError: Missing opening frontmatter delimiter or no closing
                separator.
        """
        if not text.startswith("---\n"):
            raise ValueError("missing opening frontmatter delimiter")
        end = text.index("\n---\n", 4)
        fm = yaml.safe_load(text[4 : end + 1])
        body = text[end + 5 :]
        return cls(yaml=fm, body=body)

    def __str__(self) -> str:
        """Serialise the card to its on-disk file form (frontmatter + body)."""
        fm = yaml.safe_dump(self.yaml, sort_keys=False, allow_unicode=True)
        return f"---\n{fm}---\n{self.body}"
