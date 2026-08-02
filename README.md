# mddb

Correct, minimal documentation is best. Omission is preferable to an
unsupported or obsolete claim. Incorrect documentation is worst.

MDDB is a Python substrate for YAML-frontmatter Markdown cards. Cards and Git
history are authoritative; a rebuildable SQLite index provides structured and
full-text queries. The substrate knows only `id`, `title`, `summary`, `relpath`,
and `tags`. Domain fields and policy belong in callers.

```python
import mddb

db = mddb.MDDB.init("~/notes")

with db.editor(rationale="record shed inventory") as editor:
    card = editor.create(
        title="Shed inventory",
        summary="Tools kept in the shed.",
        tags=["shed"],
        yaml={"location": "shed"},
        body="A wheelbarrow.",
    )

card = db.read(card.id)
card.yaml["location"] = "barn"
with db.editor(rationale="move tools to barn") as editor:
    editor.update(card, summary="Tools moved from the shed to the barn.")
```

`db.editor()` is the only mutation primitive. One mutating editor block produces
one Git commit and one SQLite transaction. Supply `base=db.head()` to protect a
read-to-write span against another MDDB writer; stale bases raise
`mddb.ConflictError`.

Queries compose through the exposed SQLite connection:

```python
ids = [row[0] for row in db.conn.execute(
    "SELECT id FROM entries WHERE rowid IN "
    "(SELECT rowid FROM entries_fts WHERE entries_fts MATCH ?)",
    ("wheelbarrow",),
)]
cards = [db.read(card_id) for card_id in ids]
```

Print `mddb.SCHEMA_DOC` for the index schema and FTS idiom. `db.list()` provides
progressive disclosure; `db.history(card.id)` returns the card's Git history.

## Semantic merge driver

MDDB supplies a Git merge driver for cards. Register its Python implementation
once per user, then commit the deck's `.gitattributes`:

```python
from mddb._merge import install, install_global

install_global()
install("~/notes")
```

The driver set-merges tags, line-merges bodies, preserves immutable IDs, and
leaves genuine scalar divergence as a conflict. Without the global registration,
Git silently falls back to its ordinary text merge.

See [CLAUDE.md](CLAUDE.md) for repository invariants and
`src/mddb/schema.sql` for exact index DDL.

## License

MIT.
