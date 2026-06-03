import pytest

import mddb
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
    with db.transaction(rationale="moved shed contents to barn") as tx:
        tx.update(card, summary="Tools and equipment, moved to the barn.")
    assert db.read(card.id).yaml["location"] == "barn"


def test_delete(db, seed):
    card = seed(title="Disposable", summary="A card created so we can verify delete.")
    with db.transaction(rationale="verifying removal makes read raise") as tx:
        tx.delete(card.id)
    with pytest.raises(KeyError):
        db.read(card.id)


def test_move_keeps_id(db, seed):
    card = seed(
        title="Flat Card",
        summary="A card initially at the root.",
        body="contents",
    )
    with db.transaction(rationale="reorganised into subfolder") as tx:
        tx.move(card.id, "moved/here.md")
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
    with db.transaction(rationale="bumped x") as tx:
        tx.update(card, summary=card.summary)
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
    from mddb.index import cache_path

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
        {"id": a.id, "title": "Fridge", "summary": "What's in the fridge."},
        {"id": b.id, "title": "Shed", "summary": "Tools and equipment."},
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


def test_mddb_init_sets_active_tx_none(tmp_path):
    new_db = mddb.MDDB(tmp_path)
    assert new_db._active_tx is None
