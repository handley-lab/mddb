# CLAUDE.md

Guidance for Claude Code working in this repository.

`mddb` is a Python library: a minimal YAML-frontmatter + markdown-body card substrate. Cards live as `.md` files in a directory under git; a SQLite index outside the directory (at `~/.cache/mddb/`) provides fast structured + full-text queries; rationales live in commit messages. The substrate has no domain knowledge — no `card_type`, no `status`, no `due`, no privileged keys other than `id`. Anything heavier (inventories, GTD, anything else) is layer code or, more usually, Alan reasoning in his REPL.

First consumer: `alan` working in a persistent Python REPL. `import mddb; db = mddb.MDDB(path)`. There is no MCP server. There is no CLI. There is no TUI. There is no validation boundary — Alan constructs the calls.

## Philosophy

These rules bound this codebase. They are themselves bound by lean code — don't apply them to absurdity.

- **Compose, don't wrap.** This is the load-bearing principle. The library exists to give an LLM agent (and a human) versatile composable machinery — not to hand them a curated UX. Alan writes Python. Alan writes SQL. The substrate exposes `self.conn` directly rather than wrapping it in a filter DSL; YAML round-trips through PyYAML's defaults rather than a custom loader; `history()` returns `list[dict]` rather than a `Commit` class; mutation verbs return real `Card` objects you can mutate and pass back. Every helper that intermediates between Alan and the raw primitive is a thing that can get in his way the next time he needs to do something we didn't pre-imagine. When in doubt, expose the primitive; let the caller compose.

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
- Core substrate has no domain fields beyond `id`. Flat YAML.
- Mutation order: filesystem → git → SQLite. If the SQLite step fails, unlink the cache; the next `MDDB(path)` rebuilds it.
- Subprocess git via `subprocess.run(["git", ...], check=True)`. No GitPython, no libgit2.
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
    def create(self, yaml: dict, body: str = "", *, relpath: str | None = None, rationale: str) -> Card: ...
    def read(self, card_id: str) -> Card: ...
    def update(self, card: Card, *, rationale: str) -> Card: ...
    def delete(self, card_id: str, *, rationale: str) -> None: ...
    def search(self, query: str, *, filter: dict | None = None, limit: int = 50) -> list[Card]: ...
    def list(self, filter: dict | None = None, limit: int = 500) -> list[Card]: ...
    def history(self, card_id: str) -> list[Commit]: ...

class Card:
    yaml: dict
    body: str
    @property
    def id(self) -> str: return self.yaml["id"]
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

`id` is required. Substrate generates UUIDv4 on create if absent. YAML is loaded by a CSafeLoader subclass with the implicit timestamp resolver removed — bare `2026-06-02` stays a string, lexicographically sortable. Otherwise PyYAML defaults.

### Directories and slugs

The substrate has no opinion about directory structure or slugs. `create(relpath=None)` writes to `<path>/<id>.md` (UUID-flat). `create(relpath="inventory/fridge.md")` writes to exactly that path. The caller decides; no inference from `yaml["doctype"]` or `yaml["title"]`. No `rename` verb — to move a card, `delete` + `create` with a new relpath. The id changes; rationales narrate the why.

### Mutation flow

1. Build new card bytes + commit message in memory.
2. Create / update: write to a sibling temp file, `os.replace` to relpath. Delete: skip.
3. Create / update: `git add -- <relpath>`. Delete: `git rm -- <relpath>`. Then `git commit -m <rationale>`.
4. Insert / update / delete the matching row in SQLite inside a single transaction. If this raises, unlink the cache file and re-raise.

The next `MDDB(path)` opens the cache only if `meta.schema_version` matches; otherwise (or if missing / unreadable) it rebuilds from `.md` files on disk. That is the only recovery the substrate has. Git and SQLite failures propagate; if `git commit` fails after `os.replace`, the working tree is dirty and the caller resolves with the native exception.

### SQLite

```sql
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE entries(rowid INTEGER PRIMARY KEY, id TEXT UNIQUE, relpath TEXT UNIQUE, yaml_text TEXT, body TEXT);
CREATE TABLE entry_fields(entry_rowid INTEGER, key TEXT, value_str TEXT, value_num REAL);
CREATE VIRTUAL TABLE entries_fts USING fts5(yaml_text, body, content='entries', content_rowid='rowid');
```

SQLite default journal mode (single-writer). FTS sync via the standard external-content triggers (AI / AD / AU with the `'delete'` row idiom). `entries.body` and `entries.yaml_text` duplicate disk content for FTS; reads always go to disk.

### Filter

```python
{"field": "<top-level-key>", "op": "eq|ne|lt|le|gt|ge|in|contains|like", "value": ...}
{"fts": "<query>"}
{"and": [...]}  {"or": [...]}  {"not": ...}
```

Top-level keys only. `compile_filter` is a pure function returning `(sql_where, params)`. Unknown op names crash.

## Style

- Comments explain *why*, not *what*. Names explain *what*. If you find yourself writing a comment to explain a name, change the name instead.
- No inline `#` comments. Docstrings only when the why is non-obvious (a hidden constraint, a subtle invariant, a workaround for a specific bug, behaviour that would surprise a reader). Never narrate what the code does; never restate the signature; never duplicate CLAUDE.md.
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
dev = ["pytest>=8.0", "ruff>=0.5"]
```

stdlib `sqlite3`, `subprocess` (for git), `pathlib`, `uuid`, `hashlib`, `os`, `shutil`. No GitPython, no Pydantic, no MCP SDK, no mypy, no pytest-cov.

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

`ruff` config lives in `pyproject.toml` with defaults — don't override without a reason.

## Footguns

- `id` is immutable.
- `relpath` must not collide.
- SQLite is disposable; `.md` files and git are truth.
- Concurrent writers from another process are outside the prototype contract.
