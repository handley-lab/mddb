import pytest

import mddb


def _git_log_count(db) -> int:
    out = db._git("rev-list", "--count", "HEAD").stdout.strip()
    return int(out)


def test_transaction_create_batch(db):
    before = _git_log_count(db)
    with db.transaction(rationale="batch create three") as tx:
        a = tx.create(title="A", summary="A sum")
        b = tx.create(title="B", summary="B sum")
        c = tx.create(title="C", summary="C sum")
    assert _git_log_count(db) == before + 1
    assert (db.root / "a.md").exists()
    assert (db.root / "b.md").exists()
    assert (db.root / "c.md").exists()
    ids = {row[0] for row in db.conn.execute("SELECT id FROM entries").fetchall()}
    assert {a.id, b.id, c.id} <= ids
    assert db._active_tx is None


def test_transaction_exception_rolls_back(db):
    before = _git_log_count(db)
    with pytest.raises(RuntimeError, match="bail"):
        with db.transaction(rationale="rollback test") as tx:
            tx.create(title="A", summary="A")
            tx.create(title="B", summary="B")
            raise RuntimeError("bail")
    assert _git_log_count(db) == before
    assert not (db.root / "a.md").exists()
    assert not (db.root / "b.md").exists()
    assert db.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
    assert db._active_tx is None


def test_transaction_empty_no_commit(db):
    before = _git_log_count(db)
    with db.transaction(rationale="empty"):
        pass
    assert _git_log_count(db) == before
    assert db._active_tx is None


def test_transaction_collision_in_buffer(db):
    with db.transaction(rationale="collision in buffer") as tx:
        tx.create(title="A", summary="x", relpath="dup.md")
        with pytest.raises(FileExistsError):
            tx.create(title="B", summary="x", relpath="dup.md")


def test_transaction_collision_against_disk(db):
    db.create(title="Existing", summary="x", rationale="seed", relpath="seed.md")
    with db.transaction(rationale="collision against disk") as tx:
        with pytest.raises(FileExistsError):
            tx.create(title="Other", summary="x", relpath="seed.md")


def test_transaction_nested_raises(db):
    with db.transaction(rationale="outer"):
        with pytest.raises(RuntimeError, match="nested"):
            with db.transaction(rationale="inner"):
                pass


def test_transaction_create_returns_copy(db):
    with db.transaction(rationale="returned-card body isolation") as tx:
        card = tx.create(title="A", summary="A", body="original")
        card.body = "mutated"
    again = db.read(card.id)
    assert again.body == "original"


def test_transaction_active_slot_cleared_after_failure(db, monkeypatch):
    real_git = db._git

    def fail_commit(*args):
        if args and args[0] == "commit":
            raise RuntimeError("simulated commit failure")
        return real_git(*args)

    monkeypatch.setattr(db, "_git", fail_commit)
    with pytest.raises(RuntimeError, match="simulated"):
        with db.transaction(rationale="will fail") as tx:
            tx.create(title="A", summary="A")
    monkeypatch.undo()
    assert db._active_tx is None
    with db.transaction(rationale="after failure") as tx:
        tx.create(title="B", summary="B")
    assert (db.root / "b.md").exists()


def test_transaction_create_returns_deep_copy(db):
    with db.transaction(rationale="deep copy") as tx:
        card = tx.create(title="A", summary="A", yaml={"tags": []})
        card.yaml["tags"].append("shed")
    again = db.read(card.id)
    assert again.yaml["tags"] == []


def test_transaction_duplicate_id_in_buffer(db):
    with db.transaction(rationale="dup id buffer") as tx:
        tx.create(title="A", summary="A", yaml={"id": "fixed"})
        with pytest.raises(RuntimeError, match="duplicate id in transaction"):
            tx.create(title="B", summary="B", yaml={"id": "fixed"})


def test_transaction_id_collision_against_db(db):
    db.create(
        title="X",
        summary="seed",
        yaml={"id": "fixed-id"},
        rationale="seed",
    )
    with db.transaction(rationale="id collision db") as tx:
        with pytest.raises(RuntimeError, match="id already exists"):
            tx.create(title="Y", summary="y", yaml={"id": "fixed-id"})


def test_transaction_closed_after_empty_exit(db):
    with db.transaction(rationale="empty close") as tx:
        pass
    with pytest.raises(RuntimeError, match="already closed"):
        tx.create(title="A", summary="A")


def test_transaction_closed_after_body_exception(db):
    with pytest.raises(ValueError):
        with db.transaction(rationale="body raise") as tx:
            raise ValueError("oops")
    with pytest.raises(RuntimeError, match="already closed"):
        tx.create(title="A", summary="A")


def test_transaction_closed_after_successful_commit(db):
    with db.transaction(rationale="ok") as tx:
        tx.create(title="A", summary="A")
    with pytest.raises(RuntimeError, match="already closed"):
        tx.create(title="B", summary="B")


def test_base_mutators_blocked_during_active_transaction(db):
    seed = db.create(title="Seed", summary="seed", rationale="seed")
    with db.transaction(rationale="active") as _tx:
        with pytest.raises(RuntimeError, match="while a transaction is active"):
            db.create(title="X", summary="x", rationale="r")
        with pytest.raises(RuntimeError, match="while a transaction is active"):
            db.update(seed, summary="x", rationale="r")
        with pytest.raises(RuntimeError, match="while a transaction is active"):
            db.delete(seed.id, rationale="r")
        with pytest.raises(RuntimeError, match="while a transaction is active"):
            db.move(seed.id, "moved.md", rationale="r")


def test_base_reads_allowed_during_active_transaction(db):
    seed = db.create(title="Seed", summary="seed", rationale="seed")
    with db.transaction(rationale="active") as _tx:
        assert db.read(seed.id).id == seed.id
        listed = db.list()
        assert any(e["id"] == seed.id for e in listed)
        assert db.history(seed.id)
        db.conn.execute("SELECT 1").fetchone()


def test_mddb_init_sets_active_tx_none(tmp_path):
    new_db = mddb.MDDB(tmp_path)
    assert new_db._active_tx is None


def test_transaction_closed_after_commit_phase_failure(db, monkeypatch):
    real_git = db._git

    def fail_commit(*args):
        if args and args[0] == "commit":
            raise RuntimeError("simulated commit failure")
        return real_git(*args)

    monkeypatch.setattr(db, "_git", fail_commit)
    with pytest.raises(RuntimeError, match="simulated"):
        with db.transaction(rationale="will fail") as tx:
            tx.create(title="A", summary="A")
    with pytest.raises(RuntimeError, match="already closed"):
        tx.create(title="B", summary="B")
    with pytest.raises(RuntimeError, match="already closed"):
        with tx:
            pass
