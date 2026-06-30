"""A minimal YAML-frontmatter + markdown-body card substrate.

Cards live as ``.md`` files in a directory under git; a SQLite cache at
``~/.cache/mddb/`` provides full-text and structured queries. The public API
is :class:`MDDB` and :class:`Card`; ``db.conn`` is the live SQLite connection
for callers who want to compose SQL directly. :data:`SCHEMA_DOC` documents the
cache tables (entries / entry_fields / entries_fts) for those queries — an
agent can ``print(mddb.SCHEMA_DOC)`` to recall the schema and the FTS idiom.
"""

from ._core import ConflictError, MDDB
from ._index import SCHEMA_DOC
from .card import Card

__version__ = "0.0.14"
__all__ = ["MDDB", "Card", "ConflictError", "SCHEMA_DOC"]
