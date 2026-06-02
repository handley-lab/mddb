from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Card:
    yaml: dict = field(default_factory=dict)
    body: str = ""

    @property
    def id(self) -> str:
        return self.yaml["id"]

    @classmethod
    def from_file(cls, path: Path) -> Card:
        return cls.from_text(path.read_text(encoding="utf-8"))

    @classmethod
    def from_text(cls, text: str) -> Card:
        if not text.startswith("---\n"):
            raise ValueError("missing opening frontmatter delimiter")
        end = text.index("\n---\n", 4)
        fm = yaml.safe_load(text[4 : end + 1])
        body = text[end + 5 :]
        return cls(yaml=fm, body=body)

    def __str__(self) -> str:
        fm = yaml.safe_dump(self.yaml, sort_keys=False, allow_unicode=True)
        return f"---\n{fm}---\n{self.body}"
