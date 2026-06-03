"""A minimal YAML-frontmatter + markdown-body card substrate.

Cards live as ``.md`` files in a directory under git; a SQLite cache at
``~/.cache/mddb/`` provides full-text and structured queries. The public API
is :class:`MDDB` and :class:`Card`; ``db.conn`` is the live SQLite connection
for callers who want to compose SQL directly.
"""

from ._core import MDDB
from .card import Card

__version__ = "0.0.5"
__all__ = ["MDDB", "Card"]
