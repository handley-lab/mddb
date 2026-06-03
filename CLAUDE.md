# CLAUDE.md

Guidance for Claude Code working in this repository.

`mddb` is a Python library: a minimal YAML-frontmatter + markdown-body card substrate. Cards live as `.md` files in a directory under git; a SQLite index outside the directory (at `~/.cache/mddb/`) provides fast structured + full-text queries; rationales live in commit messages. The substrate has no domain knowledge — no `card_type`, no `status`, no `due`. The **substrate filing vocabulary** is `id`, `title`, `summary`, `relpath`, and `tags` — these are how the substrate identifies and slices cards (see the "Card format", "Directories and slugs", and "Tags" sections below for each one's semantics). Anything heavier (inventories, GTD, anything else) is layer code or, more usually, Alan reasoning in his REPL.

First consumer: `alan` working in a persistent Python REPL. `import mddb; db = mddb.MDDB(path)` opens an existing deck; `mddb.MDDB.init(path)` bootstraps a fresh one. There is no MCP server. There is no CLI. There is no TUI. There is no validation boundary — Alan constructs the calls.

## Philosophy

These rules bound this codebase. They are themselves bound by lean code — don't apply them to absurdity.

- **Compose, don't wrap.** This is the load-bearing principle. The library exists to give an LLM agent (and a human) versatile composable machinery — not to hand them a curated UX. Alan writes Python. Alan writes SQL. The substrate exposes `self.conn` directly rather than wrapping it in a filter DSL; YAML loads via PyYAML's defaults; `history()` returns `list[dict]` rather than a `Commit` class; mutation verbs return real `Card` objects you can mutate and pass back. Every helper that intermediates between Alan and the raw primitive is a thing that can get in his way the next time he needs to do something we didn't pre-imagine. When in doubt, expose the primitive; let the caller compose.

  *Corollary — sugar undermines its own existence.* If a safer primitive is the only path for a real failure mode (e.g. transactional batching to prevent half-committed loops), don't expose a simpler one-shot sugar alongside it. Callers reach for the sugar, the failure mode reappears, and the safer primitive is dead weight. The bare `MDDB.create/update/delete/move` verbs were removed for exactly this reason: `db.editor(rationale=...)` is the only mutation-primitive *factory*, full stop. It returns a `_Editor` whose methods (`create`/`read`/`update`/`delete`/`move`/`edit`) are the operations. The doorway is noun-shaped (an `editor`); the operations on it are verb-shaped.

- **Lean code wins ties.** Minimum lines, minimum dependencies, minimum abstraction. Three similar lines beat one premature abstraction. Audit for dead code regularly. Single-caller helpers especially must earn their name; if `_init_git` or `_write_atomic` is called from one place, inline it.

- **No defensive programming.** Trust internal code. Never `try; except: pass`. Never `dict.get(key)` to paper over a key the rest of the code assumes is there. Never `or []` to substitute a fallback. If a caller passes something the function doesn't handle, the function crashes with the natural exception and the caller fixes it. The native Python traceback is the error UI.

  *And it bites downstream.* A defensive precondition check forces every legitimate code path to satisfy it, including your own helpers. Removing the `.git` existence check from `MDDB.__init__` was what let `MDDB.init()` construct the instance first and use `self._git` for all four bootstrap commands instead of bare `subprocess.run`. Defensive checks create chicken-and-egg constraints that force you to break your own abstractions to work around them.

- **Crash on drift.** When code parses a value (a config key, a YAML field your layer wrote, a filter dict op name), enumerate the known cases and crash on anything else. No `default` branches, no silent fallthrough. This applies to values your code parses; it does not licence validating every aspect of every persisted object.

- **No over-engineering.** Don't add features, config, or abstractions until they're needed. Don't add a `verify_card_integrity()` for a class of failure that requires an attacker who already has filesystem access. The substrate's safety surface is "trust the OS and trust git" — anything beyond that is theatre.

- **No migration code.** Pre-alpha. No backwards-compatibility shims. If the on-disk layout changes, the change is the migration: bump the schema-version row in `meta`, the next open rebuilds the index from the cards on disk, done. Don't write code to read old schemas.

- **No MCP, no Pydantic, no validation theatre.** Alan is the agent constructing the calls; there is no untrusted boundary. The API takes plain Python types. Errors are plain Python exceptions. Add Pydantic and an MCP server when a real cross-process consumer needs them.

- **Iterative reviews have a stopping rule.** When a review escalates into deeper checks for drift that requires unusual operator behaviour to trigger, stop and call convergence. "Lean code" is a bound, not a soft preference; an APPROVED that adds 300 lines is worse than a NOT APPROVED at 200.

- **Boundaries translate errors, don't hide them.** The only real boundary is the disk and git. If git fails, `subprocess.CalledProcessError` propagates. If SQLite fails, `sqlite3.Error` propagates. We don't catch these to retranslate them; the caller sees the native exception with the native traceback.

- **Locality of schema knowledge.** Per-table SQL operations live in `_index.py` (next to the schema and `index_fields`), not scattered through `_core.py`. This isn't wrapping — there's no DSL, no class hierarchy, no validation; just named functions over a raw `sqlite3.Connection`. The `_core.py` orchestrator owns mutation ordering (filesystem → git → SQLite); `_index.py` owns "given conn and card/path, mutate cache tables." If you find yourself writing `conn.execute("INSERT INTO entries...")` outside `_index.py`, move it.

- **Name primary APIs for the thing the caller holds.** When a method returns a context manager whose methods are the operations, the *factory* names the object the caller binds (`editor = db.editor(...)`), and the *operations* on it are the verbs (`editor.create`, `editor.edit`, `editor.update`). `db.editor()` instead of `db.transaction()`: the second sounds like banking ceremony with failure machinery; the first reads as the natural thing — you ask for an editor, you get one. Jargon belongs in internals (`_Editor` privately implements an atomic batch); the public surface uses words the caller already thinks in. Verb-returns-noun is well established in Python (`tempfile.NamedTemporaryFile()`, `sqlite3.connect()` returning a `Connection`).

- **Optional types should reflect real None states, not "I might not pass this kwarg."** `relpath: str = ""` beats `relpath: str | None = None` when the empty string already means "no relpath given." `Optional` is Python idiom, but it's only honest when `None` is semantically distinct from a sensible default value of the type. Reach for the default value first; reach for `| None` only when the None state is load-bearing.

## Design shape

- Files are truth; SQLite is a derived cache at `~/.cache/mddb/<sha1(abs-path)>/index.sqlite`; git records rationale/history.
- Core substrate has no domain fields. The substrate filing vocabulary is `id`, `title`, `summary`, `relpath`, and `tags` — structures the substrate provides; *names* (what `area/work` or `inventory` means) stay user-owned. Flat YAML.
- Mutation order: filesystem → git → SQLite. SQLite failures propagate; the cache may be left stale. Delete the cache file manually if you want a fresh one.
- Subprocess git via `subprocess.run(["git", ...], check=True)`. No GitPython (it's itself a subprocess wrapper — adds API cost without value). No libgit2 (gtd dropped it after fighting it). If bulk operations ever bottleneck, dulwich (pure-Python git) is the leanest alternative — not another wrapper.
- No MCP, no CLI, no GUI in the prototype.

## Architecture

### On disk

```
<path>/
  <slugify(title)>.md   cards at root by default; caller may opt into a richer relpath like inventory/fridge.md
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
    def read(self, card_id: str) -> Card: ...
    def list(self) -> list[dict]: ...  # [{id, title, summary}, ...] — progressive disclosure
    def history(self, card_id: str) -> list[dict]: ...
    def editor(self, *, rationale: str) -> _Editor: ...
    conn: sqlite3.Connection  # exposed; write SQL against the schema below

class _Editor:  # private; only reachable via the with-block from MDDB.editor
    def create(self, *, title: str, summary: str, yaml: dict | None = None,
               body: str = "", relpath: str = "",
               tags: Sequence[str] | None = None) -> Card: ...
    def read(self, card_id: str) -> Card: ...
    def update(self, card: Card, *, summary: str,
               tags: Sequence[str] | None = None) -> Card: ...
    def delete(self, card_id: str) -> None: ...
    def move(self, card_id: str, new_relpath: str) -> None: ...
    def edit(self, card_id: str, old: str, new: str, *,
             replace_all: bool = False) -> int: ...

class Card:
    yaml: dict
    body: str
    @property
    def id(self) -> str: return self.yaml["id"]
    @property
    def title(self) -> str: return self.yaml["title"]
    @property
    def summary(self) -> str: return self.yaml["summary"]
    @property
    def tags(self) -> list: return self.yaml["tags"]  # raises if absent (normal for untagged cards)
```

`MDDB(path)` opens the mddb at `path`; mutation operations fire native `subprocess.CalledProcessError` from git if there's no repo there. `MDDB.init(path)` bootstraps a fresh one (`mkdir -p`, `git init`, commits a `.gitignore` containing `*.tmp`). Two explicit entry points — no silent "create if missing." `Card` is composition, not a dict subclass: callers write `card.yaml["key"] = value` and `card.body = "..."`. Equality, pickling, and hashing follow ordinary attribute semantics.

### Card format

```markdown
---
id: <uuid-v4>
<arbitrary flat yaml>
---

<markdown body>
```

`id`, `title`, and `summary` are the **disclosure trio** — the progressive-disclosure levels (see "Progressive disclosure" below). All three are present on every card created through the API: `editor.create(title=..., summary=..., ...)` requires the two disclosure kwargs and inserts them into the YAML, plus a UUIDv4 `id` if the caller didn't supply one. `editor.update(card, summary=...)` also requires `summary` so the caller must make a deliberate decision about disclosure currency at every structured mutation (pass the existing value to acknowledge it's still accurate, or pass a new one to re-summarise). For mechanical body-only edits (typo fixes, link updates), the substrate offers `editor.edit(card_id, old, new)` which does *not* force the disclosure check — see "Body edits" below for the duality. `Card.id`/`Card.title`/`Card.summary` use direct dict access and raise `KeyError` only if a card from a different source (manual file write, external import) lacks them — a missing disclosure key signals drift.

`tags` is also a substrate filing key but with **different absence semantics**: untagged cards routinely omit the `tags` key, so `Card.tags` raising `KeyError` is *normal* for untagged cards, not drift. Callers who treat tags as optional use `card.yaml.get("tags", [])`.

`MDDB.list()` returns `None` for missing title/summary values via nullable `entries.title` / `entries.summary` columns (populated from `card.yaml.get("title")` / `.get("summary")` at insert time) to keep incomplete cards visible during overviews. YAML is loaded via `yaml.safe_load` (PyYAML defaults). Bare ISO dates parse as `datetime.date`; if you want date strings for lexicographic comparison, quote them in the source YAML.

On the write path, `_Editor.create` constructs YAML in **canonical key order**: `id`, `title`, `summary`, `tags` (when present), then the caller's remaining `yaml=` keys in their original relative order. The on-disk frontmatter is scannable: the first lines tell you what the card is. `_Editor.update` does NOT re-canonicalise existing YAML order (it preserves what's already on disk).

Two PyYAML default overrides on the write path (`yaml.safe_dump(data, sort_keys=False, allow_unicode=True)`): `sort_keys=False` so cards retain the field order the caller wrote (alphabetised output reorders frontmatter on every update, which is jarring in git diffs); `allow_unicode=True` so international characters aren't escaped into `\\uXXXX` sequences in the on-disk YAML.

### Directories and slugs

Title drives the default file slug; directory is the caller's choice via `relpath`. Resolution rules (suffix-decides):

- `relpath=""` (default) → `<slugify(title)>.md` (flat at root).
- `relpath` ends in `.md` → used verbatim as the filename.
- otherwise → treated as a directory; substrate appends `<slugify(title)>.md` inside it.

So `relpath="inventory"` and `relpath="inventory/"` both produce `inventory/<slug>.md`. A caller who wants a custom filename types the `.md` explicitly. Slug generation uses `python-slugify`'s defaults.

Title and directory are **orthogonal**: title is *what the card is*; directory is *where the caller chose to put it*. Title changes do not move the file — `editor.update()` rewrites in place. To rename the file, call `editor.move(card_id, new_relpath)` explicitly (`git mv` + index update; id stays the same so history follows). `move` takes an exact `.md` filename — it does *not* apply suffix-decides (unlike `relpath=` on `create`); a non-`.md` target raises `ValueError` because the cache rebuild only indexes `*.md` files and would lose the card. Collisions on the resolved relpath raise `FileExistsError`; the caller resolves by changing the title or passing an explicit `relpath`.

### Tags

`tags` is a YAML list of strings: `tags: ["area/work", "topic/cosmology"]`. The substrate stores tags as flat strings and indexes them via `entry_fields`; **hierarchy via `/` is convention only** — the substrate doesn't know what `area/work` "means," only that it's a string.

The library-and-card-catalogue analogy: `relpath` is the shelves (one location per book); `tags` is the card catalogue (multiple cross-refs per book). Both are filing structure the substrate provides. The names (`inventory/kitchen`, `area/work`) are user-owned domain choices.

`Card.tags` is direct access (`self.yaml["tags"]`) — raises `KeyError` for untagged cards. Use `card.yaml.get("tags", [])` if absence should be treated as the empty list at the call site.

`_Editor.create` and `_Editor.update` accept a three-state `tags: Sequence[str] | None = None` kwarg:

- `tags=None` (default): no override. On `create`, the caller's `yaml["tags"]` is preserved if present. On `update`, `card.yaml["tags"]` is left as-is (in-place mutations the caller made before calling `update` persist).
- `tags=()` / `tags=[]` (empty sequence): explicit clear. On `create`, no `tags` key on disk even if `yaml={"tags": [...]}` was passed. On `update`, the `tags` key is removed from `card.yaml`.
- `tags=["x", "y"]` (non-empty): replace.

The "omit empty" rule applies to the `tags=` kwarg only — raw `yaml={"tags": []}` is preserved verbatim (substrate doesn't normalise raw YAML input).

Hierarchical queries are caller-composed via the existing `entry_fields` index. Descendants:

```python
db.conn.execute(
    "SELECT entries.id FROM entries JOIN entry_fields f "
    "ON f.entry_rowid = entries.rowid "
    "WHERE f.key = 'tags' AND f.value_str LIKE 'area/%'",
)
```

Self plus descendants: `f.value_str = 'area' OR f.value_str LIKE 'area/%'`. Substring: `f.value_str LIKE '%work%'`. Shell-style: `f.value_str GLOB 'area/*'`. Regex via SQLite's `REGEXP` operator is available if the caller registers a function on `db.conn` themselves (one line: `conn.create_function("regexp", 2, lambda p, v: bool(re.search(p, v)))`) — substrate doesn't preinstall, matching the compose-don't-wrap stance.

### Mutation flow

All mutation flows through `db.editor()`. The commit phase, on clean `__exit__`:

1. `git rm` staged deletes.
2. `git mv` staged moves (parent dirs created as needed).
3. Write staged creates/updates via temp file + `os.replace`, then `git add`.
4. One `git commit -m <rationale>`.
5. SQLite insert/update/delete inside `with self.conn:`. If this raises, `sqlite3.Error` propagates and the cache may be left stale.

The next `MDDB(path)` opens the cache if `meta.schema_version` matches; if missing or different version, it rebuilds from `.md` files on disk. There is no automatic stale-cache detection. If a SQLite mutation fails and you want a fresh index, `rm ~/.cache/mddb/<sha1(abs-path)>/index.sqlite` and reopen.

### SQLite

```sql
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE entries(rowid INTEGER PRIMARY KEY, id TEXT UNIQUE, relpath TEXT UNIQUE, title TEXT, summary TEXT, yaml_text TEXT, body TEXT);
CREATE TABLE entry_fields(entry_rowid INTEGER, key TEXT, value_str TEXT, value_num REAL);
CREATE VIRTUAL TABLE entries_fts USING fts5(yaml_text, body, content='entries', content_rowid='rowid');
```

`title` and `summary` are promoted to dedicated nullable columns on `entries` because they are part of the substrate filing/disclosure vocabulary; `db.list()` reads them directly. `entry_fields` indexes every *other* top-level scalar and list-of-scalars from `card.yaml` (notably `tags`) but skips `title` and `summary` (they have their own columns).

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

### Edits

`db.editor(rationale=...)` is the only mutation primitive. It returns a context manager that buffers `create`/`update`/`delete`/`move`/`edit` and materialises them as one git commit + one SQLite transaction on clean `__exit__`. `editor.read()` sees the staged buffer; it is not itself buffered. On body exception, the buffer is dropped and the mddb root is untouched.

```python
with db.editor(rationale="bulk import of inventory cards") as editor:
    a = editor.create(title="Fridge", summary="...", body="...")
    b = editor.create(title="Shed",   summary="...", body="...")
    editor.update(a, summary="...")
    editor.move(b.id, "inventory/shed.md")
```

Clean exit produces one commit covering all four operations. A body exception inside the `with` block produces no commit and no on-disk change.

The editor rationale is the single commit message for the whole batch; there is no per-operation rationale. `editor.update(card, summary=...)` requires `summary` so the caller acknowledges the disclosure decision at every structured mutation.

Returned `Card` objects are deep copies; mutate them freely without affecting the staged buffer. Mutation must go through `editor.update()` to persist.

Operation collapse in a single editor: create + update → one create with mutated card; create + delete → no-op; update + delete → one delete; move + update → staged update at the new relpath; move-away-then-back → no-op; double-create at the same id → `RuntimeError`. Modify-after-delete raises `KeyError`.

Reads (`db.read`, `db.list`, `db.history`, raw `db.conn` SELECTs) remain available during an active editor and see committed state, not the staged buffer. Once the commit phase begins, git/SQLite failures propagate native exceptions; the working tree or cache may be left dirty, matching the policy in "Mutation flow" above.

Nested editors raise `RuntimeError`. An editor is single-shot: after exit (clean, body exception, or commit-phase failure) it cannot be reused.

### Body edits

Two paths for mutating a card's body, with different disclosure semantics:

- **Disciplined path — `editor.update(card, summary=...)`.** Requires the caller to pass `summary` on every call. This is the *disclosure-currency check*: the caller has to make a deliberate decision about whether the existing summary still reflects what the card is about (pass it through unchanged) or whether the change warrants a new summary. Use this for structured changes that might alter the card's gist.

- **Mechanical path — `editor.edit(card_id, old, new, *, replace_all=False) -> int`.** Body-only find/replace. Preserves title, summary, tags, relpath, and body content outside the match region. Mirrors `loop.edit(path, old, new)` semantics. Raises `ValueError` on empty `old`, `old` not found, or `old` found multiple times without `replace_all=True` (unless `old == new`, which short-circuits as a no-op returning the match count without staging or raising). Returns the replacement count. Does *not* force a disclosure check — the targeted use case is mechanical edits (typo fixes, variable renames, link updates) where re-summarising is overkill.

The substrate enforces neither — both paths are available; the caller picks. This is a deliberate policy retreat from the previous rule's universality. `editor.edit` could in principle be used to replace a substantive claim and leave a stale summary; the substrate cannot tell trivial from semantic body changes. Use `update` when the change might affect what the card *is about*; use `edit` for typo fixes and mechanical renames.

**Footgun — `edit` then `update` with a stale snapshot.** `editor.update` replaces the staged card *wholesale* with the caller-supplied `Card`. So if a caller mixes `editor.edit` and `editor.update` on the same card within one editor session and reuses a stale snapshot, the `update` silently overwrites the `edit`-staged body:

```python
card = db.read(card_id)  # snapshot, body="foo"
with db.editor(rationale="...") as editor:
    editor.edit(card.id, "foo", "bar")           # stages body="bar"
    editor.update(card, summary=card.summary)    # overwrites with stale body="foo"
```

To compose them safely, re-read the card via `editor.read(card_id)` after any body-staging operation before passing it to `editor.update`:

```python
with db.editor(rationale="...") as editor:
    editor.edit(card.id, "foo", "bar")
    fresh = editor.read(card.id)
    editor.update(fresh, summary=fresh.summary)
```

The substrate does not auto-detect this — distinguishing an `edit`-staged overwrite from a legitimate update-after-update would require per-staged-record provenance, out of scope for the primitive itself.

### Progressive disclosure

Three disclosure levels — `id` → `title` → `summary` → full card. This is how an agent navigates a large mddb without burning tokens on every body.

```python
# Level 1: headlines — just id and title
for cid, title in db.conn.execute("SELECT id, title FROM entries"):
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

`id`, `title`, `summary`, `relpath`, and `tags` are substrate filing keys (privileged at the substrate level). Any other field (e.g. `due`, `status`, `card_type`, `location`) is layer-defined and reached via raw SQL through `entry_fields`.

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
dependencies = ["pyyaml>=6.0", "python-slugify>=8.0"]
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
- `relpath` must be relative, canonical (no `.` or `..` path parts), inside the mddb root after symlink resolution, AND textually equal to its resolved relative path (no symlink aliases). Validated at `create` and `move`; other operations preserve the existing relpath. Violations raise `ValueError` — the substrate refuses to store a relpath the cache rebuild can't reproduce.
- SQLite is disposable; `.md` files and git are truth.
- Concurrent writers from another process are outside the prototype contract.
