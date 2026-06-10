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


def test_two_writer_conflict_then_retry(db, seed):
    seed(title="Seed", summary="s")
    stale = db.head()
    with db.editor(rationale="A commits first") as e:
        e.create(title="A", summary="a")
    with pytest.raises(mddb.ConflictError):
        with db.editor(rationale="B on stale base", base=stale) as e:
            e.create(title="B", summary="b")
    with db.editor(rationale="B retries on fresh base", base=db.head()) as e:
        card = e.create(title="B", summary="b")
    assert db.read(card.id).title == "B"


def test_disjoint_edits_still_conflict(db, seed):
    a = seed(title="A", summary="a")
    b = seed(title="B", summary="b")
    stale = db.head()
    with db.editor(rationale="touch A") as e:
        card_a = e.read(a.id)
        card_a.body = "changed\n"
        e.update(card_a, summary="a2")
    with pytest.raises(mddb.ConflictError):
        with db.editor(rationale="touch B on stale base", base=stale) as e:
            card_b = e.read(b.id)
            card_b.body = "changed\n"
            e.update(card_b, summary="b2")


def test_flock_serialises_concurrent_writers(db, seed):
    import threading

    seed(title="Seed", summary="s")
    barrier = threading.Barrier(2)
    outcomes = []

    def writer(name):
        handle = mddb.MDDB(db.root)
        try:
            with handle.editor(rationale=name) as e:
                e.create(title=name, summary="s")
                barrier.wait(timeout=10)
            outcomes.append("ok")
        except mddb.ConflictError:
            outcomes.append("conflict")

    threads = [threading.Thread(target=writer, args=(n,)) for n in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["conflict", "ok"]


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
