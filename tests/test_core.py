import pytest

import mddb
from mddb._index import blob_on_disk, cache_path
from mddb.card import Card


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
            "blob_relpath": None,
        },
        {
            "id": b.id,
            "title": "Shed",
            "summary": "Tools and equipment.",
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
