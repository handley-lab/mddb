import pytest

import mddb
from mddb import _index


def _git_log_count(db) -> int:
    out = db._git("rev-list", "--count", "HEAD").stdout.strip()
    return int(out)


def test_edit_create_batch(db):
    before = _git_log_count(db)
    with db.editor(rationale="batch create three") as editor:
        a = editor.create(title="A", summary="A sum")
        b = editor.create(title="B", summary="B sum")
        c = editor.create(title="C", summary="C sum")
    assert _git_log_count(db) == before + 1
    assert (db.root / "a.md").exists()
    assert (db.root / "b.md").exists()
    assert (db.root / "c.md").exists()
    ids = {row[0] for row in db.conn.execute("SELECT id FROM entries").fetchall()}
    assert {a.id, b.id, c.id} <= ids
    assert db._active_editor is None


def test_edit_exception_rolls_back(db):
    before = _git_log_count(db)
    with pytest.raises(RuntimeError, match="bail"):
        with db.editor(rationale="rollback test") as editor:
            editor.create(title="A", summary="A")
            editor.create(title="B", summary="B")
            raise RuntimeError("bail")
    assert _git_log_count(db) == before
    assert not (db.root / "a.md").exists()
    assert not (db.root / "b.md").exists()
    assert db.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
    assert db._active_editor is None


def test_edit_empty_no_commit(db):
    before = _git_log_count(db)
    with db.editor(rationale="empty"):
        pass
    assert _git_log_count(db) == before
    assert db._active_editor is None


def test_edit_collision_in_buffer(db):
    with db.editor(rationale="collision in buffer") as editor:
        editor.create(title="A", summary="x", relpath="dup.md")
        with pytest.raises(FileExistsError):
            editor.create(title="B", summary="x", relpath="dup.md")


def test_edit_collision_against_disk(db, seed):
    seed(title="Existing", summary="x", relpath="seed.md")
    with db.editor(rationale="collision against disk") as editor:
        with pytest.raises(FileExistsError):
            editor.create(title="Other", summary="x", relpath="seed.md")


def test_edit_nested_raises(db):
    with db.editor(rationale="outer"):
        with pytest.raises(RuntimeError, match="nested"):
            with db.editor(rationale="inner"):
                pass


def test_edit_create_returns_copy(db):
    with db.editor(rationale="returned-card body isolation") as editor:
        card = editor.create(title="A", summary="A", body="original")
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
        with db.editor(rationale="will fail") as editor:
            editor.create(title="A", summary="A")
    monkeypatch.setattr(db, "_git", real_git)
    assert db._active_editor is None
    with db.editor(rationale="after failure") as editor:
        editor.create(title="B", summary="B")
    assert (db.root / "b.md").exists()


def test_edit_create_returns_deep_copy(db):
    with db.editor(rationale="deep copy") as editor:
        card = editor.create(title="A", summary="A", yaml={"tags": []})
        card.yaml["tags"].append("shed")
    again = db.read(card.id)
    assert again.yaml["tags"] == []


def test_edit_duplicate_id_in_buffer(db):
    with db.editor(rationale="dup id buffer") as editor:
        editor.create(title="A", summary="A", yaml={"id": "fixed"})
        with pytest.raises(RuntimeError, match="duplicate id in editor"):
            editor.create(title="B", summary="B", yaml={"id": "fixed"})


def test_edit_id_collision_against_db_fails_at_commit(db, seed):
    import sqlite3

    seed(title="X", summary="seed", yaml={"id": "fixed-id"})
    with pytest.raises(sqlite3.IntegrityError):
        with db.editor(rationale="id collision db") as editor:
            editor.create(title="Y", summary="y", yaml={"id": "fixed-id"})


def test_edit_closed_after_empty_exit(db):
    with db.editor(rationale="empty close") as editor:
        pass
    with pytest.raises(RuntimeError, match="already closed"):
        editor.create(title="A", summary="A")


def test_edit_closed_after_body_exception(db):
    with pytest.raises(ValueError):
        with db.editor(rationale="body raise") as editor:
            raise ValueError("oops")
    with pytest.raises(RuntimeError, match="already closed"):
        editor.create(title="A", summary="A")


def test_edit_closed_after_successful_commit(db):
    with db.editor(rationale="ok") as editor:
        editor.create(title="A", summary="A")
    with pytest.raises(RuntimeError, match="already closed"):
        editor.create(title="B", summary="B")


def test_edit_read_sees_staged(db):
    with db.editor(rationale="read staged") as editor:
        card = editor.create(title="A", summary="A", body="body-a")
        again = editor.read(card.id)
        assert again.body == "body-a"


def test_edit_read_existing_then_update(db, seed):
    card = seed(title="A", summary="A", body="x")
    with db.editor(rationale="update existing") as editor:
        existing = editor.read(card.id)
        existing.body = "y"
        editor.update(existing, summary="A-updated")
    again = db.read(card.id)
    assert again.body == "y"
    assert again.summary == "A-updated"


def test_edit_create_then_update(db):
    before = _git_log_count(db)
    with db.editor(rationale="create+update") as editor:
        card = editor.create(title="A", summary="A")
        editor.update(card, summary="new summary")
    assert _git_log_count(db) == before + 1
    assert db.read(card.id).summary == "new summary"


def test_edit_create_then_delete(db):
    before = _git_log_count(db)
    with db.editor(rationale="create+delete") as editor:
        card = editor.create(title="A", summary="A", relpath="will-vanish.md")
        editor.delete(card.id)
    assert _git_log_count(db) == before
    assert not (db.root / "will-vanish.md").exists()
    assert db.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0


def test_edit_delete_then_create_same_relpath(db, seed):
    old = seed(title="Old", summary="old", relpath="slot.md")
    before = _git_log_count(db)
    with db.editor(rationale="swap") as editor:
        editor.delete(old.id)
        new = editor.create(title="New", summary="new", relpath="slot.md")
    assert _git_log_count(db) == before + 1
    again = db.read(new.id)
    assert again.title == "New"
    with pytest.raises(KeyError):
        db.read(old.id)


def test_edit_move_existing(db, seed):
    card = seed(title="A", summary="A", relpath="orig.md")
    before = _git_log_count(db)
    with db.editor(rationale="move") as editor:
        editor.move(card.id, "moved.md")
    assert _git_log_count(db) == before + 1
    assert not (db.root / "orig.md").exists()
    assert (db.root / "moved.md").exists()
    assert _index.relpath_of(db.conn, card.id) == "moved.md"


def test_edit_move_to_subdirectory(db, seed):
    card = seed(title="A", summary="A", relpath="flat.md")
    with db.editor(rationale="move to subdir") as editor:
        editor.move(card.id, "sub/dir/here.md")
    assert (db.root / "sub" / "dir" / "here.md").exists()


def test_edit_move_only_does_not_rewrite_body(db, seed):
    card = seed(title="A", summary="A", body="byte-exact body\n", relpath="orig.md")
    original_text = (db.root / "orig.md").read_text()
    with db.editor(rationale="move only") as editor:
        editor.move(card.id, "new.md")
    assert (db.root / "new.md").read_text() == original_text


def test_edit_read_after_move_only(db, seed):
    card = seed(title="A", summary="A", body="hello", relpath="orig.md")
    with db.editor(rationale="read after move-only") as editor:
        editor.move(card.id, "moved.md")
        c = editor.read(card.id)
        assert c.body == "hello"


def test_edit_modify_after_delete_raises(db, seed):
    card = seed(title="A", summary="A")
    with db.editor(rationale="modify-after-delete") as editor:
        editor.delete(card.id)
        with pytest.raises(KeyError):
            editor.read(card.id)
        with pytest.raises(KeyError):
            editor.update(card, summary="x")
        with pytest.raises(KeyError):
            editor.move(card.id, "new.md")


def test_edit_move_into_staged_deleted_slot_raises(db, seed):
    a = seed(title="A", summary="A", relpath="slot.md")
    b = seed(title="B", summary="B", relpath="other.md")
    with db.editor(rationale="move into deleted slot") as editor:
        editor.delete(a.id)
        with pytest.raises(FileExistsError):
            editor.move(b.id, "slot.md")


def test_edit_move_staged_create(db):
    with db.editor(rationale="move staged create") as editor:
        card = editor.create(title="A", summary="A", relpath="initial.md")
        editor.move(card.id, "moved.md")
    assert not (db.root / "initial.md").exists()
    assert (db.root / "moved.md").exists()


def test_edit_move_same_path_is_noop(db, seed):
    card = seed(title="A", summary="A")
    current = _index.relpath_of(db.conn, card.id)
    before = _git_log_count(db)
    with db.editor(rationale="self move") as editor:
        editor.move(card.id, current)
    assert _git_log_count(db) == before


def test_edit_create_into_move_away_slot_raises(db, seed):
    a = seed(title="A", summary="A", relpath="orig.md")
    with db.editor(rationale="create into move-away") as editor:
        editor.move(a.id, "new.md")
        with pytest.raises(FileExistsError):
            editor.create(title="B", summary="B", relpath="orig.md")


def test_edit_move_into_move_away_slot_raises(db, seed):
    a = seed(title="A", summary="A", relpath="orig.md")
    b = seed(title="B", summary="B", relpath="other.md")
    with db.editor(rationale="move into move-away") as editor:
        editor.move(a.id, "new.md")
        with pytest.raises(FileExistsError):
            editor.move(b.id, "orig.md")


def test_edit_update_then_move(db, seed):
    card = seed(title="A", summary="A", body="x", relpath="orig.md")
    with db.editor(rationale="update+move") as editor:
        card.body = "y"
        editor.update(card, summary="A-updated")
        editor.move(card.id, "moved.md")
    assert not (db.root / "orig.md").exists()
    assert (db.root / "moved.md").exists()
    again = db.read(card.id)
    assert again.body == "y"
    assert again.summary == "A-updated"


def test_edit_delete_after_staged_move(db, seed):
    card = seed(title="A", summary="A", relpath="orig.md")
    with db.editor(rationale="move then delete") as editor:
        editor.move(card.id, "moved.md")
        editor.delete(card.id)
    assert not (db.root / "orig.md").exists()
    assert not (db.root / "moved.md").exists()
    with pytest.raises(KeyError):
        db.read(card.id)


def test_edit_read_returns_deep_copy_for_updates(db, seed):
    card = seed(title="A", summary="A", yaml={"tags": []})
    with db.editor(rationale="deep copy update") as editor:
        card.body = "changed"
        returned = editor.update(card, summary="A")
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
        with db.editor(rationale="sqlite fails") as editor:
            card = editor.create(title="A", summary="A")
            staged_id = card.id
    monkeypatch.setattr(db, "_git", real_git)
    assert db._active_editor is None
    assert (db.root / "a.md").exists()
    from mddb._index import cache_path

    cache_path(db.root).unlink()
    fresh = mddb.MDDB(db.root)
    recovered = fresh.read(staged_id)
    assert recovered.title == "A"
    with fresh.editor(rationale="after sqlite failure") as editor:
        editor.create(title="B", summary="B")
    assert (db.root / "b.md").exists()


def test_edit_move_into_committed_unstaged_card_relpath_raises(db, seed):
    a = seed(title="A", summary="A", relpath="alpha.md")
    b = seed(title="B", summary="B", relpath="beta.md")
    with db.editor(rationale="move into committed") as editor:
        with pytest.raises(FileExistsError):
            editor.move(a.id, "beta.md")
    assert _index.relpath_of(db.conn, a.id) == "alpha.md"
    assert _index.relpath_of(db.conn, b.id) == "beta.md"


def test_edit_move_away_and_back_collapses(db, seed):
    card = seed(title="A", summary="A", relpath="orig.md")
    before = _git_log_count(db)
    with db.editor(rationale="move away and back") as editor:
        editor.move(card.id, "new.md")
        editor.move(card.id, "orig.md")
    assert _git_log_count(db) == before
    assert (db.root / "orig.md").exists()
    assert not (db.root / "new.md").exists()


def test_edit_update_input_card_is_copied(db, seed):
    card = seed(title="A", summary="A", yaml={"tags": []})
    with db.editor(rationale="input copy") as editor:
        card.body = "changed"
        editor.update(card, summary=card.summary)
        card.yaml["tags"].append("shed")
    again = db.read(card.id)
    assert again.body == "changed"
    assert again.yaml["tags"] == []


def test_edit_read_after_move_plus_update(db, seed):
    card = seed(title="A", summary="A")
    with db.editor(rationale="move+update read") as editor:
        editor.update(card, summary="new")
        editor.move(card.id, "moved.md")
        c = editor.read(card.id)
        assert c.summary == "new"


def test_edit_move_collision_against_staged_create(db, seed):
    card = seed(title="A", summary="A", relpath="orig.md")
    with db.editor(rationale="move collide staged create") as editor:
        editor.create(title="B", summary="B", relpath="claimed.md")
        with pytest.raises(FileExistsError):
            editor.move(card.id, "claimed.md")


def test_edit_read_after_create_then_delete_in_buffer_raises(db):
    with db.editor(rationale="create+delete read") as editor:
        card = editor.create(title="A", summary="A")
        editor.delete(card.id)
        with pytest.raises(KeyError):
            editor.read(card.id)


def test_edit_closed_after_commit_phase_failure(db, monkeypatch):
    real_git = db._git

    def fail_commit(*args):
        if args and args[0] == "commit":
            raise RuntimeError("simulated commit failure")
        return real_git(*args)

    monkeypatch.setattr(db, "_git", fail_commit)
    with pytest.raises(RuntimeError, match="simulated"):
        with db.editor(rationale="will fail") as editor:
            editor.create(title="A", summary="A")
    with pytest.raises(RuntimeError, match="already closed"):
        editor.create(title="B", summary="B")
    with pytest.raises(RuntimeError, match="already closed"):
        with editor:
            pass


def test_edit_create_tags_kwarg_basic(db):
    with db.editor(rationale="tags basic") as editor:
        card = editor.create(
            title="A", summary="A", tags=["area/work", "topic/cosmology"]
        )
    again = db.read(card.id)
    assert again.tags == ["area/work", "topic/cosmology"]


def test_edit_create_no_tags_omits_key(db):
    with db.editor(rationale="no tags") as editor:
        card = editor.create(title="A", summary="A")
    assert "tags" not in db.read(card.id).yaml


def test_edit_create_empty_tuple_omits_key(db):
    with db.editor(rationale="empty tuple") as editor:
        card = editor.create(title="A", summary="A", tags=())
    assert "tags" not in db.read(card.id).yaml


def test_edit_create_empty_list_omits_key(db):
    with db.editor(rationale="empty list") as editor:
        card = editor.create(title="A", summary="A", tags=[])
    assert "tags" not in db.read(card.id).yaml


def test_edit_create_tags_kwarg_wins_over_yaml(db):
    with db.editor(rationale="kwarg wins") as editor:
        card = editor.create(title="A", summary="A", yaml={"tags": ["x"]}, tags=["y"])
    assert db.read(card.id).tags == ["y"]


def test_edit_create_empty_tags_clears_yaml_tags(db):
    with db.editor(rationale="empty clears") as editor:
        card = editor.create(title="A", summary="A", yaml={"tags": ["x"]}, tags=())
    assert "tags" not in db.read(card.id).yaml


def test_edit_create_none_tags_preserves_yaml_tags(db):
    with db.editor(rationale="preserve yaml") as editor:
        card = editor.create(title="A", summary="A", yaml={"tags": ["x"]}, tags=None)
    assert db.read(card.id).tags == ["x"]


def test_edit_create_omitted_tags_preserves_yaml_tags(db):
    with db.editor(rationale="implicit preserve") as editor:
        card = editor.create(title="A", summary="A", yaml={"tags": ["x"]})
    assert db.read(card.id).tags == ["x"]


def test_edit_create_yaml_empty_tags_preserved(db):
    with db.editor(rationale="raw empty preserved") as editor:
        card = editor.create(title="A", summary="A", yaml={"tags": []})
    assert db.read(card.id).yaml["tags"] == []


def test_edit_create_required_kwargs_win_over_yaml(db):
    with db.editor(rationale="kwargs win") as editor:
        card = editor.create(
            title="from kwarg",
            summary="kwarg summary",
            yaml={"title": "from yaml", "summary": "yaml summary"},
        )
    again = db.read(card.id)
    assert again.title == "from kwarg"
    assert again.summary == "kwarg summary"


def test_edit_create_caller_supplied_id_preserved(db):
    with db.editor(rationale="caller id") as editor:
        card = editor.create(title="A", summary="A", yaml={"id": "fixed-id"})
    assert card.id == "fixed-id"


def test_edit_create_falsey_id_not_replaced(db):
    with db.editor(rationale="empty id") as editor:
        card = editor.create(title="A", summary="A", yaml={"id": ""})
    assert card.yaml["id"] == ""


def test_edit_create_canonical_key_order_on_disk(db):
    with db.editor(rationale="canonical order") as editor:
        card = editor.create(
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
    with db.editor(rationale="no override") as editor:
        editor.update(card, summary=card.summary)
    assert db.read(card.id).tags == ["original"]


def test_edit_update_no_tags_kwarg_when_card_has_no_tags(db, seed):
    card = seed(title="A", summary="A")
    card.body = "changed"
    with db.editor(rationale="no override no tags") as editor:
        editor.update(card, summary=card.summary)
    assert "tags" not in db.read(card.id).yaml


def test_edit_update_replaces_tags(db, seed):
    card = seed(title="A", summary="A", tags=["original"])
    with db.editor(rationale="replace") as editor:
        editor.update(card, summary=card.summary, tags=["replacement"])
    assert db.read(card.id).tags == ["replacement"]


def test_edit_update_empty_tags_removes_key_on_disk(db, seed):
    card = seed(title="A", summary="A", tags=["x"])
    with db.editor(rationale="clear") as editor:
        editor.update(card, summary=card.summary, tags=())
    relpath = _index.relpath_of(db.conn, card.id)
    assert "tags:" not in (db.root / relpath).read_text()
    assert "tags" not in db.read(card.id).yaml


def test_edit_update_empty_tags_removes_index_rows(db, seed):
    card = seed(title="A", summary="A", tags=["x", "y"])
    with db.editor(rationale="clear index") as editor:
        editor.update(card, summary=card.summary, tags=())
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
    with db.editor(rationale="clear untagged") as editor:
        editor.update(card, summary=card.summary, tags=())
    assert "tags" not in db.read(card.id).yaml


def test_edit_update_in_place_mutation_persists(db, seed):
    card = seed(title="A", summary="A", tags=["original"])
    card.yaml["tags"].append("added")
    with db.editor(rationale="in-place") as editor:
        editor.update(card, summary=card.summary)
    assert db.read(card.id).tags == ["original", "added"]


def test_edit_create_omit_empty_disk_text(db):
    with db.editor(rationale="omit empty disk") as editor:
        card = editor.create(title="A", summary="A", tags=())
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


def test_editor_edit_basic_replace(db, seed):
    card = seed(title="A", summary="A", body="foo teh bar")
    with db.editor(rationale="fix typo") as editor:
        n = editor.edit(card.id, "teh", "the")
    assert n == 1
    assert db.read(card.id).body == "foo the bar"


def test_editor_edit_replace_all(db, seed):
    card = seed(title="A", summary="A", body="a a a")
    with db.editor(rationale="replace all") as editor:
        n = editor.edit(card.id, "a", "b", replace_all=True)
    assert n == 3
    assert db.read(card.id).body == "b b b"


def test_editor_edit_not_found_raises(db, seed):
    card = seed(title="A", summary="A", body="hello")
    with db.editor(rationale="missing") as editor:
        with pytest.raises(ValueError, match="not found"):
            editor.edit(card.id, "x", "y")


def test_editor_edit_not_unique_without_replace_all_raises(db, seed):
    card = seed(title="A", summary="A", body="a a")
    with db.editor(rationale="ambiguous") as editor:
        with pytest.raises(ValueError, match="2 occurrences.*replace_all"):
            editor.edit(card.id, "a", "b")


def test_editor_edit_empty_old_raises(db, seed):
    card = seed(title="A", summary="A", body="hello")
    with db.editor(rationale="empty old") as editor:
        with pytest.raises(ValueError, match="must not be empty"):
            editor.edit(card.id, "", "x")


def test_editor_edit_missing_card_id_raises(db):
    with db.editor(rationale="missing id") as editor:
        with pytest.raises(KeyError):
            editor.edit("no-such-id", "x", "y")


def test_editor_edit_on_staged_deleted_card_raises(db, seed):
    card = seed(title="A", summary="A", body="foo")
    with db.editor(rationale="modify-after-delete") as editor:
        editor.delete(card.id)
        with pytest.raises(KeyError):
            editor.edit(card.id, "foo", "bar")


def test_editor_edit_on_staged_created_card(db):
    before = _git_log_count(db)
    with db.editor(rationale="create+edit") as editor:
        card = editor.create(title="A", summary="A", body="foo")
        n = editor.edit(card.id, "foo", "bar")
    assert n == 1
    assert _git_log_count(db) == before + 1
    assert db.read(card.id).body == "bar"


def test_editor_edit_on_unstaged_disk_card(db, seed):
    card = seed(title="A", summary="A", body="foo")
    with db.editor(rationale="edit from disk") as editor:
        editor.edit(card.id, "foo", "bar")
    assert db.read(card.id).body == "bar"


def test_editor_edit_preserves_title_summary_tags(db, seed):
    card = seed(title="A", summary="A summary", tags=["x"], body="foo")
    with db.editor(rationale="preserve") as editor:
        editor.edit(card.id, "foo", "bar")
    again = db.read(card.id)
    assert again.title == "A"
    assert again.summary == "A summary"
    assert again.tags == ["x"]
    assert again.body == "bar"


def test_editor_edit_after_close_raises(db, seed):
    card = seed(title="A", summary="A", body="foo")
    with db.editor(rationale="empty") as editor:
        pass
    with pytest.raises(RuntimeError, match="already closed"):
        editor.edit(card.id, "foo", "bar")


def test_editor_edit_multiple_cards_one_commit(db, seed):
    a = seed(title="A", summary="A", body="foo")
    b = seed(title="B", summary="B", body="baz")
    before = _git_log_count(db)
    with db.editor(rationale="multi-card edit") as editor:
        editor.edit(a.id, "foo", "FOO")
        editor.edit(b.id, "baz", "BAZ")
    assert _git_log_count(db) == before + 1
    assert db.read(a.id).body == "FOO"
    assert db.read(b.id).body == "BAZ"


def test_editor_edit_on_same_card_sees_prior_staged_body(db, seed):
    card = seed(title="A", summary="A", body="a")
    with db.editor(rationale="edit-then-edit") as editor:
        editor.edit(card.id, "a", "b")
        editor.edit(card.id, "b", "c")
    assert db.read(card.id).body == "c"


def test_editor_edit_after_move_in_same_editor(db, seed):
    card = seed(title="A", summary="A", body="foo", relpath="orig.md")
    with db.editor(rationale="move-then-edit") as editor:
        editor.move(card.id, "new.md")
        editor.edit(card.id, "foo", "bar")
    assert not (db.root / "orig.md").exists()
    assert (db.root / "new.md").exists()
    assert db.read(card.id).body == "bar"


def test_editor_move_after_edit_in_same_editor(db, seed):
    card = seed(title="A", summary="A", body="foo", relpath="orig.md")
    with db.editor(rationale="edit-then-move") as editor:
        editor.edit(card.id, "foo", "bar")
        editor.move(card.id, "new.md")
    assert not (db.root / "orig.md").exists()
    assert (db.root / "new.md").exists()
    assert db.read(card.id).body == "bar"


def test_editor_edit_empty_new_removes_match(db, seed):
    card = seed(title="A", summary="A", body="keep garbage keep")
    with db.editor(rationale="strip") as editor:
        n = editor.edit(card.id, "garbage ", "")
    assert n == 1
    assert db.read(card.id).body == "keep keep"


def test_editor_edit_updates_fts_index(db, seed):
    card = seed(title="A", summary="A", body="oldterm in body")
    with db.editor(rationale="edit fts") as editor:
        editor.edit(card.id, "oldterm", "newterm")
    matched_new = [
        r[0]
        for r in db.conn.execute(
            "SELECT id FROM entries WHERE rowid IN (SELECT rowid FROM entries_fts WHERE entries_fts MATCH ?)",
            ("newterm",),
        )
    ]
    matched_old = [
        r[0]
        for r in db.conn.execute(
            "SELECT id FROM entries WHERE rowid IN (SELECT rowid FROM entries_fts WHERE entries_fts MATCH ?)",
            ("oldterm",),
        )
    ]
    assert matched_new == [card.id]
    assert matched_old == []


def test_editor_edit_with_old_equals_new_returns_count_and_stages_nothing(db, seed):
    card = seed(title="A", summary="A", body="foo")
    before = _git_log_count(db)
    with db.editor(rationale="identity") as editor:
        n = editor.edit(card.id, "foo", "foo")
    assert n == 1
    assert _git_log_count(db) == before


def test_editor_edit_with_old_equals_new_no_op_bypasses_uniqueness_check(db, seed):
    card = seed(title="A", summary="A", body="foo foo")
    before = _git_log_count(db)
    with db.editor(rationale="identity multi") as editor:
        n = editor.edit(card.id, "foo", "foo")
    assert n == 2
    assert _git_log_count(db) == before


def test_editor_edit_then_update_with_stale_snapshot_overwrites_body_change(db, seed):
    card = seed(title="A", summary="A", body="foo")
    snapshot = db.read(card.id)
    with db.editor(rationale="edit then update overwrites") as editor:
        editor.edit(card.id, "foo", "bar")
        editor.update(snapshot, summary="A-updated")
    again = db.read(card.id)
    assert again.body == "foo"
    assert again.summary == "A-updated"


def test_editor_edit_handles_multiline_substring(db, seed):
    card = seed(title="A", summary="A", body="line1\nline2\nline3")
    with db.editor(rationale="multiline edit") as editor:
        n = editor.edit(card.id, "line1\nline2", "line1\nLINE2")
    assert n == 1
    assert db.read(card.id).body == "line1\nLINE2\nline3"


def test_editor_edit_matches_unicode_codepoints_exactly_not_canonical_forms(db, seed):
    nfc = "café"
    nfd = "café"
    card_nfc = seed(title="NFC", summary="NFC", body=f"a {nfc} b")
    card_nfd = seed(title="NFD", summary="NFD", body=f"a {nfd} b")
    with db.editor(rationale="unicode match") as editor:
        n = editor.edit(card_nfc.id, nfc, "cafe")
        assert n == 1
        with pytest.raises(ValueError, match="not found"):
            editor.edit(card_nfd.id, nfc, "cafe")
    assert db.read(card_nfc.id).body == "a cafe b"
    assert db.read(card_nfd.id).body == f"a {nfd} b"


def test_editor_move_requires_md_suffix(db, seed):
    card = seed(title="A", summary="A", relpath="orig.md")
    with db.editor(rationale="reject non-md move target") as editor:
        with pytest.raises(ValueError, match="must end in .md"):
            editor.move(card.id, "inventory/shed")
    assert (db.root / "orig.md").exists()


def test_editor_create_same_id_after_staged_delete_raises(db, seed):
    card = seed(title="Old", summary="Old", yaml={"id": "fixed-id"})
    with db.editor(rationale="same id after delete") as editor:
        editor.delete(card.id)
        with pytest.raises(RuntimeError, match="duplicate id"):
            editor.create(
                title="New", summary="New", yaml={"id": "fixed-id"}, relpath="new.md"
            )


def test_index_skips_title_summary_in_entry_fields(db, seed):
    card = seed(title="A", summary="A sum", tags=["x"])
    rows = db.conn.execute(
        "SELECT key FROM entry_fields WHERE entry_rowid = "
        "(SELECT rowid FROM entries WHERE id = ?) AND key IN ('title', 'summary')",
        (card.id,),
    ).fetchall()
    assert rows == []
    tag_rows = db.conn.execute(
        "SELECT key FROM entry_fields WHERE entry_rowid = "
        "(SELECT rowid FROM entries WHERE id = ?) AND key = 'tags'",
        (card.id,),
    ).fetchall()
    assert tag_rows == [("tags",)]


def test_editor_create_rejects_path_escape(db):
    with db.editor(rationale="reject create path escape") as editor:
        with pytest.raises(ValueError, match="relative and canonical"):
            editor.create(title="A", summary="A", relpath="../escape.md")


def test_editor_move_rejects_path_escape(db, seed):
    card = seed(title="A", summary="A", relpath="orig.md")
    with db.editor(rationale="reject move path escape") as editor:
        with pytest.raises(ValueError, match="relative and canonical"):
            editor.move(card.id, "../escape.md")


def test_editor_create_rejects_non_canonical_relpath(db):
    with db.editor(rationale="reject non-canonical create") as editor:
        with pytest.raises(ValueError, match="relative and canonical"):
            editor.create(title="A", summary="A", relpath="sub/../card.md")


def test_editor_move_rejects_non_canonical_relpath(db, seed):
    card = seed(title="A", summary="A", relpath="orig.md")
    with db.editor(rationale="reject non-canonical move") as editor:
        with pytest.raises(ValueError, match="relative and canonical"):
            editor.move(card.id, "./moved.md")


def test_editor_create_rejects_in_root_symlink_alias(db):
    (db.root / "real").mkdir()
    (db.root / "link").symlink_to(db.root / "real")
    with db.editor(rationale="reject in-root symlink alias") as editor:
        with pytest.raises(ValueError, match="relative and canonical"):
            editor.create(title="A", summary="A", relpath="link/card.md")


def test_editor_commit_does_not_sweep_unrelated_staged_changes(db):
    unrelated = db.root / "unrelated.txt"
    unrelated.write_text("not a card\n")
    db._git("add", "--", "unrelated.txt")
    with db.editor(rationale="card only") as editor:
        editor.create(title="A", summary="A")
    out = db._git("show", "--name-only", "--pretty=format:", "HEAD").stdout
    committed = {line.strip() for line in out.splitlines() if line.strip()}
    assert committed == {"a.md"}
    status = db._git("status", "--porcelain").stdout
    assert "A  unrelated.txt" in status


def test_editor_create_with_path_payload(db, tmp_path):
    source = tmp_path / "src.pdf"
    source.write_bytes(b"%PDF-fake")
    with db.editor(rationale="path payload") as editor:
        editor.create(title="A", summary="A", relpath="papers/a.md", payload=source)
    assert (db.root / "papers/a.md").exists()
    assert (db.root / "papers/a.pdf").exists()
    assert (db.root / "papers/a.pdf").read_bytes() == b"%PDF-fake"
    tracked = db._git("ls-files", "--").stdout.split("\n")
    assert "papers/a.md" in tracked
    assert "papers/a.pdf" in tracked
    out = db._git("show", "--name-only", "--pretty=format:", "HEAD").stdout
    committed = {line.strip() for line in out.splitlines() if line.strip()}
    assert committed == {"papers/a.md", "papers/a.pdf"}


def test_editor_create_with_bytes_payload(db):
    with db.editor(rationale="bytes payload") as editor:
        editor.create(
            title="A",
            summary="A",
            relpath="papers/a.md",
            payload=b"\x00\x01\x02",
            payload_ext=".bin",
        )
    assert (db.root / "papers/a.md").exists()
    assert (db.root / "papers/a.bin").read_bytes() == b"\x00\x01\x02"


def test_editor_create_payload_path_with_no_suffix_raises(db, tmp_path):
    source = tmp_path / "noext"
    source.write_bytes(b"x")
    with db.editor(rationale="no suffix") as editor:
        with pytest.raises(ValueError, match="single-suffix"):
            editor.create(title="A", summary="A", relpath="a.md", payload=source)


def test_editor_create_payload_path_with_multipart_suffix_raises(db, tmp_path):
    source = tmp_path / "archive.tar.gz"
    source.write_bytes(b"gz")
    with db.editor(rationale="multi-part path") as editor:
        with pytest.raises(ValueError, match="multi-part suffix"):
            editor.create(title="A", summary="A", relpath="a.md", payload=source)
    with db.editor(rationale="multi-part path with override") as editor:
        editor.create(
            title="B",
            summary="B",
            relpath="b.md",
            payload=source,
            payload_ext=".tgz",
        )
    assert (db.root / "b.tgz").read_bytes() == b"gz"


def test_editor_create_payload_bytes_without_ext_raises(db):
    with db.editor(rationale="bytes no ext") as editor:
        with pytest.raises(ValueError, match="payload_ext"):
            editor.create(title="A", summary="A", relpath="a.md", payload=b"x")


def test_editor_create_payload_md_ext_raises(db):
    with db.editor(rationale="md ext") as editor:
        with pytest.raises(ValueError, match="cannot be .md"):
            editor.create(
                title="A", summary="A", relpath="a.md", payload=b"x", payload_ext=".md"
            )


def test_editor_create_payload_multipart_ext_raises(db):
    with db.editor(rationale="multi-part ext") as editor:
        with pytest.raises(ValueError, match="single suffix"):
            editor.create(
                title="A",
                summary="A",
                relpath="a.md",
                payload=b"x",
                payload_ext=".tar.gz",
            )


def test_editor_create_payload_sidecar_collision_raises(db, tmp_path):
    (db.root / "a.pdf").write_text("existing")
    db._git("add", "--", "a.pdf")
    db._git("commit", "-q", "-m", "seed sidecar slot")
    source = tmp_path / "new.pdf"
    source.write_bytes(b"new")
    with db.editor(rationale="sidecar collision") as editor:
        with pytest.raises(FileExistsError):
            editor.create(title="A", summary="A", relpath="a.md", payload=source)


def test_editor_create_payload_path_source_read_eagerly(db, tmp_path):
    source = tmp_path / "src.pdf"
    source.write_bytes(b"original")
    with db.editor(rationale="eager read") as editor:
        editor.create(title="A", summary="A", relpath="a.md", payload=source)
        source.write_bytes(b"MUTATED")
    assert (db.root / "a.pdf").read_bytes() == b"original"


def test_editor_create_payload_then_update_preserves_sidecar(db):
    with db.editor(rationale="create then update") as editor:
        card = editor.create(
            title="A",
            summary="A",
            relpath="a.md",
            payload=b"sidecar",
            payload_ext=".pdf",
        )
        editor.update(card, summary="updated")
    assert (db.root / "a.md").exists()
    assert (db.root / "a.pdf").read_bytes() == b"sidecar"
    assert db.read(card.id).summary == "updated"


def test_mddb_sidecar_relpaths(db, tmp_path):
    source = tmp_path / "src.pdf"
    source.write_bytes(b"pdf-bytes")
    with db.editor(rationale="seed blob") as editor:
        blob_card = editor.create(
            title="Blob", summary="Blob", relpath="papers/blob.md", payload=source
        )
    assert db.sidecar_relpaths(blob_card.id) == ["papers/blob.pdf"]

    with db.editor(rationale="plain card") as editor:
        plain = editor.create(title="Plain", summary="Plain", relpath="plain.md")
    assert db.sidecar_relpaths(plain.id) == []

    (db.root / "papers/blob.png").write_bytes(b"png-bytes")
    db._git("add", "--", "papers/blob.png")
    db._git("commit", "-q", "-m", "add a second tracked sibling")
    assert db.sidecar_relpaths(blob_card.id) == [
        "papers/blob.pdf",
        "papers/blob.png",
    ]

    with db.editor(rationale="add extra .md sibling") as editor:
        editor.create(
            title="Notes extra", summary="extra", relpath="papers/blob.extra.md"
        )
    assert db.sidecar_relpaths(blob_card.id) == [
        "papers/blob.pdf",
        "papers/blob.png",
    ]

    with pytest.raises(KeyError):
        db.sidecar_relpaths("nope")
