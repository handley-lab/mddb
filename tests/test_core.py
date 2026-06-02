import pytest

import mddb


def test_create_read(db):
    card = db.create(
        {"tags": ["shed"], "location": "shed"},
        body="wheelbarrow\n",
        rationale="bought today",
    )
    again = db.read(card.id)
    assert again.yaml["location"] == "shed"
    assert again.body == "wheelbarrow\n"


def test_update(db):
    card = db.create(
        {"location": "shed"},
        body="a",
        rationale="testing update — initial shed location",
    )
    card.yaml["location"] = "barn"
    db.update(card, rationale="testing update — moved shed contents to barn")
    assert db.read(card.id).yaml["location"] == "barn"


def test_delete(db):
    card = db.create({"x": 1}, rationale="testing delete — created so we can remove")
    db.delete(card.id, rationale="testing delete — verifying removal makes read raise")
    with pytest.raises(KeyError):
        db.read(card.id)


def test_move_keeps_id(db):
    card = db.create(
        {"x": 1}, body="contents", rationale="testing move — initial flat layout"
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
        {"tags": ["shed"]},
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
        {"tags": ["shed"]}, rationale="testing field filter — shed card should match"
    )
    db.create(
        {"tags": ["fridge"]},
        rationale="testing field filter — fridge card should not match",
    )
    rows = db.conn.execute(
        "SELECT entries.id FROM entries JOIN entry_fields ON entry_fields.entry_rowid = entries.rowid "
        "WHERE entry_fields.key = ? AND entry_fields.value_str = ?",
        ("tags", "shed"),
    ).fetchall()
    assert [r[0] for r in rows] == [a.id]


def test_history(db):
    card = db.create({"x": 1}, body="hello", rationale="initial commit message")
    card.yaml["x"] = 2
    db.update(card, rationale="bumped x")
    commits = db.history(card.id)
    assert [c["message"].strip() for c in commits] == [
        "bumped x",
        "initial commit message",
    ]


def test_relpath(db):
    db.create(
        {"location": "shed"},
        relpath="inventory/shed.md",
        rationale="testing explicit relpath — caller-chosen inventory path",
    )
    assert (db.root / "inventory" / "shed.md").exists()


def test_cache_rebuild(db):
    card = db.create(
        {"x": 1},
        body="hello",
        rationale="testing cache rebuild — initial card before cache deletion",
    )
    root = db.root
    from mddb.index import cache_path

    db.conn.close()
    cache_path(root).unlink()
    db2 = mddb.MDDB(root)
    assert db2.read(card.id).yaml["x"] == 1
