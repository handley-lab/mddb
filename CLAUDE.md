# mddb

Correct, minimal documentation is best. Omission is preferable to an
unsupported or obsolete claim. Incorrect documentation is worst.

MDDB is a Python library for Git-backed YAML-frontmatter Markdown cards. Card
files and Git history are truth. SQLite under `~/.cache/mddb/` is a disposable
index. Domain semantics belong in callers.

## Public substrate

```python
db = mddb.MDDB(path)              # open an existing deck
db = mddb.MDDB.init(path)         # create a deck explicitly
db.read(card_id)                  # Card
db.list()                         # progressive-disclosure mappings
db.history(card_id)               # Git history mappings
db.at(card_id, sha)               # the card's bytes at a commit
db.head()                         # current deck HEAD
db.conn                           # sqlite3.Connection for composed SQL

with db.editor(rationale=reason, base=base) as editor:
    editor.create(...)
    editor.read(card_id)
    editor.update(card, summary=summary)
    editor.delete(card_id)
    editor.move(card_id, relpath)
    editor.edit(card_id, old, new, replace_all=False)
```

`db.editor()` is the only mutation primitive. A clean exit materializes one Git
commit and one SQLite transaction; an exception in the block discards staged
work. `base` protects the whole deck against concurrent MDDB commits and raises
`ConflictError` on drift. Raw external Git commits and uncommitted editor changes
do not take the MDDB lock.

`Card` is an ordinary object with mutable `yaml` and `body`, plus a derived
`blob: Path | None`. Its `id`, `title`, `summary`, `kind`, and `tags` properties
read frontmatter directly. Untagged cards may omit `tags`. `kind` names the
vocabulary whose verbs own the card and returns `None` when absent — a card no
layer owns is inert.

The index schema is authoritative in `src/mddb/schema.sql`. `mddb.SCHEMA_DOC`
documents the query surface. Expose `db.conn`; do not add a filter or search DSL.

## Invariants

- The substrate filing vocabulary is `id`, `title`, `summary`, `kind`,
  `relpath`, and `tags`. Do not add domain fields such as status or due dates.
  A key is filing vocabulary only when the substrate must answer it without
  interpreting the card: which card, where it is, whether it is worth opening,
  and which vocabulary owns it. `kind` carries no state within a domain — its
  values are named by callers and mddb never interprets them.
- Opening never creates. `MDDB.init` is the explicit bootstrap operation.
- Mutation order is filesystem, Git, then SQLite. A stale or schema-mismatched
  cache rebuilds from the locked Git HEAD.
- Card IDs are immutable. Relpaths are relative, canonical, inside the deck,
  and collision-free.
- A card may own one tracked non-Markdown sibling with the same exact stem.
  Blob discovery is filesystem-derived; blobs are not YAML fields.
- YAML loads with `yaml.CSafeLoader`; absence of libyaml fails at import. Writes
  preserve field order and Unicode.
- The merge driver is Python code registered through `install_global()` and
  `install()`. Keep the installed `mddb-merge` executable as thin Git plumbing,
  not a second semantic interface.
- `src/mddb/_mcp.py` and its console entry point are legacy transport code. Do
  not advertise, extend, or use them for new integrations; callers compose the
  Python API directly. Removing them is a separate behavioural change.

## Engineering rules

- Compose ordinary Python values, iterators, paths, SQL, and Git. Do not wrap
  primitives in convenience APIs that compete with the editor or connection.
- Validate only genuine boundaries and let unexpected internal state fail with
  its native exception. Do not add guessed defaults or catch errors that cannot
  be resolved locally.
- No migration or compatibility readers. Schema-version drift rebuilds the
  derived index from cards.
- Keep table-specific SQL in `_index.py`; `_core.py` owns mutation ordering.
- Public modules, classes, methods, and functions use Google-style docstrings as
  enforced by ruff. Private code and tests need documentation only for a
  non-obvious invariant. Do not add inline narration or history comments.
- Run `pytest`, `ruff check`, and `ruff format --check` for code changes. For
  documentation-only changes, verify examples and run `git diff --check`.

The optional MCP adapter remains in packaging today, but the core `import mddb`
must never import MCP or Pydantic. Packaging, publication, installation, and
merging are separate authorized operations.
