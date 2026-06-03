import pytest

import mddb
from mddb import _index


def _git_log_count(db) -> int:
    out = db._git("rev-list", "--count", "HEAD").stdout.strip()
    return int(out)


def test_edit_create_batch(db):
    before = _git_log_count(db)
    with db.edit(rationale="batch create three") as edit:
        a = edit.create(title="A", summary="A sum")
        b = edit.create(title="B", summary="B sum")
        c = edit.create(title="C", summary="C sum")
    assert _git_log_count(db) == before + 1
    assert (db.root / "a.md").exists()
    assert (db.root / "b.md").exists()
    assert (db.root / "c.md").exists()
    ids = {row[0] for row in db.conn.execute("SELECT id FROM entries").fetchall()}
    assert {a.id, b.id, c.id} <= ids
    assert db._active_edit is None


def test_edit_exception_rolls_back(db):
    before = _git_log_count(db)
    with pytest.raises(RuntimeError, match="bail"):
        with db.edit(rationale="rollback test") as edit:
            edit.create(title="A", summary="A")
            edit.create(title="B", summary="B")
            raise RuntimeError("bail")
    assert _git_log_count(db) == before
    assert not (db.root / "a.md").exists()
    assert not (db.root / "b.md").exists()
    assert db.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
    assert db._active_edit is None


def test_edit_empty_no_commit(db):
    before = _git_log_count(db)
    with db.edit(rationale="empty"):
        pass
    assert _git_log_count(db) == before
    assert db._active_edit is None


def test_edit_collision_in_buffer(db):
    with db.edit(rationale="collision in buffer") as edit:
        edit.create(title="A", summary="x", relpath="dup.md")
        with pytest.raises(FileExistsError):
            edit.create(title="B", summary="x", relpath="dup.md")


def test_edit_collision_against_disk(db, seed):
    seed(title="Existing", summary="x", relpath="seed.md")
    with db.edit(rationale="collision against disk") as edit:
        with pytest.raises(FileExistsError):
            edit.create(title="Other", summary="x", relpath="seed.md")


def test_edit_nested_raises(db):
    with db.edit(rationale="outer"):
        with pytest.raises(RuntimeError, match="nested"):
            with db.edit(rationale="inner"):
                pass


def test_edit_create_returns_copy(db):
    with db.edit(rationale="returned-card body isolation") as edit:
        card = edit.create(title="A", summary="A", body="original")
        card.body = "mutated"
    again = db.read(card.id)
    assert again.body == "original"


def test_edit_active_slot_cleared_after_failure(db, monkeypatch):
    real_git = db._git

    def fail_commit(*args):
        if args and args[0] == "commit":
            raise RuntimeError("simulated commit failure")
        return real_git(*args)

    monkeypatch.setattr(db, "_git", fail_commit)
    with pytest.raises(RuntimeError, match="simulated"):
        with db.edit(rationale="will fail") as edit:
            edit.create(title="A", summary="A")
    monkeypatch.setattr(db, "_git", real_git)
    assert db._active_edit is None
    with db.edit(rationale="after failure") as edit:
        edit.create(title="B", summary="B")
    assert (db.root / "b.md").exists()


def test_edit_create_returns_deep_copy(db):
    with db.edit(rationale="deep copy") as edit:
        card = edit.create(title="A", summary="A", yaml={"tags": []})
        card.yaml["tags"].append("shed")
    again = db.read(card.id)
    assert again.yaml["tags"] == []


def test_edit_duplicate_id_in_buffer(db):
    with db.edit(rationale="dup id buffer") as edit:
        edit.create(title="A", summary="A", yaml={"id": "fixed"})
        with pytest.raises(RuntimeError, match="duplicate id in edit"):
            edit.create(title="B", summary="B", yaml={"id": "fixed"})


def test_edit_id_collision_against_db_fails_at_commit(db, seed):
    import sqlite3

    seed(title="X", summary="seed", yaml={"id": "fixed-id"})
    with pytest.raises(sqlite3.IntegrityError):
        with db.edit(rationale="id collision db") as edit:
            edit.create(title="Y", summary="y", yaml={"id": "fixed-id"})


def test_edit_closed_after_empty_exit(db):
    with db.edit(rationale="empty close") as edit:
        pass
    with pytest.raises(RuntimeError, match="already closed"):
        edit.create(title="A", summary="A")


def test_edit_closed_after_body_exception(db):
    with pytest.raises(ValueError):
        with db.edit(rationale="body raise") as edit:
            raise ValueError("oops")
    with pytest.raises(RuntimeError, match="already closed"):
        edit.create(title="A", summary="A")


def test_edit_closed_after_successful_commit(db):
    with db.edit(rationale="ok") as edit:
        edit.create(title="A", summary="A")
    with pytest.raises(RuntimeError, match="already closed"):
        edit.create(title="B", summary="B")


def test_edit_read_sees_staged(db):
    with db.edit(rationale="read staged") as edit:
        card = edit.create(title="A", summary="A", body="body-a")
        again = edit.read(card.id)
        assert again.body == "body-a"


def test_edit_read_existing_then_update(db, seed):
    card = seed(title="A", summary="A", body="x")
    with db.edit(rationale="update existing") as edit:
        existing = edit.read(card.id)
        existing.body = "y"
        edit.update(existing, summary="A-updated")
    again = db.read(card.id)
    assert again.body == "y"
    assert again.summary == "A-updated"


def test_edit_create_then_update(db):
    before = _git_log_count(db)
    with db.edit(rationale="create+update") as edit:
        card = edit.create(title="A", summary="A")
        edit.update(card, summary="new summary")
    assert _git_log_count(db) == before + 1
    assert db.read(card.id).summary == "new summary"


def test_edit_create_then_delete(db):
    before = _git_log_count(db)
    with db.edit(rationale="create+delete") as edit:
        card = edit.create(title="A", summary="A", relpath="will-vanish.md")
        edit.delete(card.id)
    assert _git_log_count(db) == before
    assert not (db.root / "will-vanish.md").exists()
    assert db.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0


def test_edit_delete_then_create_same_relpath(db, seed):
    old = seed(title="Old", summary="old", relpath="slot.md")
    before = _git_log_count(db)
    with db.edit(rationale="swap") as edit:
        edit.delete(old.id)
        new = edit.create(title="New", summary="new", relpath="slot.md")
    assert _git_log_count(db) == before + 1
    again = db.read(new.id)
    assert again.title == "New"
    with pytest.raises(KeyError):
        db.read(old.id)


def test_edit_move_existing(db, seed):
    card = seed(title="A", summary="A", relpath="orig.md")
    before = _git_log_count(db)
    with db.edit(rationale="move") as edit:
        edit.move(card.id, "moved.md")
    assert _git_log_count(db) == before + 1
    assert not (db.root / "orig.md").exists()
    assert (db.root / "moved.md").exists()
    assert _index.relpath_of(db.conn, card.id) == "moved.md"


def test_edit_move_to_subdirectory(db, seed):
    card = seed(title="A", summary="A", relpath="flat.md")
    with db.edit(rationale="move to subdir") as edit:
        edit.move(card.id, "sub/dir/here.md")
    assert (db.root / "sub" / "dir" / "here.md").exists()


def test_edit_move_only_does_not_rewrite_body(db, seed):
    card = seed(title="A", summary="A", body="byte-exact body\n", relpath="orig.md")
    original_text = (db.root / "orig.md").read_text()
    with db.edit(rationale="move only") as edit:
        edit.move(card.id, "new.md")
    assert (db.root / "new.md").read_text() == original_text


def test_edit_read_after_move_only(db, seed):
    card = seed(title="A", summary="A", body="hello", relpath="orig.md")
    with db.edit(rationale="read after move-only") as edit:
        edit.move(card.id, "moved.md")
        c = edit.read(card.id)
        assert c.body == "hello"


def test_edit_modify_after_delete_raises(db, seed):
    card = seed(title="A", summary="A")
    with db.edit(rationale="modify-after-delete") as edit:
        edit.delete(card.id)
        with pytest.raises(KeyError):
            edit.read(card.id)
        with pytest.raises(KeyError):
            edit.update(card, summary="x")
        with pytest.raises(KeyError):
            edit.move(card.id, "new.md")


def test_edit_move_into_staged_deleted_slot_raises(db, seed):
    a = seed(title="A", summary="A", relpath="slot.md")
    b = seed(title="B", summary="B", relpath="other.md")
    with db.edit(rationale="move into deleted slot") as edit:
        edit.delete(a.id)
        with pytest.raises(FileExistsError):
            edit.move(b.id, "slot.md")


def test_edit_move_staged_create(db):
    with db.edit(rationale="move staged create") as edit:
        card = edit.create(title="A", summary="A", relpath="initial.md")
        edit.move(card.id, "moved.md")
    assert not (db.root / "initial.md").exists()
    assert (db.root / "moved.md").exists()


def test_edit_move_same_path_is_noop(db, seed):
    card = seed(title="A", summary="A")
    current = _index.relpath_of(db.conn, card.id)
    before = _git_log_count(db)
    with db.edit(rationale="self move") as edit:
        edit.move(card.id, current)
    assert _git_log_count(db) == before


def test_edit_create_into_move_away_slot_raises(db, seed):
    a = seed(title="A", summary="A", relpath="orig.md")
    with db.edit(rationale="create into move-away") as edit:
        edit.move(a.id, "new.md")
        with pytest.raises(FileExistsError):
            edit.create(title="B", summary="B", relpath="orig.md")


def test_edit_move_into_move_away_slot_raises(db, seed):
    a = seed(title="A", summary="A", relpath="orig.md")
    b = seed(title="B", summary="B", relpath="other.md")
    with db.edit(rationale="move into move-away") as edit:
        edit.move(a.id, "new.md")
        with pytest.raises(FileExistsError):
            edit.move(b.id, "orig.md")


def test_edit_update_then_move(db, seed):
    card = seed(title="A", summary="A", body="x", relpath="orig.md")
    with db.edit(rationale="update+move") as edit:
        card.body = "y"
        edit.update(card, summary="A-updated")
        edit.move(card.id, "moved.md")
    assert not (db.root / "orig.md").exists()
    assert (db.root / "moved.md").exists()
    again = db.read(card.id)
    assert again.body == "y"
    assert again.summary == "A-updated"


def test_edit_delete_after_staged_move(db, seed):
    card = seed(title="A", summary="A", relpath="orig.md")
    with db.edit(rationale="move then delete") as edit:
        edit.move(card.id, "moved.md")
        edit.delete(card.id)
    assert not (db.root / "orig.md").exists()
    assert not (db.root / "moved.md").exists()
    with pytest.raises(KeyError):
        db.read(card.id)


def test_edit_read_returns_deep_copy_for_updates(db, seed):
    card = seed(title="A", summary="A", yaml={"tags": []})
    with db.edit(rationale="deep copy update") as edit:
        card.body = "changed"
        returned = edit.update(card, summary="A")
        returned.yaml["tags"].append("shed")
    again = db.read(card.id)
    assert again.body == "changed"
    assert again.yaml["tags"] == []


def test_edit_active_slot_cleared_after_sqlite_failure(db, monkeypatch):
    real_git = db._git

    def close_conn_after_commit(*args):
        result = real_git(*args)
        if args and args[0] == "commit":
            db.conn.close()
        return result

    monkeypatch.setattr(db, "_git", close_conn_after_commit)
    with pytest.raises(Exception):
        with db.edit(rationale="sqlite fails") as edit:
            card = edit.create(title="A", summary="A")
            staged_id = card.id
    monkeypatch.setattr(db, "_git", real_git)
    assert db._active_edit is None
    assert (db.root / "a.md").exists()
    from mddb._index import cache_path

    cache_path(db.root).unlink()
    fresh = mddb.MDDB(db.root)
    recovered = fresh.read(staged_id)
    assert recovered.title == "A"
    with fresh.edit(rationale="after sqlite failure") as edit:
        edit.create(title="B", summary="B")
    assert (db.root / "b.md").exists()


def test_edit_move_into_committed_unstaged_card_relpath_raises(db, seed):
    a = seed(title="A", summary="A", relpath="alpha.md")
    b = seed(title="B", summary="B", relpath="beta.md")
    with db.edit(rationale="move into committed") as edit:
        with pytest.raises(FileExistsError):
            edit.move(a.id, "beta.md")
    assert _index.relpath_of(db.conn, a.id) == "alpha.md"
    assert _index.relpath_of(db.conn, b.id) == "beta.md"


def test_edit_move_away_and_back_collapses(db, seed):
    card = seed(title="A", summary="A", relpath="orig.md")
    before = _git_log_count(db)
    with db.edit(rationale="move away and back") as edit:
        edit.move(card.id, "new.md")
        edit.move(card.id, "orig.md")
    assert _git_log_count(db) == before
    assert (db.root / "orig.md").exists()
    assert not (db.root / "new.md").exists()


def test_edit_update_input_card_is_copied(db, seed):
    card = seed(title="A", summary="A", yaml={"tags": []})
    with db.edit(rationale="input copy") as edit:
        card.body = "changed"
        edit.update(card, summary=card.summary)
        card.yaml["tags"].append("shed")
    again = db.read(card.id)
    assert again.body == "changed"
    assert again.yaml["tags"] == []


def test_edit_read_after_move_plus_update(db, seed):
    card = seed(title="A", summary="A")
    with db.edit(rationale="move+update read") as edit:
        edit.update(card, summary="new")
        edit.move(card.id, "moved.md")
        c = edit.read(card.id)
        assert c.summary == "new"


def test_edit_move_collision_against_staged_create(db, seed):
    card = seed(title="A", summary="A", relpath="orig.md")
    with db.edit(rationale="move collide staged create") as edit:
        edit.create(title="B", summary="B", relpath="claimed.md")
        with pytest.raises(FileExistsError):
            edit.move(card.id, "claimed.md")


def test_edit_read_after_create_then_delete_in_buffer_raises(db):
    with db.edit(rationale="create+delete read") as edit:
        card = edit.create(title="A", summary="A")
        edit.delete(card.id)
        with pytest.raises(KeyError):
            edit.read(card.id)


def test_edit_closed_after_commit_phase_failure(db, monkeypatch):
    real_git = db._git

    def fail_commit(*args):
        if args and args[0] == "commit":
            raise RuntimeError("simulated commit failure")
        return real_git(*args)

    monkeypatch.setattr(db, "_git", fail_commit)
    with pytest.raises(RuntimeError, match="simulated"):
        with db.edit(rationale="will fail") as edit:
            edit.create(title="A", summary="A")
    with pytest.raises(RuntimeError, match="already closed"):
        edit.create(title="B", summary="B")
    with pytest.raises(RuntimeError, match="already closed"):
        with edit:
            pass


def test_edit_create_tags_kwarg_basic(db):
    with db.edit(rationale="tags basic") as edit:
        card = edit.create(
            title="A", summary="A", tags=["area/work", "topic/cosmology"]
        )
    again = db.read(card.id)
    assert again.tags == ["area/work", "topic/cosmology"]


def test_edit_create_no_tags_omits_key(db):
    with db.edit(rationale="no tags") as edit:
        card = edit.create(title="A", summary="A")
    assert "tags" not in db.read(card.id).yaml


def test_edit_create_empty_tuple_omits_key(db):
    with db.edit(rationale="empty tuple") as edit:
        card = edit.create(title="A", summary="A", tags=())
    assert "tags" not in db.read(card.id).yaml


def test_edit_create_empty_list_omits_key(db):
    with db.edit(rationale="empty list") as edit:
        card = edit.create(title="A", summary="A", tags=[])
    assert "tags" not in db.read(card.id).yaml


def test_edit_create_tags_kwarg_wins_over_yaml(db):
    with db.edit(rationale="kwarg wins") as edit:
        card = edit.create(title="A", summary="A", yaml={"tags": ["x"]}, tags=["y"])
    assert db.read(card.id).tags == ["y"]


def test_edit_create_empty_tags_clears_yaml_tags(db):
    with db.edit(rationale="empty clears") as edit:
        card = edit.create(title="A", summary="A", yaml={"tags": ["x"]}, tags=())
    assert "tags" not in db.read(card.id).yaml


def test_edit_create_none_tags_preserves_yaml_tags(db):
    with db.edit(rationale="preserve yaml") as edit:
        card = edit.create(title="A", summary="A", yaml={"tags": ["x"]}, tags=None)
    assert db.read(card.id).tags == ["x"]


def test_edit_create_omitted_tags_preserves_yaml_tags(db):
    with db.edit(rationale="implicit preserve") as edit:
        card = edit.create(title="A", summary="A", yaml={"tags": ["x"]})
    assert db.read(card.id).tags == ["x"]


def test_edit_create_yaml_empty_tags_preserved(db):
    with db.edit(rationale="raw empty preserved") as edit:
        card = edit.create(title="A", summary="A", yaml={"tags": []})
    assert db.read(card.id).yaml["tags"] == []


def test_edit_create_required_kwargs_win_over_yaml(db):
    with db.edit(rationale="kwargs win") as edit:
        card = edit.create(
            title="from kwarg",
            summary="kwarg summary",
            yaml={"title": "from yaml", "summary": "yaml summary"},
        )
    again = db.read(card.id)
    assert again.title == "from kwarg"
    assert again.summary == "kwarg summary"


def test_edit_create_caller_supplied_id_preserved(db):
    with db.edit(rationale="caller id") as edit:
        card = edit.create(title="A", summary="A", yaml={"id": "fixed-id"})
    assert card.id == "fixed-id"


def test_edit_create_falsey_id_not_replaced(db):
    with db.edit(rationale="empty id") as edit:
        card = edit.create(title="A", summary="A", yaml={"id": ""})
    assert card.yaml["id"] == ""


def test_edit_create_canonical_key_order_on_disk(db):
    with db.edit(rationale="canonical order") as edit:
        card = edit.create(
            title="A",
            summary="A",
            yaml={"location": "shed", "z": 1, "tags": ["x"]},
            tags=["y"],
        )
    relpath = _index.relpath_of(db.conn, card.id)
    text = (db.root / relpath).read_text()
    fm_keys = [
        line.split(":", 1)[0]
        for line in text.split("---\n")[1].splitlines()
        if ":" in line and not line.startswith(" ")
    ]
    assert fm_keys[:4] == ["id", "title", "summary", "tags"]
    assert "location" in fm_keys
    assert "z" in fm_keys


def test_edit_update_no_tags_kwarg_preserves_existing(db, seed):
    card = seed(title="A", summary="A", tags=["original"])
    card.body = "changed"
    with db.edit(rationale="no override") as edit:
        edit.update(card, summary=card.summary)
    assert db.read(card.id).tags == ["original"]


def test_edit_update_no_tags_kwarg_when_card_has_no_tags(db, seed):
    card = seed(title="A", summary="A")
    card.body = "changed"
    with db.edit(rationale="no override no tags") as edit:
        edit.update(card, summary=card.summary)
    assert "tags" not in db.read(card.id).yaml


def test_edit_update_replaces_tags(db, seed):
    card = seed(title="A", summary="A", tags=["original"])
    with db.edit(rationale="replace") as edit:
        edit.update(card, summary=card.summary, tags=["replacement"])
    assert db.read(card.id).tags == ["replacement"]


def test_edit_update_empty_tags_removes_key_on_disk(db, seed):
    card = seed(title="A", summary="A", tags=["x"])
    with db.edit(rationale="clear") as edit:
        edit.update(card, summary=card.summary, tags=())
    relpath = _index.relpath_of(db.conn, card.id)
    assert "tags:" not in (db.root / relpath).read_text()
    assert "tags" not in db.read(card.id).yaml


def test_edit_update_empty_tags_removes_index_rows(db, seed):
    card = seed(title="A", summary="A", tags=["x", "y"])
    with db.edit(rationale="clear index") as edit:
        edit.update(card, summary=card.summary, tags=())
    rows = db.conn.execute(
        "SELECT value_str FROM entry_fields f "
        "JOIN entries e ON e.rowid = f.entry_rowid "
        "WHERE e.id = ? AND f.key = 'tags'",
        (card.id,),
    ).fetchall()
    assert rows == []


def test_edit_update_empty_tags_on_untagged_card_noops(db, seed):
    card = seed(title="A", summary="A")
    card.body = "changed"
    with db.edit(rationale="clear untagged") as edit:
        edit.update(card, summary=card.summary, tags=())
    assert "tags" not in db.read(card.id).yaml


def test_edit_update_in_place_mutation_persists(db, seed):
    card = seed(title="A", summary="A", tags=["original"])
    card.yaml["tags"].append("added")
    with db.edit(rationale="in-place") as edit:
        edit.update(card, summary=card.summary)
    assert db.read(card.id).tags == ["original", "added"]


def test_edit_create_omit_empty_disk_text(db):
    with db.edit(rationale="omit empty disk") as edit:
        card = edit.create(title="A", summary="A", tags=())
    relpath = _index.relpath_of(db.conn, card.id)
    assert "tags:" not in (db.root / relpath).read_text()


def _ids_for_tag_query(db, where_clause: str, *params) -> set:
    rows = db.conn.execute(
        f"SELECT entries.id FROM entries "
        f"JOIN entry_fields f ON f.entry_rowid = entries.rowid "
        f"WHERE f.key = 'tags' AND ({where_clause})",
        params,
    ).fetchall()
    return {r[0] for r in rows}


def test_tags_hierarchical_like_prefix(db, seed):
    a = seed(title="A", summary="A", tags=["area/work"])
    b = seed(title="B", summary="B", tags=["area/home"])
    c = seed(title="C", summary="C", tags=["topic/cosmology"])
    matched = _ids_for_tag_query(db, "f.value_str LIKE ?", "area/%")
    assert matched == {a.id, b.id}
    assert c.id not in matched


def test_tags_substring_like(db, seed):
    a = seed(title="A", summary="A", tags=["area/work"])
    b = seed(title="B", summary="B", tags=["area/home"])
    seed(title="C", summary="C", tags=["topic/cosmology"])
    matched = _ids_for_tag_query(db, "f.value_str LIKE ?", "%work%")
    assert matched == {a.id}
    assert b.id not in matched


def test_tags_self_plus_descendants(db, seed):
    parent = seed(title="P", summary="P", tags=["area"])
    work = seed(title="W", summary="W", tags=["area/work"])
    home = seed(title="H", summary="H", tags=["area/home"])
    other = seed(title="O", summary="O", tags=["topic/cosmology"])
    matched = _ids_for_tag_query(
        db, "f.value_str = ? OR f.value_str LIKE ?", "area", "area/%"
    )
    assert matched == {parent.id, work.id, home.id}
    assert other.id not in matched


def test_tags_glob_pattern(db, seed):
    a = seed(title="A", summary="A", tags=["area/work"])
    b = seed(title="B", summary="B", tags=["area/home"])
    seed(title="C", summary="C", tags=["topic/cosmology"])
    rows = db.conn.execute(
        "SELECT entries.id FROM entries "
        "JOIN entry_fields f ON f.entry_rowid = entries.rowid "
        "WHERE f.key = 'tags' AND f.value_str GLOB 'area/*'"
    ).fetchall()
    matched = {r[0] for r in rows}
    assert matched == {a.id, b.id}


def test_tags_query_matches_middle_tag_in_multi_tag_card(db, seed):
    multi = seed(
        title="A",
        summary="A",
        tags=["topic/cosmology", "area/work", "status/open"],
    )
    other = seed(title="B", summary="B", tags=["topic/cosmology"])
    matched = _ids_for_tag_query(db, "f.value_str LIKE ?", "area/%")
    assert matched == {multi.id}
    assert other.id not in matched


def test_tags_hierarchical_prefix_does_not_match_lookalikes(db, seed):
    real = seed(title="R", summary="R", tags=["area/work"])
    seed(title="L1", summary="L1", tags=["area2/work"])
    seed(title="L2", summary="L2", tags=["area-work"])
    matched = _ids_for_tag_query(db, "f.value_str LIKE ?", "area/%")
    assert matched == {real.id}
