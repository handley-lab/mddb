import fcntl
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

import mddb
from mddb import Card, _index


def commit(root, message):
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", message], check=True)


def write_card(root, path, card_id, **fields):
    card = Card({"id": card_id, "title": path, "summary": "Fixture", **fields}, "Body")
    (root / path).write_text(str(card))


@pytest.mark.parametrize("kind", [None, "agent"])
def test_reader_uses_committed_revision_without_locks_or_cache(db, monkeypatch, kind):
    write_card(db.root, "role.md", "role", kind="agent")
    (db.root / "role.png").write_bytes(b"blob")
    commit(db.root, "First revision")
    revision = db.head()
    write_card(db.root, "role.md", "replacement")
    commit(db.root, "Second revision")
    (db.root / "role.md").unlink()
    db.conn.close()
    cache = _index.cache_path(db.root)
    cache.unlink()

    def forbidden(*args, **kwargs):
        pytest.fail("committed reader accessed a lock or cache")

    with _index.deck_lock(db.root):
        monkeypatch.setattr(fcntl, "flock", forbidden)
        monkeypatch.setattr(_index, "cache_path", forbidden)
        cards, rejected = mddb.read_committed_cards(db.root, revision, kind=kind)
    assert not cache.exists()
    assert rejected == {}
    assert cards["role.md"][0].id == "role"
    assert cards["role.md"][1] == "role.png"


@pytest.mark.parametrize("other_kind", ["agent", "note"])
def test_duplicate_ids_are_rejected_by_reader_rebuild_and_refresh(db, other_kind):
    write_card(db.root, "role.md", "shared", kind="agent")
    write_card(db.root, "valid.md", "valid")
    commit(db.root, "Valid deck")
    indexed = mddb.MDDB(db.root)
    indexed.conn.close()
    write_card(db.root, "duplicate.md", "shared", kind=other_kind)
    commit(db.root, "Duplicate identity")

    cards, rejected = mddb.read_committed_cards(db.root)
    assert set(cards) == {"valid.md"}
    assert set(rejected) == {"role.md", "duplicate.md"}
    assert all("duplicate id 'shared'" in error for error in rejected.values())
    refreshed = mddb.MDDB(db.root)
    assert refreshed.conn.execute("SELECT relpath FROM entries").fetchall() == [
        ("valid.md",)
    ]
    assert dict(_index.malformed(refreshed.conn)) == rejected
    refreshed.conn.close()
    rebuilt = _index.rebuild_index(db.root, db.head())
    assert dict(_index.malformed(rebuilt)) == rejected
    assert rebuilt.execute("SELECT relpath FROM entries").fetchall() == [("valid.md",)]
    _index.set_git_head(rebuilt, db.head())
    rebuilt.commit()
    rebuilt.close()

    (db.root / "duplicate.md").unlink()
    commit(db.root, "Remove duplicate")
    recovered = mddb.MDDB(db.root)
    assert {
        row[0] for row in recovered.conn.execute("SELECT relpath FROM entries")
    } == {
        "role.md",
        "valid.md",
    }
    assert _index.malformed(recovered.conn) == []
    recovered.conn.close()


def test_reader_and_index_share_per_path_acceptance(db):
    write_card(db.root, "valid.md", "valid")
    write_card(db.root, "attachments.md", "attachments")
    (db.root / "attachments.png").write_bytes(b"png")
    (db.root / "attachments.pdf").write_bytes(b"pdf")
    write_card(db.root, "summary.md", "summary", summary=["invalid"])
    write_card(db.root, "overflow.md", "overflow", summary=2**63)
    write_card(db.root, "surrogate-id.md", "\ud800")
    write_card(db.root, "surrogate-summary.md", "surrogate-summary", summary="\ud800")
    write_card(db.root, "null.md", "null")
    text = (
        (db.root / "null.md")
        .read_text()
        .replace("summary: Fixture", "summary: Fixture\nnull: invalid")
    )
    (db.root / "null.md").write_text(text)
    (db.root / "bad.md").write_text("not a card")
    commit(db.root, "Malformed cards")
    cards, rejected = mddb.read_committed_cards(db.root)
    assert_selected_parity(db.root)
    assert_selected_parity(db.root)
    assert set(cards) == {"valid.md"}
    assert len(rejected) == 7
    conn = _index.rebuild_index(db.root, db.head())
    try:
        assert dict(_index.malformed(conn)) == rejected
        assert conn.execute("SELECT relpath FROM entries").fetchall() == [("valid.md",)]
    finally:
        conn.close()


def test_reader_pins_tree_before_head_changes(db, monkeypatch):
    write_card(db.root, "role.md", "old")
    commit(db.root, "Old card")
    read_blobs = _index._cat_blobs

    def advance(root, objects):
        write_card(root, "role.md", "new")
        commit(root, "New card")
        return read_blobs(root, objects)

    monkeypatch.setattr(_index, "_cat_blobs", advance)
    cards, rejected = mddb.read_committed_cards(db.root)
    assert not rejected
    assert cards["role.md"][0].id == "old"


def test_invalid_revision_fails_visibly(db):
    with pytest.raises(subprocess.CalledProcessError):
        mddb.read_committed_cards(db.root, "missing-revision")


def test_reader_validates_without_serialization_or_full_text_indexing(db, monkeypatch):
    write_card(db.root, "role.md", "role", kind="agent")
    commit(db.root, "Committed role")
    read_cards = _index._cards_from_tree

    def forbid_fts(action, table, *args):
        if action == _index.sqlite3.SQLITE_INSERT and table == "entries_fts":
            return _index.sqlite3.SQLITE_DENY
        return _index.sqlite3.SQLITE_OK

    def checked_cards(conn, *args):
        result = read_cards(conn, *args)
        conn.set_authorizer(forbid_fts)
        return result

    def forbidden(*args, **kwargs):
        pytest.fail("committed reader serialized frontmatter for a discarded index")

    monkeypatch.setattr(_index, "_cards_from_tree", checked_cards)
    monkeypatch.setattr(_index, "_yaml_text", forbidden)
    cards, rejected = mddb.read_committed_cards(db.root)
    assert not rejected
    assert cards["role.md"][0].id == "role"


def test_reader_enforces_schema_even_if_per_file_checks_accept_invalid_card(
    db, monkeypatch
):
    invalid = Card({"id": None, "title": "Invalid", "summary": "Fixture"}, "Body")
    monkeypatch.setattr(
        _index, "_cards_from_tree", lambda *args: ({"invalid.md": (invalid, None)}, {})
    )
    cards, rejected = mddb.read_committed_cards(db.root)
    assert cards == {}
    assert "NOT NULL constraint failed: entries.id" in rejected["invalid.md"]


def assert_selected_parity(root, revision="HEAD"):
    complete, errors = mddb.read_committed_cards(root, revision)
    selected, rejected = mddb.read_committed_cards(root, revision, kind="agent")
    assert rejected == errors
    expected = {p: value for p, value in complete.items() if value[0].kind == "agent"}
    assert selected.keys() == expected.keys()
    for path, (card, blob) in selected.items():
        assert (card.yaml, card.body, blob) == (
            expected[path][0].yaml,
            expected[path][0].body,
            expected[path][1],
        )
    return selected, rejected


def test_selected_census_reuses_unchanged_blobs_and_prunes(db, monkeypatch):
    write_card(db.root, "agent.md", "agent", kind="agent")
    for number in range(200):
        write_card(db.root, f"note-{number}.md", f"note-{number}", kind="note")
    (db.root / "bad.md").write_text("not a card")
    commit(db.root, "Large census")
    assert_selected_parity(db.root)
    reads = []
    cat = _index._cat_blobs

    def counted(root, objects):
        reads.extend(path for path, _oid in objects)
        return cat(root, objects)

    monkeypatch.setattr(_index, "_cat_blobs", counted)
    selected, errors = mddb.read_committed_cards(db.root, kind="agent")
    assert list(selected) == ["agent.md"]
    assert list(errors) == ["bad.md"]
    assert reads == ["agent.md"]
    before = json.loads(_index._committed_census_path(db.root).read_text())["blobs"]
    reads.clear()
    write_card(db.root, "note-0.md", "changed", kind="note")
    (db.root / "note-1.md").unlink()
    commit(db.root, "Change and remove notes")
    mddb.read_committed_cards(db.root, kind="agent")
    assert reads == ["note-0.md", "agent.md"]
    after = json.loads(_index._committed_census_path(db.root).read_text())["blobs"]
    assert len(after) == len(before) - 1
    assert len(set(before) - set(after)) == 2
    assert set(after) == set(
        oid
        for path, oid in _index._tree(db.root, "HEAD").items()
        if path.endswith(".md")
    )


def test_selected_census_recomputes_tree_acceptance(db):
    write_card(db.root, "agent.md", "shared", kind="agent")
    write_card(db.root, "note.md", "shared", kind="note")
    (db.root / "bad.md").write_text("not a card")
    commit(db.root, "Duplicate across kinds")
    first = db.head()
    assert_selected_parity(db.root)
    (db.root / "note.md").unlink()
    (db.root / "bad.md").rename(db.root / "moved-bad.md")
    (db.root / "agent.png").write_bytes(b"png")
    (db.root / "agent.pdf").write_bytes(b"pdf")
    commit(db.root, "Attachment-only rejection and moved diagnostic")
    selected, errors = assert_selected_parity(db.root)
    assert not selected
    assert set(errors) == {"agent.md", "moved-bad.md"}
    (db.root / "agent.pdf").unlink()
    commit(db.root, "Recover agent")
    selected, errors = assert_selected_parity(db.root)
    assert selected["agent.md"][1] == "agent.png"
    cache = _index._committed_census_path(db.root)
    before = cache.read_bytes()
    assert_selected_parity(db.root, first)
    assert cache.read_bytes() == before
    (db.root / "identical.md").write_bytes((db.root / "agent.md").read_bytes())
    commit(db.root, "Same blob at another path")
    selected, errors = assert_selected_parity(db.root)
    assert not selected
    assert "duplicate id" in errors["identical.md"]


def test_selected_cache_loss_and_validation_change_rebuild(db, monkeypatch):
    write_card(db.root, "agent.md", "agent", kind="agent")
    write_card(db.root, "note.md", "note", kind="note")
    commit(db.root, "Census")
    assert_selected_parity(db.root)
    cache = _index._committed_census_path(db.root)
    cache.unlink()
    assert_selected_parity(db.root)
    monkeypatch.setattr(_index, "_committed_validation_version", lambda: "changed")
    assert_selected_parity(db.root)
    assert json.loads(cache.read_text())["version"] == "changed"


def test_selected_atomic_publication_failure_keeps_prior_cache(db, monkeypatch, capsys):
    write_card(db.root, "agent.md", "agent", kind="agent")
    commit(db.root, "First")
    mddb.read_committed_cards(db.root, kind="agent")
    cache = _index._committed_census_path(db.root)
    before = cache.read_bytes()
    write_card(db.root, "note.md", "note", kind="note")
    commit(db.root, "Next")

    def fail(*args):
        raise OSError("interrupted publication")

    monkeypatch.setattr(_index.os, "replace", fail)
    cards, errors = mddb.read_committed_cards(db.root, kind="agent")
    assert set(cards) == {"agent.md"} and not errors
    assert f"cannot publish census {cache}" in capsys.readouterr().err
    assert cache.read_bytes() == before
    assert set(cache.parent.iterdir()) == {cache, _index.cache_path(db.root)}


@pytest.mark.parametrize("damage", [b'{"version":', b"\xff", b'{"version":"unknown"}'])
def test_selected_bad_census_rebuilds_with_diagnostic(db, damage, capsys):
    write_card(db.root, "agent.md", "agent", kind="agent")
    write_card(db.root, "note.md", "note", kind="note")
    commit(db.root, "Census")
    assert_selected_parity(db.root)
    cache = _index._committed_census_path(db.root)
    cache.write_bytes(damage)
    assert_selected_parity(db.root)
    assert f"discarding census {cache}" in capsys.readouterr().err
    assert (
        json.loads(cache.read_text())["version"]
        == _index._committed_validation_version()
    )


def test_census_is_synced_before_replacement(db, monkeypatch):
    events = []
    sync, replace = _index.os.fsync, _index.os.replace

    def synced(fd):
        events.append("fsync")
        sync(fd)

    def replaced(source, target):
        assert events == ["fsync"]
        assert json.loads(source.read_text()) == {"version": "fixture", "blobs": {}}
        events.append("replace")
        replace(source, target)

    monkeypatch.setattr(_index.os, "fsync", synced)
    monkeypatch.setattr(_index.os, "replace", replaced)
    _index._write_committed_census(
        _index._committed_census_path(db.root), {"version": "fixture", "blobs": {}}
    )
    assert events == ["fsync", "replace"]


def test_selected_unreadable_census_is_not_an_enumeration_failure(
    db, monkeypatch, capsys
):
    write_card(db.root, "agent.md", "agent", kind="agent")
    commit(db.root, "Agent")
    assert_selected_parity(db.root)
    cache = _index._committed_census_path(db.root)
    read = type(cache).read_text

    def unreadable(path, *args, **kwargs):
        if path == cache:
            raise PermissionError("census unreadable")
        return read(path, *args, **kwargs)

    monkeypatch.setattr(type(cache), "read_text", unreadable)
    cards, errors = mddb.read_committed_cards(db.root, kind="agent")
    assert set(cards) == {"agent.md"} and not errors
    assert f"discarding census {cache}" in capsys.readouterr().err


def test_selected_concurrent_bootstrap_is_atomic(db):
    write_card(db.root, "agent.md", "agent", kind="agent")
    write_card(db.root, "note.md", "note", kind="note")
    commit(db.root, "Concurrent census")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: mddb.read_committed_cards(db.root, kind="agent"), range(8)
            )
        )
    assert all(set(cards) == {"agent.md"} and not errors for cards, errors in results)
    assert_selected_parity(db.root)


@pytest.mark.parametrize("kind", [None, "agent"])
def test_selected_reader_pins_changed_head(db, monkeypatch, kind):
    write_card(db.root, "agent.md", "old", kind="agent")
    commit(db.root, "Old")
    original = _index._cat_blobs

    def advance(root, objects):
        if objects:
            write_card(db.root, "agent.md", "new", kind="agent")
            commit(db.root, "New")
        return original(root, objects)

    monkeypatch.setattr(_index, "_cat_blobs", advance)
    cards, _ = mddb.read_committed_cards(db.root, kind=kind)
    assert cards["agent.md"][0].id == "old"


@pytest.mark.parametrize(
    "header",
    [
        "{id: role, title: Role, kind: agent, summary: Fixture}",
        'id: role\n"title": Role\n"ki\\u006ed": agent\nsummary: Fixture',
        "base: &base {kind: agent, title: Role, summary: Fixture}\n<<: *base\nid: role",
    ],
)
def test_selected_reader_accepts_supported_yaml(db, header):
    (db.root / "role.md").write_text(f"---\n{header}\n---\nCanonical body\n")
    commit(db.root, "Supported YAML")
    for _ in range(2):
        cards, errors = assert_selected_parity(db.root)
        assert not errors
        assert cards["role.md"][0].body == "Canonical body\n"
