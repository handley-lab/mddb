import fcntl
import subprocess

import pytest

import mddb
from mddb import Card, _index


def commit(root, message):
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", message], check=True)


def write_card(root, path, card_id, **fields):
    card = Card({"id": card_id, "title": path, "summary": "Fixture", **fields}, "Body")
    (root / path).write_text(str(card))


def test_reader_uses_committed_revision_without_locks_or_cache(db, monkeypatch):
    write_card(db.root, "role.md", "role")
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
        cards, rejected = mddb.read_committed_cards(db.root, revision)
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
    assert set(cards) == {"valid.md"}
    assert len(rejected) == 5
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
