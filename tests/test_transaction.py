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
    monkeypatch.setattr(db, "_git", real_git)
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


def test_transaction_read_sees_staged(db):
    with db.transaction(rationale="read staged") as tx:
        card = tx.create(title="A", summary="A", body="body-a")
        again = tx.read(card.id)
        assert again.body == "body-a"


def test_transaction_read_existing_then_update(db):
    card = db.create(title="A", summary="A", body="x", rationale="seed")
    with db.transaction(rationale="update existing") as tx:
        existing = tx.read(card.id)
        existing.body = "y"
        tx.update(existing, summary="A-updated")
    again = db.read(card.id)
    assert again.body == "y"
    assert again.summary == "A-updated"


def test_transaction_create_then_update(db):
    before = _git_log_count(db)
    with db.transaction(rationale="create+update") as tx:
        card = tx.create(title="A", summary="A")
        tx.update(card, summary="new summary")
    assert _git_log_count(db) == before + 1
    assert db.read(card.id).summary == "new summary"


def test_transaction_create_then_delete(db):
    before = _git_log_count(db)
    with db.transaction(rationale="create+delete") as tx:
        card = tx.create(title="A", summary="A", relpath="will-vanish.md")
        tx.delete(card.id)
    assert _git_log_count(db) == before
    assert not (db.root / "will-vanish.md").exists()
    assert db.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0


def test_transaction_delete_then_create_same_relpath(db):
    seed = db.create(title="Old", summary="old", relpath="slot.md", rationale="seed")
    before = _git_log_count(db)
    with db.transaction(rationale="swap") as tx:
        tx.delete(seed.id)
        new = tx.create(title="New", summary="new", relpath="slot.md")
    assert _git_log_count(db) == before + 1
    again = db.read(new.id)
    assert again.title == "New"
    with pytest.raises(KeyError):
        db.read(seed.id)


def test_transaction_move_existing(db):
    seed = db.create(title="A", summary="A", relpath="orig.md", rationale="seed")
    before = _git_log_count(db)
    with db.transaction(rationale="move") as tx:
        tx.move(seed.id, "moved.md")
    assert _git_log_count(db) == before + 1
    assert not (db.root / "orig.md").exists()
    assert (db.root / "moved.md").exists()
    assert db._relpath(seed.id) == "moved.md"


def test_transaction_move_to_subdirectory(db):
    seed = db.create(title="A", summary="A", relpath="flat.md", rationale="seed")
    with db.transaction(rationale="move to subdir") as tx:
        tx.move(seed.id, "sub/dir/here.md")
    assert (db.root / "sub" / "dir" / "here.md").exists()


def test_transaction_move_only_does_not_rewrite_body(db):
    seed = db.create(
        title="A",
        summary="A",
        body="byte-exact body\n",
        rationale="seed",
        relpath="orig.md",
    )
    original_text = (db.root / "orig.md").read_text()
    with db.transaction(rationale="move only") as tx:
        tx.move(seed.id, "new.md")
    assert (db.root / "new.md").read_text() == original_text


def test_transaction_read_after_move_only(db):
    seed = db.create(
        title="A", summary="A", body="hello", rationale="seed", relpath="orig.md"
    )
    with db.transaction(rationale="read after move-only") as tx:
        tx.move(seed.id, "moved.md")
        card = tx.read(seed.id)
        assert card.body == "hello"


def test_transaction_modify_after_delete_raises(db):
    seed = db.create(title="A", summary="A", rationale="seed")
    with db.transaction(rationale="modify-after-delete") as tx:
        tx.delete(seed.id)
        with pytest.raises(KeyError):
            tx.read(seed.id)
        with pytest.raises(KeyError):
            tx.update(seed, summary="x")
        with pytest.raises(KeyError):
            tx.move(seed.id, "new.md")


def test_transaction_move_into_staged_deleted_slot_raises(db):
    a = db.create(title="A", summary="A", relpath="slot.md", rationale="seed-a")
    b = db.create(title="B", summary="B", relpath="other.md", rationale="seed-b")
    with db.transaction(rationale="move into deleted slot") as tx:
        tx.delete(a.id)
        with pytest.raises(FileExistsError):
            tx.move(b.id, "slot.md")


def test_transaction_move_staged_create(db):
    with db.transaction(rationale="move staged create") as tx:
        card = tx.create(title="A", summary="A", relpath="initial.md")
        tx.move(card.id, "moved.md")
    assert not (db.root / "initial.md").exists()
    assert (db.root / "moved.md").exists()


def test_transaction_move_same_path_is_noop(db):
    seed = db.create(title="A", summary="A", rationale="seed")
    current = db._relpath(seed.id)
    before = _git_log_count(db)
    with db.transaction(rationale="self move") as tx:
        tx.move(seed.id, current)
    assert _git_log_count(db) == before


def test_transaction_create_into_move_away_slot_raises(db):
    a = db.create(title="A", summary="A", relpath="orig.md", rationale="seed-a")
    with db.transaction(rationale="create into move-away") as tx:
        tx.move(a.id, "new.md")
        with pytest.raises(FileExistsError):
            tx.create(title="B", summary="B", relpath="orig.md")


def test_transaction_move_into_move_away_slot_raises(db):
    a = db.create(title="A", summary="A", relpath="orig.md", rationale="seed-a")
    b = db.create(title="B", summary="B", relpath="other.md", rationale="seed-b")
    with db.transaction(rationale="move into move-away") as tx:
        tx.move(a.id, "new.md")
        with pytest.raises(FileExistsError):
            tx.move(b.id, "orig.md")


def test_transaction_update_then_move(db):
    seed = db.create(
        title="A", summary="A", body="x", rationale="seed", relpath="orig.md"
    )
    with db.transaction(rationale="update+move") as tx:
        seed.body = "y"
        tx.update(seed, summary="A-updated")
        tx.move(seed.id, "moved.md")
    assert not (db.root / "orig.md").exists()
    assert (db.root / "moved.md").exists()
    again = db.read(seed.id)
    assert again.body == "y"
    assert again.summary == "A-updated"


def test_transaction_delete_after_staged_move(db):
    seed = db.create(title="A", summary="A", relpath="orig.md", rationale="seed")
    with db.transaction(rationale="move then delete") as tx:
        tx.move(seed.id, "moved.md")
        tx.delete(seed.id)
    assert not (db.root / "orig.md").exists()
    assert not (db.root / "moved.md").exists()
    with pytest.raises(KeyError):
        db.read(seed.id)


def test_transaction_read_returns_deep_copy_for_updates(db):
    seed = db.create(title="A", summary="A", yaml={"tags": []}, rationale="seed")
    with db.transaction(rationale="deep copy update") as tx:
        seed.body = "changed"
        returned = tx.update(seed, summary="A")
        returned.yaml["tags"].append("shed")
    again = db.read(seed.id)
    assert again.body == "changed"
    assert again.yaml["tags"] == []


def test_transaction_active_tx_cleared_after_sqlite_failure(db, monkeypatch):
    real_git = db._git

    def close_conn_after_commit(*args):
        result = real_git(*args)
        if args and args[0] == "commit":
            db.conn.close()
        return result

    monkeypatch.setattr(db, "_git", close_conn_after_commit)
    with pytest.raises(Exception):
        with db.transaction(rationale="sqlite fails") as tx:
            card = tx.create(title="A", summary="A")
            staged_id = card.id
    monkeypatch.setattr(db, "_git", real_git)
    assert db._active_tx is None
    assert (db.root / "a.md").exists()
    from mddb.index import cache_path

    cache_path(db.root).unlink()
    fresh = mddb.MDDB(db.root)
    recovered = fresh.read(staged_id)
    assert recovered.title == "A"
    with fresh.transaction(rationale="after sqlite failure") as tx:
        tx.create(title="B", summary="B")
    assert (db.root / "b.md").exists()


def test_transaction_move_into_committed_unstaged_card_relpath_raises(db):
    a = db.create(title="A", summary="A", relpath="alpha.md", rationale="seed-a")
    b = db.create(title="B", summary="B", relpath="beta.md", rationale="seed-b")
    with db.transaction(rationale="move into committed") as tx:
        with pytest.raises(FileExistsError):
            tx.move(a.id, "beta.md")
    assert db._relpath(a.id) == "alpha.md"
    assert db._relpath(b.id) == "beta.md"


def test_transaction_move_away_and_back_collapses(db):
    seed = db.create(title="A", summary="A", relpath="orig.md", rationale="seed")
    before = _git_log_count(db)
    with db.transaction(rationale="move away and back") as tx:
        tx.move(seed.id, "new.md")
        tx.move(seed.id, "orig.md")
    assert _git_log_count(db) == before
    assert (db.root / "orig.md").exists()
    assert not (db.root / "new.md").exists()


def test_transaction_update_input_card_is_copied(db):
    seed = db.create(title="A", summary="A", yaml={"tags": []}, rationale="seed")
    with db.transaction(rationale="input copy") as tx:
        seed.body = "changed"
        tx.update(seed, summary=seed.summary)
        seed.yaml["tags"].append("shed")
    again = db.read(seed.id)
    assert again.body == "changed"
    assert again.yaml["tags"] == []


def test_transaction_read_after_move_plus_update(db):
    seed = db.create(title="A", summary="A", rationale="seed")
    with db.transaction(rationale="move+update read") as tx:
        tx.update(seed, summary="new")
        tx.move(seed.id, "moved.md")
        card = tx.read(seed.id)
        assert card.summary == "new"


def test_transaction_move_collision_against_staged_create(db):
    seed = db.create(title="A", summary="A", relpath="orig.md", rationale="seed")
    with db.transaction(rationale="move collide staged create") as tx:
        tx.create(title="B", summary="B", relpath="claimed.md")
        with pytest.raises(FileExistsError):
            tx.move(seed.id, "claimed.md")


def test_transaction_read_after_create_then_delete_in_buffer_raises(db):
    with db.transaction(rationale="create+delete read") as tx:
        card = tx.create(title="A", summary="A")
        tx.delete(card.id)
        with pytest.raises(KeyError):
            tx.read(card.id)


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
