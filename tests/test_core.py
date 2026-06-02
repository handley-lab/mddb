import pytest

import mddb
from mddb.card import Card


def test_create_read(db):
    card = db.create(
        title="Wheelbarrow",
        summary="A garden tool kept in the shed.",
        yaml={"tags": ["shed"], "location": "shed"},
        body="wheelbarrow\n",
        rationale="bought today",
    )
    again = db.read(card.id)
    assert again.yaml["location"] == "shed"
    assert again.body == "wheelbarrow\n"


def test_update(db):
    card = db.create(
        title="Shed Inventory",
        summary="Tools and equipment in the shed.",
        yaml={"location": "shed"},
        body="a",
        rationale="testing update — initial shed location",
    )
    card.yaml["location"] = "barn"
    db.update(
        card,
        summary="Tools and equipment, moved to the barn.",
        rationale="testing update — moved shed contents to barn",
    )
    assert db.read(card.id).yaml["location"] == "barn"


def test_delete(db):
    card = db.create(
        title="Disposable",
        summary="A card created so we can verify delete.",
        yaml={"x": 1},
        rationale="testing delete — created so we can remove",
    )
    db.delete(card.id, rationale="testing delete — verifying removal makes read raise")
    with pytest.raises(KeyError):
        db.read(card.id)


def test_move_keeps_id(db):
    card = db.create(
        title="Flat Card",
        summary="A card initially at the root.",
        yaml={"x": 1},
        body="contents",
        rationale="testing move — initial flat layout",
    )
    db.move(
        card.id, "moved/here.md", rationale="testing move — reorganised into subfolder"
    )
    again = db.read(card.id)
    assert again.id == card.id
    assert again.body == "contents"
    assert (db.root / "moved" / "here.md").exists()


def test_fts_via_conn(db):
    card = db.create(
        title="Tool Audit",
        summary="An audit of garden tools.",
        yaml={"tags": ["shed"]},
        body="wheelbarrow and spade",
        rationale="testing fts — body should match wheelbarrow",
    )
    rows = db.conn.execute(
        "SELECT id FROM entries WHERE rowid IN (SELECT rowid FROM entries_fts WHERE entries_fts MATCH ?)",
        ("wheelbarrow",),
    ).fetchall()
    assert [r[0] for r in rows] == [card.id]


def test_field_filter_via_conn(db):
    a = db.create(
        title="Shed",
        summary="Shed-tagged card.",
        yaml={"tags": ["shed"]},
        rationale="testing field filter — shed card should match",
    )
    db.create(
        title="Fridge",
        summary="Fridge-tagged card.",
        yaml={"tags": ["fridge"]},
        rationale="testing field filter — fridge card should not match",
    )
    rows = db.conn.execute(
        "SELECT entries.id FROM entries JOIN entry_fields ON entry_fields.entry_rowid = entries.rowid "
        "WHERE entry_fields.key = ? AND entry_fields.value_str = ?",
        ("tags", "shed"),
    ).fetchall()
    assert [r[0] for r in rows] == [a.id]


def test_history(db):
    card = db.create(
        title="Counter",
        summary="A card whose body counter we bump.",
        yaml={"x": 1},
        body="hello",
        rationale="initial commit message",
    )
    card.yaml["x"] = 2
    db.update(
        card,
        summary="A card whose body counter we bump.",
        rationale="bumped x",
    )
    commits = db.history(card.id)
    assert [c["message"].strip() for c in commits] == [
        "bumped x",
        "initial commit message",
    ]


def test_relpath_explicit(db):
    db.create(
        title="Shed",
        summary="Shed-located audit.",
        yaml={"location": "shed"},
        relpath="inventory/shed.md",
        rationale="testing explicit relpath — caller-chosen inventory path",
    )
    assert (db.root / "inventory" / "shed.md").exists()


def test_relpath_default_slug(db):
    db.create(
        title="Fridge Inventory",
        summary="Items in the fridge.",
        rationale="testing slug-from-title default",
    )
    assert (db.root / "fridge-inventory.md").exists()


def test_relpath_directory_with_autofill(db):
    db.create(
        title="Fridge",
        summary="Items in the fridge.",
        relpath="inventory/",
        rationale="testing trailing-slash relpath autofills slug",
    )
    assert (db.root / "inventory" / "fridge.md").exists()


def test_relpath_appends_md(db):
    db.create(
        title="Fridge",
        summary="Items in the fridge.",
        relpath="inventory/fridge",
        rationale="testing missing .md is appended",
    )
    assert (db.root / "inventory" / "fridge.md").exists()


def test_cache_rebuild(db):
    card = db.create(
        title="Persistent",
        summary="A card whose cache we will delete.",
        yaml={"x": 1},
        body="hello",
        rationale="testing cache rebuild — initial card before cache deletion",
    )
    root = db.root
    from mddb.index import cache_path

    db.conn.close()
    cache_path(root).unlink()
    db2 = mddb.MDDB(root)
    assert db2.read(card.id).yaml["x"] == 1


def test_create_requires_title(db):
    with pytest.raises(TypeError):
        db.create(summary="x", rationale="missing title kwarg")


def test_create_requires_summary(db):
    with pytest.raises(TypeError):
        db.create(title="x", rationale="missing summary kwarg")


def test_update_requires_summary(db):
    card = db.create(
        title="x",
        summary="y",
        rationale="testing update kwargs",
    )
    with pytest.raises(TypeError):
        db.update(card, rationale="missing summary kwarg")


def test_list_progressive_disclosure(db):
    a = db.create(
        title="Fridge",
        summary="What's in the fridge.",
        yaml={"tags": ["fridge"]},
        body="milk, eggs",
        rationale="testing progressive disclosure — fridge card",
    )
    b = db.create(
        title="Shed",
        summary="Tools and equipment.",
        rationale="testing progressive disclosure — shed card",
    )
    entries = sorted(db.list(), key=lambda e: e["title"])
    assert entries == [
        {"id": a.id, "title": "Fridge", "summary": "What's in the fridge."},
        {"id": b.id, "title": "Shed", "summary": "Tools and equipment."},
    ]


def test_card_title_summary_properties(db):
    card = db.create(
        title="Fridge",
        summary="What's in the fridge.",
        body="milk",
        rationale="testing card properties — title and summary are direct dict access",
    )
    again = db.read(card.id)
    assert again.title == "Fridge"
    assert again.summary == "What's in the fridge."


def test_card_properties_raise_on_missing_keys():
    """Bypass MDDB.create so we can verify Card's strict access on a raw dict."""
    card = Card(yaml={}, body="")
    with pytest.raises(KeyError):
        _ = card.id
    with pytest.raises(KeyError):
        _ = card.title
    with pytest.raises(KeyError):
        _ = card.summary
