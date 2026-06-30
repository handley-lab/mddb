# mddb

A minimal YAML-frontmatter + markdown-body card substrate for agentic and human knowledge work.

Cards live as `.md` files in a directory under git. A derived SQLite index at `~/.cache/mddb/` provides fast structured + full-text queries. Rationales live in commit messages. The substrate has no domain knowledge — flat YAML. The substrate filing vocabulary is `id`, `title`, `summary`, `relpath`, and `tags` (structures the substrate provides; the names are user choice). Anything heavier is layer code or an agent reasoning in a REPL.

## Quickstart

```python
import mddb

db = mddb.MDDB.init("~/my-mddb")  # or mddb.MDDB(path) to open an existing one

with db.editor(rationale="bought today") as editor:
    card = editor.create(
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
with db.editor(rationale="moved to barn") as editor:
    editor.update(card, summary="Tools and equipment, moved to the barn.")
```

`db.editor()` is the only mutation primitive. Batch many mutations into one commit + one SQLite transaction. A body exception inside the `with` block discards the buffer; on-disk state is unchanged.

```python
with db.editor(rationale="bulk import") as editor:
    for item in ["fridge", "shed", "loft"]:
        editor.create(title=item.title(), summary=f"contents of the {item}")

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

## MCP server (optional)

For cross-process agents (Codex, Clawde), the `mddb[mcp]` extra ships a FastMCP
server. One server serves many decks — each tool call passes the deck path.

```bash
pip install mddb[mcp]
claude mcp add mddb -- mcp-mddb
```

Two tools: `read(deck, op=list|get|history|query|blob, ...)` and
`editor(deck, rationale, ops)` (a JSON-array of operations applied as one
commit). For example, `read(deck="/home/me/finance", op="list")` or
`editor(deck="/home/me/home", rationale="...", ops='[{"op":"create",...}]')`.
A non-existent/non-deck path errors clearly rather than reading empty; bootstrap
a new deck with `editor(deck, rationale, ops='[{"op":"init"}]')`. The core
`import mddb` does not pull in the MCP dependency.

## Merge driver (optional)

For multi-agent / offline-then-sync workflows, divergent branches reconcile by
**merge** with a custom git driver (`mddb-card`) so cards merge *semantically*
rather than by dumb line-merge: `tags` are three-way set-merged (deletions win,
additions union, no resurrection), the body uses git's line-level three-way
merge, `id` is immutable, and other frontmatter scalars conflict only on genuine
divergence. Registration is opt-in operator policy (like LFS):

```python
from mddb._merge import install
install("/home/me/finance")   # sets the per-clone driver config + .gitattributes
# then commit the .gitattributes once
```

`mddb._merge.conflict_rationales(root, relpath)` returns each side's commit
rationales for a card conflicted mid-merge — intent for an agent resolving it.

While a *frontmatter* conflict is unresolved the card is not valid YAML, so
`Card.from_file`, `db.read`, and the cache rebuild raise on it — a deck
mid-unresolved-merge must be resolved (or the merge aborted) before normal use.
A *body-only* conflict keeps valid frontmatter, so the card still reads and
rebuilds (markers indexed as body text).

## Status

Prototype. Linux only. Concurrent mddb writers (multiple processes / MCP
agents) are serialised by a short `.git/mddb.lock` and a base-vs-HEAD conflict
check — a stale write raises `mddb.ConflictError` (re-read and retry) rather
than silently clobbering; capture `base = db.head()` with your read and later
pass `db.editor(base=base)` to guard that read→write span. Raw external `git` commits and uncommitted editor edits are
not yet guarded.
