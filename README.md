# mddb

A minimal YAML-frontmatter + markdown-body card substrate for agentic and human knowledge work.

Cards live as `.md` files in a directory under git. A derived SQLite index at `~/.cache/mddb/` provides fast structured + full-text queries. Rationales live in commit messages. The substrate has no domain knowledge — flat YAML, with three privileged keys (`id`, `title`, `summary`) corresponding to the progressive-disclosure levels. Anything heavier is layer code or an agent reasoning in a REPL.

## Quickstart

```python
import mddb

db = mddb.MDDB("~/my-mddb")

with db.transaction(rationale="bought today") as tx:
    card = tx.create(
        title="Shed inventory",
        summary="Tools and equipment kept in the shed.",
        yaml={"tags": ["shed"], "location": "shed"},
        body="A wheelbarrow.",
    )

# read by id
card = db.read(card.id)

# mutate and write back
card.yaml["location"] = "barn"
with db.transaction(rationale="moved to barn") as tx:
    tx.update(card, summary="Tools and equipment, moved to the barn.")
```

`db.transaction()` is the only mutation primitive. Batch many mutations into one commit + one SQLite transaction. A body exception inside the `with` block discards the buffer; on-disk state is unchanged.

```python
with db.transaction(rationale="bulk import") as tx:
    for item in ["fridge", "shed", "loft"]:
        tx.create(title=item.title(), summary=f"contents of the {item}")

# full-text via raw SQL — no DSL
ids = [r[0] for r in db.conn.execute(
    "SELECT id FROM entries WHERE rowid IN "
    "(SELECT rowid FROM entries_fts WHERE entries_fts MATCH ?)",
    ("wheelbarrow",),
)]

# history
for commit in db.history(card.id):
    print(commit["sha"][:7], commit["message"])
```

See `CLAUDE.md` for the philosophy and the SQLite schema, `src/mddb/schema.sql` for the schema itself.

## Status

Prototype. Single-writer. Linux only.
