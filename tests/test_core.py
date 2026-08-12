import os
import shutil
import subprocess

import pytest

import mddb
from mddb._index import blob_on_disk, cache_path
from mddb.card import Card


def test_init_rolls_back_a_failed_bootstrap(tmp_path, monkeypatch):
    """A bootstrap whose commit fails (no committer identity) leaves no half-born
    deck — a bare ``.git`` that a later ``MDDB(path)`` would open and then choke on."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    for var in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "EMAIL",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
    ):
        monkeypatch.delenv(var, raising=False)
    deck = tmp_path / "deck"
    with pytest.raises(subprocess.CalledProcessError):
        mddb.MDDB.init(deck)
    assert not (deck / ".git").exists()
    assert not (deck / ".gitignore").exists()


def test_init_refuses_an_existing_repo(tmp_path):
    """init is the fresh entry point: a pre-existing ``.git`` raises before any
    mutation, so the failure rollback can never delete a repo init didn't create."""
    deck = tmp_path / "deck"
    deck.mkdir()
    subprocess.run(["git", "init", "-q", deck], check=True)
    sentinel = deck / ".git" / "SENTINEL"
    sentinel.write_text("real repo")
    with pytest.raises(FileExistsError):
        mddb.MDDB.init(deck)
    assert sentinel.read_text() == "real repo"


def test_init_preserves_a_pre_existing_gitignore(tmp_path):
    """A non-git dir with a ``.gitignore`` is refused before any mutation — the
    failure rollback removes only the ``.gitignore`` init itself would create."""
    deck = tmp_path / "deck"
    deck.mkdir()
    (deck / ".gitignore").write_text("secrets/\n")
    with pytest.raises(FileExistsError):
        mddb.MDDB.init(deck)
    assert (deck / ".gitignore").read_text() == "secrets/\n"
    assert not (deck / ".git").exists()


def test_create_read(db, seed):
    card = seed(
        title="Wheelbarrow",
        summary="A garden tool kept in the shed.",
        yaml={"tags": ["shed"], "location": "shed"},
        body="wheelbarrow\n",
        rationale="bought today",
    )
    again = db.read(card.id)
    assert again.yaml["location"] == "shed"
    assert again.body == "wheelbarrow\n"


def test_update(db, seed):
    card = seed(
        title="Shed Inventory",
        summary="Tools and equipment in the shed.",
        yaml={"location": "shed"},
        body="a",
    )
    card.yaml["location"] = "barn"
    with db.editor(rationale="moved shed contents to barn") as editor:
        editor.update(card, summary="Tools and equipment, moved to the barn.")
    assert db.read(card.id).yaml["location"] == "barn"


def test_delete(db, seed):
    card = seed(title="Disposable", summary="A card created so we can verify delete.")
    with db.editor(rationale="verifying removal makes read raise") as editor:
        editor.delete(card.id)
    with pytest.raises(KeyError):
        db.read(card.id)


def test_move_keeps_id(db, seed):
    card = seed(
        title="Flat Card",
        summary="A card initially at the root.",
        body="contents",
    )
    with db.editor(rationale="reorganised into subfolder") as editor:
        editor.move(card.id, "moved/here.md")
    again = db.read(card.id)
    assert again.id == card.id
    assert again.body == "contents"
    assert (db.root / "moved" / "here.md").exists()


def test_fts_via_conn(db, seed):
    card = seed(
        title="Tool Audit",
        summary="An audit of garden tools.",
        yaml={"tags": ["shed"]},
        body="wheelbarrow and spade",
    )
    rows = db.conn.execute(
        "SELECT id FROM entries WHERE rowid IN (SELECT rowid FROM entries_fts WHERE entries_fts MATCH ?)",
        ("wheelbarrow",),
    ).fetchall()
    assert [r[0] for r in rows] == [card.id]


def test_field_filter_via_conn(db, seed):
    a = seed(title="Shed", summary="Shed-tagged card.", yaml={"tags": ["shed"]})
    seed(title="Fridge", summary="Fridge-tagged card.", yaml={"tags": ["fridge"]})
    rows = db.conn.execute(
        "SELECT entries.id FROM entries JOIN entry_fields ON entry_fields.entry_rowid = entries.rowid "
        "WHERE entry_fields.key = ? AND entry_fields.value_str = ?",
        ("tags", "shed"),
    ).fetchall()
    assert [r[0] for r in rows] == [a.id]


def test_history(db, seed):
    card = seed(
        title="Counter",
        summary="A card whose body counter we bump.",
        body="hello",
        rationale="initial commit message",
    )
    card.yaml["x"] = 2
    with db.editor(rationale="bumped x") as editor:
        editor.update(card, summary=card.summary)
    commits = db.history(card.id)
    assert [c["message"].strip() for c in commits] == [
        "bumped x",
        "initial commit message",
    ]


def test_relpath_explicit(db, seed):
    seed(title="Shed", summary="Shed-located audit.", relpath="inventory/shed.md")
    assert (db.root / "inventory" / "shed.md").exists()


def test_relpath_default_slug(db, seed):
    seed(title="Fridge Inventory", summary="Items in the fridge.")
    assert (db.root / "fridge-inventory.md").exists()


def test_relpath_no_suffix_treated_as_directory(db, seed):
    seed(title="Fridge", summary="Items in the fridge.", relpath="inventory/cold")
    assert (db.root / "inventory" / "cold" / "fridge.md").exists()


def test_cache_rebuild(db, seed):
    card = seed(
        title="Persistent",
        summary="A card whose cache we will delete.",
        body="hello",
        yaml={"x": 1},
    )
    root = db.root
    from mddb._index import cache_path

    db.conn.close()
    cache_path(root).unlink()
    db2 = mddb.MDDB(root)
    assert db2.read(card.id).yaml["x"] == 1


def test_cache_rebuild_needs_only_read_access_to_deck_lock(db, seed):
    seed(title="Persistent", summary="s")
    from mddb._index import cache_path

    db.conn.close()
    cache_path(db.root).unlink()
    git_dir = db.root / ".git"
    git_dir.chmod(0o500)
    try:
        assert len(mddb.MDDB(db.root).list()) == 1
    finally:
        git_dir.chmod(0o700)


def test_cache_rebuild_ignores_untracked_markdown(db):
    from mddb._index import cache_path

    stray = db.root / "broken.md"
    stray.write_text("not a card\n")
    db.conn.close()
    cache_path(db.root).unlink()
    assert mddb.MDDB(db.root).list() == []


def test_cache_rebuild_captures_head_inside_lock(db, seed, monkeypatch):
    from contextlib import contextmanager

    from mddb import _index

    seed(title="Before", summary="s")
    db.conn.close()
    _index.cache_path(db.root).unlink()
    real_lock = _index.deck_lock

    @contextmanager
    def commit_before_lock_yields(root):
        card = root / "during.md"
        card.write_text("---\nid: during\ntitle: During\nsummary: s\n---\n")
        db._git("add", "--", card.name)
        db._git("commit", "-q", "-m", "commit during lock acquisition")
        with real_lock(root):
            yield

    monkeypatch.setattr(_index, "deck_lock", commit_before_lock_yields)
    reopened = mddb.MDDB(db.root)
    assert {card["title"] for card in reopened.list()} == {"Before", "During"}
    assert _index.git_head(reopened.conn) == reopened.head()


def _external_commit(db, message="external commit", date=""):
    env = os.environ | (
        {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date} if date else {}
    )
    subprocess.run(["git", "-C", str(db.root), "add", "-A"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(db.root), "commit", "-q", "-m", message],
        check=True,
        env=env,
    )


def _card_text(card_id, title="Card", summary="s", body=""):
    return f"---\nid: {card_id}\ntitle: {title}\nsummary: {summary}\n---\n{body}"


def _reopen_without_rebuild(db, monkeypatch):
    from mddb import _index

    db.conn.close()

    def unexpected(*_args, **_kwargs):
        raise AssertionError("full rebuild used for an ancestor HEAD advance")

    monkeypatch.setattr(_index, "rebuild_index", unexpected)
    return mddb.MDDB(db.root)


def test_stale_cache_refreshes_external_add_modify_delete_and_rename(
    db, seed, monkeypatch
):
    kept = seed(title="Kept", summary="before", relpath="kept.md")
    deleted = seed(title="Deleted", summary="gone", relpath="deleted.md")
    (db.root / "added.md").write_text(_card_text("added", title="Added"))
    (db.root / "kept.md").write_text(_card_text(kept.id, title="Kept", summary="after"))
    (db.root / "deleted.md").unlink()
    db._git("mv", "kept.md", "renamed.md")
    _external_commit(db)

    refreshed = _reopen_without_rebuild(db, monkeypatch)
    rows = {
        row[0]: row
        for row in refreshed.conn.execute("SELECT id, relpath, summary FROM entries")
    }
    assert rows[kept.id][1:] == ("renamed.md", "after")
    assert rows["added"][1] == "added.md"
    assert deleted.id not in rows


def test_stale_cache_refreshes_id_replacement_at_same_path(db, seed, monkeypatch):
    old = seed(title="Old", summary="s", relpath="same.md")
    (db.root / "same.md").write_text(_card_text("new", title="New"))
    _external_commit(db)
    refreshed = _reopen_without_rebuild(db, monkeypatch)
    assert {row["id"] for row in refreshed.list()} == {"new"}
    assert old.id != "new"


def test_stale_cache_refreshes_paired_blob_add_delete_and_move(db, seed, monkeypatch):
    first = seed(title="First", summary="s", relpath="first.md")
    second = seed(title="Second", summary="s", relpath="second.md")
    (db.root / "first.pdf").write_bytes(b"first")
    _external_commit(db, "add blob")
    refreshed = _reopen_without_rebuild(db, monkeypatch)
    assert (
        next(row for row in refreshed.list() if row["id"] == first.id)["blob_relpath"]
        == "first.pdf"
    )

    (db.root / "first.pdf").rename(db.root / "second.pdf")
    _external_commit(refreshed, "move blob")
    moved = _reopen_without_rebuild(refreshed, monkeypatch)
    rows = {row["id"]: row["blob_relpath"] for row in moved.list()}
    assert rows == {first.id: None, second.id: "second.pdf"}

    (db.root / "second.pdf").unlink()
    _external_commit(moved, "delete blob")
    removed = _reopen_without_rebuild(moved, monkeypatch)
    assert all(row["blob_relpath"] is None for row in removed.list())


def test_stale_cache_refreshes_across_several_commits(db, seed, monkeypatch):
    card = seed(title="Card", summary="zero", relpath="card.md")
    for summary in ("one", "two", "three"):
        (db.root / "card.md").write_text(_card_text(card.id, summary=summary))
        _external_commit(db, summary)
    refreshed = _reopen_without_rebuild(db, monkeypatch)
    assert (
        refreshed.conn.execute(
            "SELECT summary FROM entries WHERE id = ?", (card.id,)
        ).fetchone()[0]
        == "three"
    )


def test_refresh_delete_readd_identical_bytes_resets_lineage(db, monkeypatch):
    original = _card_text("same", title="Same")
    path = db.root / "same.md"
    path.write_text(original)
    _external_commit(db, "first add", "2024-01-01T00:00:00+00:00")
    db.conn.close()
    db = mddb.MDDB(db.root)
    path.unlink()
    _external_commit(db, "delete", "2025-01-01T00:00:00+00:00")
    path.write_text(original)
    _external_commit(db, "second add", "2026-01-01T00:00:00+00:00")
    refreshed = _reopen_without_rebuild(db, monkeypatch)
    assert (
        refreshed.conn.execute(
            "SELECT first_commit FROM entries WHERE id = 'same'"
        ).fetchone()[0]
        == "2026-01-01T00:00:00+00:00"
    )


def test_nonancestor_head_forces_rebuild(db, seed, monkeypatch):
    from mddb import _index

    seed(title="Old", summary="s")
    db.conn.close()
    db._git("checkout", "--orphan", "replacement")
    for path in db.root.glob("*.md"):
        path.unlink()
    (db.root / "new.md").write_text(_card_text("new", title="New"))
    _external_commit(db, "replacement history")
    real = _index.rebuild_index
    calls = []

    def recording(*args, **kwargs):
        calls.append(True)
        return real(*args, **kwargs)

    monkeypatch.setattr(_index, "rebuild_index", recording)
    reopened = mddb.MDDB(db.root)
    assert calls == [True]
    assert {row["id"] for row in reopened.list()} == {"new"}


def test_refresh_failure_rolls_back_rows_and_cached_head(db, seed):
    from mddb import _index

    card = seed(title="Good", summary="s", relpath="card.md")
    cached_head = _index.git_head(db.conn)
    (db.root / "card.md").write_text("not a card\n")
    _external_commit(db, "malformed external card")
    db.conn.close()
    with pytest.raises(ValueError, match="malformed frontmatter"):
        mddb.MDDB(db.root)
    conn = _index.open_index_readonly(db.root)
    assert _index.git_head(conn) == cached_head
    assert conn.execute("SELECT id FROM entries").fetchone()[0] == card.id


def test_rebuild_reads_cards_and_blobs_from_captured_commit(db, seed):
    from mddb import _index

    card = seed(title="Committed", summary="clean", relpath="card.md")
    (db.root / "card.md").write_text(_card_text(card.id, summary="dirty"))
    (db.root / "card.pdf").write_bytes(b"untracked")
    db.conn.close()
    _index.cache_path(db.root).unlink()
    rebuilt = mddb.MDDB(db.root)
    assert rebuilt.conn.execute(
        "SELECT summary, blob_relpath FROM entries WHERE id = ?", (card.id,)
    ).fetchone() == ("clean", None)


def test_rebuild_rejects_two_tracked_blobs(db, seed):
    seed(title="Scan", summary="s", relpath="scan.md")
    (db.root / "scan.pdf").write_bytes(b"a")
    (db.root / "scan.png").write_bytes(b"b")
    _external_commit(db, "ambiguous blobs")
    db.conn.close()
    cache_path(db.root).unlink()
    with pytest.raises(ValueError, match="multiple blobs"):
        mddb.MDDB(db.root)


def test_refresh_reads_changed_card_and_blob_membership_from_commit(db, seed):
    card = seed(title="Committed", summary="before", relpath="card.md")
    (db.root / "card.md").write_text(_card_text(card.id, summary="committed"))
    _external_commit(db, "committed update")
    (db.root / "card.md").write_text(_card_text(card.id, summary="dirty"))
    (db.root / "card.pdf").write_bytes(b"untracked")
    db.conn.close()
    refreshed = mddb.MDDB(db.root)
    assert refreshed.conn.execute(
        "SELECT summary, blob_relpath FROM entries WHERE id = ?", (card.id,)
    ).fetchone() == ("committed", None)


def test_refresh_with_unavailable_cached_commit_rebuilds(db, seed, monkeypatch):
    from mddb import _index

    card = seed(title="Card", summary="s")
    with db.conn:
        _index.set_git_head(db.conn, "f" * 40)
    db.conn.close()
    real = _index.rebuild_index
    calls = []

    def recording(*args, **kwargs):
        calls.append(True)
        return real(*args, **kwargs)

    monkeypatch.setattr(_index, "rebuild_index", recording)
    reopened = mddb.MDDB(db.root)
    assert calls == [True]
    assert {row["id"] for row in reopened.list()} == {card.id}


def test_refresh_includes_changes_reached_through_a_merge(db, seed, monkeypatch):
    base = seed(title="Base", summary="before", relpath="base.md")
    db._git("checkout", "-b", "side")
    (db.root / "base.md").write_text(_card_text(base.id, summary="from side"))
    _external_commit(db, "side update")
    db._git("checkout", "master")
    (db.root / "main.md").write_text(_card_text("main", title="Main"))
    _external_commit(db, "main addition")
    db._git("merge", "--no-edit", "side")
    refreshed = _reopen_without_rebuild(db, monkeypatch)
    rows = {
        row[0]: row[1]
        for row in refreshed.conn.execute("SELECT id, summary FROM entries")
    }
    assert rows == {base.id: "from side", "main": "s"}


def test_refreshed_cache_matches_fresh_rebuild(db, seed):
    from mddb import _index

    card = seed(title="Card", summary="before", relpath="card.md")
    (db.root / "card.md").write_text(_card_text(card.id, summary="after", body="body"))
    (db.root / "card.pdf").write_bytes(b"blob")
    _external_commit(db)
    db.conn.close()
    refreshed = mddb.MDDB(db.root)
    query = "SELECT id, relpath, title, summary, kind, blob_relpath, first_commit, yaml_text, body FROM entries"
    incremental_rows = refreshed.conn.execute(query).fetchall()
    refreshed.conn.close()
    _index.cache_path(db.root).unlink()
    rebuilt = mddb.MDDB(db.root)
    assert rebuilt.conn.execute(query).fetchall() == incremental_rows
    assert rebuilt.conn.execute("SELECT count(*) FROM sqlite_stat1").fetchone()[0] > 0


def test_list_progressive_disclosure(db, seed):
    a = seed(
        title="Fridge",
        summary="What's in the fridge.",
        yaml={"tags": ["fridge"]},
        body="milk, eggs",
    )
    b = seed(title="Shed", summary="Tools and equipment.")
    entries = sorted(db.list(), key=lambda e: e["title"])
    assert entries == [
        {
            "id": a.id,
            "title": "Fridge",
            "summary": "What's in the fridge.",
            "kind": None,
            "blob_relpath": None,
        },
        {
            "id": b.id,
            "title": "Shed",
            "summary": "Tools and equipment.",
            "kind": None,
            "blob_relpath": None,
        },
    ]


def test_card_title_summary_properties(db, seed):
    card = seed(title="Fridge", summary="What's in the fridge.", body="milk")
    again = db.read(card.id)
    assert again.title == "Fridge"
    assert again.summary == "What's in the fridge."


def test_card_properties_raise_on_missing_keys():
    card = Card(yaml={}, body="")
    with pytest.raises(KeyError):
        _ = card.id
    with pytest.raises(KeyError):
        _ = card.title
    with pytest.raises(KeyError):
        _ = card.summary


def test_mddb_init_sets_active_editor_none(tmp_path):
    new_db = mddb.MDDB.init(tmp_path)
    assert new_db._active_editor is None


def test_init_at_reused_path_discards_the_former_decks_cache(tmp_path):
    old = mddb.MDDB.init(tmp_path / "deck")
    with old.editor(rationale="seed former deck") as editor:
        editor.create(title="Ghost", summary="must not survive")
    old.conn.close()
    shutil.rmtree(old.root / ".git")
    (old.root / ".gitignore").unlink()
    for path in old.root.glob("*.md"):
        path.unlink()

    fresh = mddb.MDDB.init(old.root)
    assert fresh.list() == []
    assert (
        fresh.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        == mddb._index.SCHEMA_VERSION
    )


def test_blob_on_disk_finds_single_blob(tmp_path):
    (tmp_path / "notes.md").write_text("x")
    (tmp_path / "notes.pdf").write_bytes(b"p")
    assert blob_on_disk(tmp_path / "notes.md") == tmp_path / "notes.pdf"


def test_blob_on_disk_none_when_absent(tmp_path):
    (tmp_path / "notes.md").write_text("x")
    assert blob_on_disk(tmp_path / "notes.md") is None


def test_blob_on_disk_raises_on_two_blobs(tmp_path):
    (tmp_path / "notes.md").write_text("x")
    (tmp_path / "notes.pdf").write_bytes(b"p")
    (tmp_path / "notes.png").write_bytes(b"q")
    with pytest.raises(ValueError, match="multiple blobs"):
        blob_on_disk(tmp_path / "notes.md")


def test_blob_on_disk_ignores_no_suffix_sibling(tmp_path):
    (tmp_path / "notes.md").write_text("x")
    (tmp_path / "notes").write_bytes(b"p")
    assert blob_on_disk(tmp_path / "notes.md") is None


def test_blob_on_disk_ignores_longer_stem_sibling(tmp_path):
    (tmp_path / "notes.md").write_text("x")
    (tmp_path / "notes.extra.pdf").write_bytes(b"p")
    assert blob_on_disk(tmp_path / "notes.md") is None


def test_blob_on_disk_missing_parent_returns_none(tmp_path):
    assert blob_on_disk(tmp_path / "nope" / "notes.md") is None


def test_blob_on_disk_ignore_filters_candidate(tmp_path):
    (tmp_path / "notes.md").write_text("x")
    (tmp_path / "notes.pdf").write_bytes(b"p")
    (tmp_path / "notes.png").write_bytes(b"q")
    found = blob_on_disk(
        tmp_path / "notes.md", ignore=frozenset({tmp_path / "notes.png"})
    )
    assert found == tmp_path / "notes.pdf"


def test_rebuild_discovers_blob(db, seed):
    card = seed(title="Scan", summary="a scan", relpath="receipts/scan.md")
    (db.root / "receipts" / "scan.pdf").write_bytes(b"%PDF")
    db._git("add", "--", "receipts/scan.pdf")
    db._git("commit", "-q", "-m", "manual blob")
    db.conn.close()
    cache_path(db.root).unlink()
    db2 = mddb.MDDB(db.root)
    entry = next(e for e in db2.list() if e["id"] == card.id)
    assert entry["blob_relpath"] == "receipts/scan.pdf"


def test_list_blob_relpath_none_without_blob(db, seed):
    card = seed(title="Plain", summary="no blob")
    entry = next(e for e in db.list() if e["id"] == card.id)
    assert entry["blob_relpath"] is None


def _seed_card_with_manual_blob(db, seed):
    card = seed(title="Scan", summary="a scan", relpath="receipts/scan.md")
    (db.root / "receipts" / "scan.pdf").write_bytes(b"%PDF")
    db._git("add", "--", "receipts/scan.pdf")
    db._git("commit", "-q", "-m", "manual blob")
    db.conn.close()
    cache_path(db.root).unlink()
    db2 = mddb.MDDB(db.root)
    return db2, card


def test_move_carries_manual_blob(db, seed):
    db2, card = _seed_card_with_manual_blob(db, seed)
    before = next(x for x in db2.list() if x["id"] == card.id)
    assert before["blob_relpath"] == "receipts/scan.pdf"
    with db2.editor(rationale="archive") as ed:
        ed.move(card.id, "archive/scan.md")
    after = next(x for x in db2.list() if x["id"] == card.id)
    assert after["blob_relpath"] == "archive/scan.pdf"
    assert (db2.root / "archive/scan.pdf").read_bytes() == b"%PDF"
    assert not (db2.root / "receipts/scan.pdf").exists()


def test_content_update_preserves_blob_relpath(db, seed):
    db2, card = _seed_card_with_manual_blob(db, seed)
    fresh = db2.read(card.id)
    with db2.editor(rationale="edit") as ed:
        ed.update(fresh, summary="updated")
    after = next(x for x in db2.list() if x["id"] == card.id)
    assert after["blob_relpath"] == "receipts/scan.pdf"


def test_read_raises_on_two_blobs(db, seed):
    card = seed(title="Scan", summary="s", relpath="receipts/scan.md")
    (db.root / "receipts" / "scan.pdf").write_bytes(b"a")
    (db.root / "receipts" / "scan.png").write_bytes(b"b")
    db._git("add", "--", "receipts/scan.pdf", "receipts/scan.png")
    db._git("commit", "-q", "-m", "two blobs")
    with pytest.raises(ValueError, match="multiple blobs"):
        db.read(card.id)


def test_concurrent_stale_cache_opens_do_not_race(tmp_path):
    """Concurrent refreshers serialize on the same deck lock."""
    from concurrent.futures import ThreadPoolExecutor

    import mddb as mddb_mod

    db = mddb_mod.MDDB.init(tmp_path / "deck")
    with db.editor(rationale="seed a card for the refresh race") as e:
        card = e.create(title="Racer", summary="before", relpath="racer.md")
    (db.root / "racer.md").write_text(_card_text(card.id, summary="after"))
    _external_commit(db)
    db.conn.close()

    def opener(_):
        handle = mddb_mod.MDDB(tmp_path / "deck")
        return handle.conn.execute(
            "SELECT summary FROM entries WHERE id = ?", (card.id,)
        ).fetchone()[0]

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(opener, range(8))) == ["after"] * 8


def test_rebuild_leaves_planner_statistics(tmp_path):
    import mddb
    from mddb import _index

    db = mddb.MDDB.init(tmp_path / "deck")
    with db.editor(rationale="seed cards for planner statistics") as e:
        e.create(title="One", summary="s", yaml={"kind": "x", "rank": 1})
        e.create(title="Two", summary="s", yaml={"kind": "y", "rank": 2})
    _index.cache_path(db.root).unlink()
    db = mddb.MDDB(tmp_path / "deck")
    assert db.conn.execute("SELECT count(*) FROM sqlite_stat1").fetchone()[0] > 0
    plan = db.conn.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT e.id FROM entry_fields a "
        "JOIN entry_fields b ON b.entry_rowid = a.entry_rowid AND b.key='rank' "
        "JOIN entries e ON e.rowid = a.entry_rowid "
        "WHERE a.key='kind' AND a.value_str='x'"
    ).fetchall()
    assert any("entry_fields_entry_key" in row[3] for row in plan)


def test_incremental_deck_gets_planner_statistics_without_rebuild(tmp_path):
    import mddb

    db = mddb.MDDB.init(tmp_path / "deck")
    with db.editor(rationale="first card on a never-rebuilt deck") as e:
        e.create(title="Fresh", summary="s", yaml={"kind": "x"})
    assert db.conn.execute("SELECT count(*) FROM sqlite_stat1").fetchone()[0] > 0


def test_kind_is_written_indexed_and_disclosed(db, seed):
    card = seed(title="Standup", summary="Daily standup.", kind="event")
    assert db.read(card.id).kind == "event"
    assert [e["kind"] for e in db.list()] == ["event"]
    assert db.conn.execute(
        "SELECT kind FROM entries WHERE id = ?", (card.id,)
    ).fetchone() == ("event",)
    assert (
        db.conn.execute(
            "SELECT count(*) FROM entry_fields WHERE key = 'kind'"
        ).fetchone()[0]
        == 0
    )


def test_kind_absent_is_none_not_an_error(db, seed):
    card = seed(title="Anomaly detection", summary="")
    assert db.read(card.id).kind is None
    assert "kind" not in db.read(card.id).yaml
    assert db.list()[0]["kind"] is None


def test_kind_kwarg_wins_over_yaml_and_orders_after_summary(db, seed):
    card = seed(
        title="Merge the rename?",
        summary="12 files.",
        kind="judgement",
        yaml={"kind": "task", "producer": "fleet:$12"},
    )
    assert list(db.read(card.id).yaml) == [
        "id",
        "title",
        "summary",
        "kind",
        "producer",
    ]
    assert db.read(card.id).kind == "judgement"


def test_kind_survives_update(db, seed):
    card = seed(title="Standup", summary="Daily standup.", kind="event")
    with db.editor(rationale="retitle") as editor:
        fresh = editor.read(card.id)
        fresh.yaml["title"] = "Standup (moved)"
        editor.update(fresh, summary=fresh.summary)
    reopened = mddb.MDDB(db.root)
    assert reopened.read(card.id).kind == "event"
    assert reopened.list()[0]["kind"] == "event"


def test_kind_change_is_reindexed(db, seed):
    card = seed(title="Thing", summary="", kind="task")
    with db.editor(rationale="reclassify") as editor:
        fresh = editor.read(card.id)
        fresh.yaml["kind"] = "project"
        editor.update(fresh, summary=fresh.summary)
    reopened = mddb.MDDB(db.root)
    assert reopened.conn.execute(
        "SELECT kind FROM entries WHERE id = ?", (card.id,)
    ).fetchone() == ("project",)


def test_at_reads_the_bytes_pinned_at_a_commit(db, seed):
    card = seed(title="Merge?", summary="12 files", body="original\n")
    pinned = db.head()
    with db.editor(rationale="tamper after display") as editor:
        fresh = editor.read(card.id)
        fresh.body = "rewritten\n"
        editor.update(fresh, summary=fresh.summary)
    reopened = mddb.MDDB(db.root)
    assert reopened.read(card.id).body == "rewritten\n"
    assert reopened.at(card.id, pinned).body == "original\n"


def test_at_survives_a_move(db, seed):
    card = seed(title="Movable", summary="")
    pinned = db.head()
    with db.editor(rationale="file it elsewhere") as editor:
        editor.move(card.id, "elsewhere/movable.md")
    reopened = mddb.MDDB(db.root)
    assert reopened.at(card.id, pinned).id == card.id


def test_at_finds_a_card_deleted_since(db, seed):
    card = seed(title="Doomed", summary="", body="still here\n")
    pinned = db.head()
    with db.editor(rationale="delete") as editor:
        editor.delete(card.id)
    reopened = mddb.MDDB(db.root)
    assert reopened.at(card.id, pinned).body == "still here\n"


def test_at_raises_for_a_card_absent_at_that_commit(db, seed):
    seed(title="First", summary="")
    early = db.head()
    later = seed(title="Second", summary="")
    with pytest.raises(KeyError):
        mddb.MDDB(db.root).at(later.id, early)


def test_at_blob_is_unset(db, seed):
    card = seed(title="Pinned", summary="")
    assert mddb.MDDB(db.root).at(card.id, db.head()).blob is None


def test_blob_at_reads_the_blob_pinned_at_a_commit(db, seed):
    card = seed(title="Scan", summary="", blob=b"original bytes", blob_ext=".bin")
    pinned = db.head()
    with db.editor(rationale="replace the blob") as editor:
        editor.delete(card.id)
    reopened = mddb.MDDB(db.root)
    assert reopened.blob_at(card.id, pinned) == b"original bytes"


def test_blob_at_is_none_without_one(db, seed):
    card = seed(title="No blob", summary="")
    assert mddb.MDDB(db.root).blob_at(card.id, db.head()) is None
