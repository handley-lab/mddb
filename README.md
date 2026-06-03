# mddb

A minimal YAML-frontmatter + markdown-body card substrate for agentic and human knowledge work.

Cards live as `.md` files in a directory under git. A derived SQLite index at `~/.cache/mddb/` provides fast structured + full-text queries. Rationales live in commit messages. The substrate has no domain knowledge — flat YAML. The substrate filing vocabulary is `id`, `title`, `summary`, `relpath`, and `tags` (structures the substrate provides; the names are user choice). Anything heavier is layer code or an agent reasoning in a REPL.

## Quickstart

```python
import mddb

db = mddb.MDDB.init("~/my-mddb")  # or mddb.MDDB(path) to open an existing one

with db.edit(rationale="bought today") as edit:
    card = edit.create(
        title="Shed inventory",
        summary="Tools and equipment kept in the shed.",
        tags=["shed"],
        yaml={"location": "shed"},
        body="A wheelbarrow.",
    )

# read by id
card = db.read(card.id)

# mutate and write back
card.yaml["location"] = "barn"
with db.edit(rationale="moved to barn") as edit:
    edit.update(card, summary="Tools and equipment, moved to the barn.")
```

`db.edit()` is the only mutation primitive. Batch many mutations into one commit + one SQLite transaction. A body exception inside the `with` block discards the buffer; on-disk state is unchanged.

```python
with db.edit(rationale="bulk import") as edit:
    for item in ["fridge", "shed", "loft"]:
        edit.create(title=item.title(), summary=f"contents of the {item}")

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
