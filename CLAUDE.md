# CLAUDE.md

Guidance for Claude Code working in this repository.

`mddb` is a Python library: a minimal YAML-frontmatter + markdown-body card substrate. Cards live as `.md` files in a directory under git; a SQLite index outside the directory (at `~/.cache/mddb/`) provides fast structured + full-text queries; rationales live in commit messages. The substrate has no domain knowledge — no `card_type`, no `status`, no `due`. The only privileged YAML keys are **`id`, `title`, and `summary`** (the three disclosure levels — see "Progressive disclosure" below). Anything heavier (inventories, GTD, anything else) is layer code or, more usually, Alan reasoning in his REPL.

First consumer: `alan` working in a persistent Python REPL. `import mddb; db = mddb.MDDB(path)`. There is no MCP server. There is no CLI. There is no TUI. There is no validation boundary — Alan constructs the calls.

## Philosophy

These rules bound this codebase. They are themselves bound by lean code — don't apply them to absurdity.

- **Compose, don't wrap.** This is the load-bearing principle. The library exists to give an LLM agent (and a human) versatile composable machinery — not to hand them a curated UX. Alan writes Python. Alan writes SQL. The substrate exposes `self.conn` directly rather than wrapping it in a filter DSL; YAML loads via PyYAML's defaults; `history()` returns `list[dict]` rather than a `Commit` class; mutation verbs return real `Card` objects you can mutate and pass back. Every helper that intermediates between Alan and the raw primitive is a thing that can get in his way the next time he needs to do something we didn't pre-imagine. When in doubt, expose the primitive; let the caller compose.

- **Lean code wins ties.** Minimum lines, minimum dependencies, minimum abstraction. Three similar lines beat one premature abstraction. Audit for dead code regularly.

- **No defensive programming.** Trust internal code. Never `try; except: pass`. Never `dict.get(key)` to paper over a key the rest of the code assumes is there. Never `or []` to substitute a fallback. If a caller passes something the function doesn't handle, the function crashes with the natural exception and the caller fixes it. The native Python traceback is the error UI.

- **Crash on drift.** When code parses a value (a config key, a YAML field your layer wrote, a filter dict op name), enumerate the known cases and crash on anything else. No `default` branches, no silent fallthrough. This applies to values your code parses; it does not licence validating every aspect of every persisted object.

- **No over-engineering.** Don't add features, config, or abstractions until they're needed. Don't add a `verify_card_integrity()` for a class of failure that requires an attacker who already has filesystem access. The substrate's safety surface is "trust the OS and trust git" — anything beyond that is theatre.

- **No migration code.** Pre-alpha. No backwards-compatibility shims. If the on-disk layout changes, the change is the migration: bump the schema-version row in `meta`, the next open rebuilds the index from the cards on disk, done. Don't write code to read old schemas.

- **No MCP, no Pydantic, no validation theatre.** Alan is the agent constructing the calls; there is no untrusted boundary. The API takes plain Python types. Errors are plain Python exceptions. Add Pydantic and an MCP server when a real cross-process consumer needs them.

- **Iterative reviews have a stopping rule.** When a review escalates into deeper checks for drift that requires unusual operator behaviour to trigger, stop and call convergence. "Lean code" is a bound, not a soft preference; an APPROVED that adds 300 lines is worse than a NOT APPROVED at 200.

- **Boundaries translate errors, don't hide them.** The only real boundary is the disk and git. If git fails, `subprocess.CalledProcessError` propagates. If SQLite fails, `sqlite3.Error` propagates. We don't catch these to retranslate them; the caller sees the native exception with the native traceback.

## Design shape

- Files are truth; SQLite is a derived cache at `~/.cache/mddb/<sha1(abs-path)>/index.sqlite`; git records rationale/history.
- Core substrate has no domain fields. The only privileged YAML keys are `id`, `title`, and `summary` (the three progressive-disclosure levels). Flat YAML.
- Mutation order: filesystem → git → SQLite. SQLite failures propagate; the cache may be left stale. Delete the cache file manually if you want a fresh one.
- Subprocess git via `subprocess.run(["git", ...], check=True)`. No GitPython (it's itself a subprocess wrapper — adds API cost without value). No libgit2 (gtd dropped it after fighting it). If bulk operations ever bottleneck, dulwich (pure-Python git) is the leanest alternative — not another wrapper.
- No MCP, no CLI, no GUI in the prototype.

## Architecture

### On disk

```
<path>/
  <id>.md        cards (caller may opt into a richer relpath like inventory/fridge.md)
  .git/
```

```
~/.cache/mddb/<sha1(abs-path)>/index.sqlite
```

The mddb directory stays clean of mddb-specific cruft. The cache is fully derived and can be deleted at any time; the next `MDDB(path)` rebuilds it.

### API surface

```python
class MDDB:
    def __init__(self, path: Path | str): ...
    def create(self, *, title: str, summary: str, yaml: dict | None = None,
               body: str = "", rationale: str, relpath: str | None = None) -> Card: ...
    def read(self, card_id: str) -> Card: ...
    def update(self, card: Card, *, summary: str, rationale: str) -> Card: ...
    def delete(self, card_id: str, *, rationale: str) -> None: ...
    def move(self, card_id: str, new_relpath: str, *, rationale: str) -> None: ...
    def list(self) -> list[dict]: ...  # [{id, title, summary}, ...] — progressive disclosure
    def history(self, card_id: str) -> list[dict]: ...
    conn: sqlite3.Connection  # exposed; write SQL against the schema below

class Card:
    yaml: dict
    body: str
    @property
    def id(self) -> str: return self.yaml["id"]
    @property
    def title(self) -> str: return self.yaml["title"]
    @property
    def summary(self) -> str: return self.yaml["summary"]
```

`MDDB(path)` opens an existing mddb directory or creates a fresh one (with `git init` + `.gitignore`). One stderr line on creation so a typoed path is visible. `Card` is composition, not a dict subclass: callers write `card.yaml["key"] = value` and `card.body = "..."`. Equality, pickling, and hashing follow ordinary attribute semantics.

### Card format

```markdown
---
id: <uuid-v4>
<arbitrary flat yaml>
---

<markdown body>
```

`id`, `title`, and `summary` are the three substrate-privileged keys (see "Progressive disclosure" below). All three are present on every card created through the API: `MDDB.create(title=..., summary=..., ...)` requires the two disclosure kwargs and inserts them into the YAML, plus a UUIDv4 `id` if the caller didn't supply one. `MDDB.update(card, summary=..., ...)` also requires `summary` so the caller must make a deliberate decision about disclosure currency at every mutation (pass the existing value to acknowledge it's still accurate, or pass a new one to re-summarise). `Card.id`/`Card.title`/`Card.summary` use direct dict access and raise `KeyError` only if a card from a different source (manual file write, gtd import) lacks them. `MDDB.list()` returns `None` for missing values via `LEFT JOIN` to keep incomplete cards visible during overviews. YAML is loaded via `yaml.safe_load` (PyYAML defaults). Bare ISO dates parse as `datetime.date`; if you want date strings for lexicographic comparison, quote them in the source YAML.

Two PyYAML default overrides on the write path (`yaml.safe_dump(data, sort_keys=False, allow_unicode=True)`): `sort_keys=False` so cards retain the field order the caller wrote (alphabetised output reorders frontmatter on every update, which is jarring in git diffs); `allow_unicode=True` so international characters aren't escaped into `\\uXXXX` sequences in the on-disk YAML.

### Directories and slugs

Title drives the default file slug; directory is the caller's choice via `relpath`. Resolution rules:

- `relpath=None` → `<slugify(title)>.md` (flat at root).
- `relpath="inventory/"` (trailing slash) → directory; substrate appends `<slugify(title)>.md`.
- `relpath="inventory/fridge"` → substrate appends `.md`.
- `relpath="inventory/fridge.md"` → used verbatim.

`slugify()` is in `mddb.card`: lowercases, replaces runs of non-word characters with hyphens, returns `"untitled"` for empty input.

Title and directory are **orthogonal**: title is *what the card is*; directory is *where the caller chose to put it*. Title changes do not move the file — `db.update()` rewrites in place. To rename the file, call `db.move(card_id, new_relpath, rationale=...)` explicitly (`git mv` + index update; id stays the same so history follows). Collisions on the resolved relpath raise `FileExistsError`; the caller resolves by changing the title or passing an explicit `relpath`.

### Mutation flow

1. Build new card bytes + commit message in memory.
2. Create / update: write to a sibling temp file, `os.replace` to relpath. Delete: skip.
3. Create / update: `git add -- <relpath>`. Delete: `git rm -- <relpath>`. Then `git commit -m <rationale>`.
4. Insert / update / delete the matching row in SQLite inside `with self.conn:`. If this raises, the `sqlite3.Error` propagates; the cache may be left in a stale state.

The next `MDDB(path)` opens the cache if `meta.schema_version` matches; if the cache file is missing or carries a different version, it rebuilds from `.md` files on disk. Other SQLite failures (corruption, missing tables) propagate as `sqlite3.Error`. There is no automatic stale-cache detection. If a SQLite mutation fails and you want a fresh index, `rm ~/.cache/mddb/<sha1(abs-path)>/index.sqlite` and reopen. Git and SQLite failures propagate; if `git commit` fails after `os.replace`, the working tree is dirty and the caller resolves with the native exception.

### SQLite

```sql
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE entries(rowid INTEGER PRIMARY KEY, id TEXT UNIQUE, relpath TEXT UNIQUE, yaml_text TEXT, body TEXT);
CREATE TABLE entry_fields(entry_rowid INTEGER, key TEXT, value_str TEXT, value_num REAL);
CREATE VIRTUAL TABLE entries_fts USING fts5(yaml_text, body, content='entries', content_rowid='rowid');
```

SQLite default journal mode (single-writer). FTS sync via the standard external-content triggers (AI / AD / AU with the `'delete'` row idiom). `entries.body` and `entries.yaml_text` duplicate disk content for FTS; reads always go to disk.

### Querying

There is no `search` / `compile_filter`. The substrate exposes `db.conn` and the schema; callers compose SQL directly. Examples:

```python
# full-text
ids = [r[0] for r in db.conn.execute(
    "SELECT id FROM entries WHERE rowid IN (SELECT rowid FROM entries_fts WHERE entries_fts MATCH ?)",
    ("wheelbarrow",),
)]

# field filter
ids = [r[0] for r in db.conn.execute(
    "SELECT entries.id FROM entries JOIN entry_fields f ON f.entry_rowid = entries.rowid "
    "WHERE f.key = ? AND f.value_str = ?",
    ("tags", "shed"),
)]

cards = [db.read(i) for i in ids]
```

If a particular query pattern shows up repeatedly in caller code, abstract it *in the caller*, not in the substrate.

### Progressive disclosure

Three disclosure levels — `id` → `title` → `summary` → full card. This is how an agent navigates a large mddb without burning tokens on every body.

```python
# Level 1: headlines — just id and title
for cid, title in db.conn.execute(
    "SELECT entries.id, f.value_str FROM entries "
    "LEFT JOIN entry_fields f "
    "  ON f.entry_rowid = entries.rowid AND f.key = 'title'"
):
    ...

# Level 2: summary view — id, title, summary (this is what db.list() returns)
for entry in db.list():
    print(entry["id"], entry["title"], "—", entry["summary"])

# Level 3: full card
card = db.read(some_id)
card.title       # raises KeyError if 'title' is missing
card.summary     # raises KeyError if 'summary' is missing
card.body        # markdown
card.yaml        # full dict
```

`id`, `title`, and `summary` are the three privileged YAML keys (substrate-level). Any other field (e.g. `tags`, `due`, `location`) is layer-defined and reached via raw SQL through `entry_fields`.

## Style

- Comments explain *why*, not *what*. Names explain *what*. If you find yourself writing a comment to explain a name, change the name instead.
- No inline `#` comments. For **public** modules, classes, methods, and functions: Google-style docstrings are required (ruff `D` enforces this — see the Linting section). The Args/Returns/Raises sections necessarily restate the signature; that's the point — they feed `inspect.getdoc()` for autogen. For **private** code (underscore-prefixed) and **tests**: docstrings only when the why is non-obvious. Never duplicate CLAUDE.md.
- No banner-style separators (`# --- Section ---`). If a file needs sections it should be split.
- Comment / docstring rules apply to source AND tests. Rename the test, don't comment it.
- No comments referring to history (`# ported from gtd`, `# matches Y implementation`).
- Self-documenting names. Rename `c` to `card`. Rename `p` to `path` only if the longer name fits — single-letter names are fine in tight scopes.

## Working style

- **Plans front-load decisions.** Once a plan is agreed, all decisions have been made. Don't ask the user questions the plan answers. Don't pause at phase boundaries. Don't present review findings and ask "want me to fix these?" — fix them.
- **Don't defer work.** Asking a question is friction, not a completion point. If the task is local and reversible, keep going.
- **A plan is a contract.** Steps in the plan are mandatory.
- **Never chain commands with `&&` or `;`.** Chained commands trigger approval prompts. Run each as a separate Bash tool call. Use absolute paths instead of `cd`.
- **Save project rules here, not in personal memory.** Project conventions belong version-controlled and shared.

## Dependencies

```toml
dependencies = ["pyyaml>=6.0"]
dev = ["pytest>=8.0", "ruff>=0.5", "pre-commit>=3.0", "tomli>=2.0"]
```

stdlib `sqlite3`, `subprocess` (for git), `pathlib`, `uuid`, `hashlib`, `os`. No GitPython, no Pydantic, no MCP SDK, no mypy, no pytest-cov. The dev extras (`pre-commit`, `tomli`) support the CI / version-check workflow under `.github/` and `.pre-commit-config.yaml`.

## Tests

```bash
pytest                # all tests
pytest tests/test_core.py::test_create  # single test
```

Tests construct a tempdir mddb per test and write rationales that explain the test case (`rationale="testing concurrent update collision"`), not placeholders. The real-world ingest against `~/inventories/` is manual: Alan and Will exercise it in a REPL session. No gated real-data e2e in pytest.

## Linting and formatting

```bash
ruff format
ruff check
```

`ruff` is configured in `pyproject.toml` with **one deliberate override**: the `D` (pydocstyle) rule set with `convention = "google"`. Public modules, classes, methods, and functions must carry a Google-style docstring so `inspect.getdoc()`-based autogeneration (e.g. LLM tool schemas) has parseable Args / Returns / Raises. Underscore-private modules (`_core.py`), tests, and scripts are exempt via `per-file-ignores`.

Other ruff defaults are left alone.

## Footguns

- `id` is immutable.
- `relpath` must not collide.
- SQLite is disposable; `.md` files and git are truth.
- Concurrent writers from another process are outside the prototype contract.
