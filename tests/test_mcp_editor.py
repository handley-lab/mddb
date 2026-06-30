import json
import os
import tempfile

import pytest

pytest.importorskip("mcp")

from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402

import mddb  # noqa: E402
from mddb._merge import install  # noqa: E402
from mddb._mcp import mcp  # noqa: E402


@pytest.fixture(autouse=True)
def _install_driver(request):
    if "db" in request.fixturenames:
        install(request.getfixturevalue("db").root)


def _result(content):
    assert len(content) == 1
    return json.loads(content[0].text)


async def _edit(deck, rationale, ops):
    return _result(
        await mcp.call_tool(
            "editor", {"deck": deck, "rationale": rationale, "ops": json.dumps(ops)}
        )
    )


async def test_editor_stale_base_conflicts(db, seed):
    seed(title="Seed", summary="s")
    stale = db.head()
    seed(title="Other", summary="moves HEAD")
    with pytest.raises(ToolError):
        await mcp.call_tool(
            "editor",
            {
                "deck": str(db.root),
                "rationale": "x",
                "ops": json.dumps([{"op": "create", "title": "A", "summary": "a"}]),
                "base": stale,
            },
        )


async def test_editor_threaded_base_commits(db, seed):
    seed(title="Seed", summary="s")
    content = await mcp.call_tool("read", {"deck": str(db.root), "op": "list"})
    base = json.loads(content[0].text)["base"]
    out = _result(
        await mcp.call_tool(
            "editor",
            {
                "deck": str(db.root),
                "rationale": "fresh base",
                "ops": json.dumps([{"op": "create", "title": "A", "summary": "a"}]),
                "base": base,
            },
        )
    )
    assert out["results"][0]["op"] == "create"


def _commit_count(db):
    return db._git("rev-list", "--count", "HEAD").stdout.strip()


async def test_create_returns_id_and_persists(db):
    out = await _edit(
        str(db.root),
        "add card",
        [{"op": "create", "title": "Shed", "summary": "store", "body": "x\n"}],
    )
    cid = out["results"][0]["id"]
    assert db.read(cid).body == "x\n"


async def test_batch_is_one_commit(db):
    ops = [
        {"op": "create", "title": "Shed", "summary": "store", "body": "foo\n"},
        {"op": "update", "id": "$prev[0]", "summary": "store it"},
        {"op": "edit", "id": "$prev[0]", "old": "foo", "new": "bar"},
        {"op": "move", "id": "$prev[0]", "new_relpath": "inventory/shed.md"},
    ]
    out = await _edit(str(db.root), "build shed", ops)
    cid = out["results"][0]["id"]
    assert _commit_count(db) == "2"
    assert len(db.history(cid)) == 1
    assert db.read(cid).body == "bar\n"
    assert (db.root / "inventory" / "shed.md").exists()


async def test_body_phase_rollback(db):
    with pytest.raises(ToolError):
        await _edit(
            str(db.root),
            "half batch",
            [
                {"op": "create", "title": "Shed", "summary": "store"},
                {"op": "frobnicate"},
            ],
        )
    assert _commit_count(db) == "1"
    assert db.list() == []
    assert list(db.root.rglob("*.md")) == []


async def test_update_requires_summary(db):
    with pytest.raises(ToolError):
        await _edit(
            str(db.root),
            "no summary",
            [
                {"op": "create", "title": "Shed", "summary": "store"},
                {"op": "update", "id": "$prev[0]"},
            ],
        )


async def test_update_yaml_shallow_merge(db):
    out = await _edit(
        str(db.root),
        "merge yaml",
        [
            {
                "op": "create",
                "title": "Shed",
                "summary": "store",
                "yaml": {"area": "garden"},
            },
            {
                "op": "update",
                "id": "$prev[0]",
                "summary": "store",
                "yaml": {"status": "open"},
            },
        ],
    )
    card = db.read(out["results"][0]["id"])
    assert card.yaml["area"] == "garden"
    assert card.yaml["status"] == "open"


async def test_update_body_replace(db):
    out = await _edit(
        str(db.root),
        "replace body",
        [
            {"op": "create", "title": "Shed", "summary": "store", "body": "old\n"},
            {"op": "update", "id": "$prev[0]", "summary": "store", "body": "new\n"},
        ],
    )
    assert db.read(out["results"][0]["id"]).body == "new\n"


async def test_edit_then_update_preserves_edit(db, seed):
    card = seed(title="Shed", summary="store", body="foo\n")
    await _edit(
        str(db.root),
        "edit then update",
        [
            {"op": "edit", "id": card.id, "old": "foo", "new": "bar"},
            {"op": "update", "id": card.id, "summary": "store"},
        ],
    )
    assert db.read(card.id).body == "bar\n"


async def test_prev_out_of_range_rolls_back(db):
    with pytest.raises(ToolError):
        await _edit(
            str(db.root),
            "bad prev",
            [
                {"op": "create", "title": "Shed", "summary": "store"},
                {"op": "move", "id": "$prev[5]", "new_relpath": "x.md"},
            ],
        )
    assert _commit_count(db) == "1"


async def test_non_deck_path_errors_without_writing(tmp_path):
    not_a_deck = tmp_path / "notgit"
    not_a_deck.mkdir()
    with pytest.raises(ToolError) as exc:
        await _edit(
            str(not_a_deck),
            "x",
            [{"op": "create", "title": "A", "summary": "a"}],
        )
    assert "deck" in str(exc.value).lower()
    assert list(not_a_deck.iterdir()) == []


async def test_editor_requires_driver_installed(tmp_path, monkeypatch):
    db = mddb.MDDB.init(tmp_path / "deck")
    (db.root / ".gitattributes").write_text("*.md merge=mddb-card\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    with pytest.raises(ToolError) as exc:
        await _edit(str(db.root), "x", [{"op": "create", "title": "A", "summary": "a"}])
    message = str(exc.value).lower()
    assert "driver" in message or "install" in message


async def test_init_creates_deck(tmp_path):
    new_deck = str(tmp_path / "fresh")
    out = await _edit(new_deck, "ignored for init", [{"op": "init"}])
    assert out["results"][0]["op"] == "init"
    assert (tmp_path / "fresh" / ".git").is_dir()
    install(new_deck)
    created = await _edit(
        new_deck, "first card", [{"op": "create", "title": "A", "summary": "a"}]
    )
    got = _result(
        await mcp.call_tool(
            "read", {"deck": new_deck, "op": "get", "id": created["results"][0]["id"]}
        )
    )["result"]
    assert got["yaml"]["title"] == "A"


async def test_create_with_blob_path(db):
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"PNGBYTES")
        src = f.name
    try:
        out = await _edit(
            str(db.root),
            "add blob card",
            [{"op": "create", "title": "Plan", "summary": "floor", "blob_path": src}],
        )
    finally:
        os.unlink(src)
    card = db.read(out["results"][0]["id"])
    assert card.blob.read_bytes() == b"PNGBYTES"
    assert card.blob.name == "plan.png"


async def test_create_blob_ext_override(db):
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        f.write(b"DATA")
        src = f.name
    try:
        out = await _edit(
            str(db.root),
            "add blob card",
            [
                {
                    "op": "create",
                    "title": "Doc",
                    "summary": "s",
                    "blob_path": src,
                    "blob_ext": ".pdf",
                }
            ],
        )
    finally:
        os.unlink(src)
    got = _result(
        await mcp.call_tool(
            "read", {"deck": str(db.root), "op": "get", "id": out["results"][0]["id"]}
        )
    )["result"]
    assert got["blob_relpath"] == "doc.pdf"


async def test_prev_collapsed_create_rolls_back(db):
    with pytest.raises(ToolError):
        await _edit(
            str(db.root),
            "collapse",
            [
                {"op": "create", "title": "Shed", "summary": "store"},
                {"op": "delete", "id": "$prev[0]"},
                {"op": "move", "id": "$prev[0]", "new_relpath": "x.md"},
            ],
        )
    assert _commit_count(db) == "1"
    assert db.list() == []
