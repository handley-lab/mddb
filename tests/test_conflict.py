import pytest

import mddb
from mddb import _index


def test_init_returns_usable_handle(tmp_path):
    db = mddb.MDDB.init(tmp_path / "deck")
    with db.editor(rationale="first card") as e:
        card = e.create(title="A", summary="a")
    assert db.read(card.id).title == "A"


def test_init_sets_git_head(tmp_path):
    db = mddb.MDDB.init(tmp_path / "deck")
    assert _index.git_head(db.conn) == db.head()


def test_commit_advances_git_head(db, seed):
    seed(title="A", summary="a")
    assert _index.git_head(db.conn) == db.head()


def test_concurrent_commit_during_block_conflicts(db, seed):
    seed(title="Seed", summary="s")
    with pytest.raises(mddb.ConflictError):
        with db.editor(rationale="A") as e:
            e.create(title="A", summary="a")
            other = mddb.MDDB(db.root)
            with other.editor(rationale="B") as e2:
                e2.create(title="B", summary="b")
    titles = {entry["title"] for entry in db.list()}
    assert "B" in titles
    assert "A" not in titles
    assert not (db.root / "a.md").exists()


def test_open_recheck_skips_rebuild_when_fresh(db, seed, monkeypatch):
    seed(title="A", summary="a")
    head = db.head()
    calls = {"n": 0}

    def flaky_git_head(conn):
        calls["n"] += 1
        return "" if calls["n"] == 1 else head

    monkeypatch.setattr(_index, "git_head", flaky_git_head)
    monkeypatch.setattr(
        _index, "_rebuild_at", lambda *a: pytest.fail("recheck should skip rebuild")
    )
    assert _index.open_index(db.root, head) is not None


def test_stale_explicit_base_raises(db, seed):
    base0 = db.head()
    seed(title="Other", summary="moves HEAD")
    with pytest.raises(mddb.ConflictError):
        with db.editor(rationale="stale", base=base0) as e:
            e.create(title="B", summary="b")


def test_fresh_explicit_base_commits(db, seed):
    seed(title="A", summary="a")
    fresh = db.head()
    with db.editor(rationale="fresh", base=fresh) as e:
        card = e.create(title="B", summary="b")
    assert db.read(card.id).title == "B"


def test_open_rebuilds_on_head_mismatch(db, seed):
    seed(title="A", summary="a")
    (db.root / "b.md").write_text("---\nid: b-id\ntitle: B\nsummary: s\n---\nbody\n")
    db._git("add", "--", "b.md")
    db._git("commit", "-q", "-m", "raw add outside the editor")
    reopened = mddb.MDDB(db.root)
    assert "b-id" in {entry["id"] for entry in reopened.list()}
    assert _index.git_head(reopened.conn) == reopened.head()
