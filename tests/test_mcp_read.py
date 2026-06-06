import json

import pytest

pytest.importorskip("mcp")

from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402

import mddb  # noqa: E402
from mddb._mcp import mcp  # noqa: E402


def _objs(content):
    return [json.loads(block.text) for block in content]


def _one(content):
    assert len(content) == 1
    return json.loads(content[0].text)


async def test_read_list(db, seed):
    card = seed(title="Wheelbarrow", summary="garden tool", body="in the shed\n")
    rows = _objs(await mcp.call_tool("read", {"deck": str(db.root), "op": "list"}))
    assert [r["id"] for r in rows] == [card.id]
    assert rows[0]["title"] == "Wheelbarrow"
    assert rows[0]["blob_relpath"] is None


async def test_read_get(db, seed):
    card = seed(title="Wheelbarrow", summary="garden tool", body="in the shed\n")
    got = _one(
        await mcp.call_tool("read", {"deck": str(db.root), "op": "get", "id": card.id})
    )
    assert got["id"] == card.id
    assert got["yaml"]["title"] == "Wheelbarrow"
    assert got["body"] == "in the shed\n"
    assert got["blob_relpath"] is None


async def test_read_history_message_is_rationale(db, seed):
    card = seed(title="Shed", summary="store", rationale="built the shed today")
    commits = _objs(
        await mcp.call_tool(
            "read", {"deck": str(db.root), "op": "history", "id": card.id}
        )
    )
    assert commits[0]["message"].strip() == "built the shed today"


async def test_read_unknown_op_raises(db):
    with pytest.raises(ToolError):
        await mcp.call_tool("read", {"deck": str(db.root), "op": "frobnicate"})


async def test_multi_deck_isolation(tmp_path):
    finance = mddb.MDDB.init(tmp_path / "finance")
    home = mddb.MDDB.init(tmp_path / "home")
    with finance.editor(rationale="seed finance") as e:
        f_card = e.create(title="Tax", summary="2025 return")
    with home.editor(rationale="seed home") as e:
        h_card = e.create(title="Boiler", summary="service due")

    f_rows = _objs(
        await mcp.call_tool("read", {"deck": str(finance.root), "op": "list"})
    )
    h_rows = _objs(await mcp.call_tool("read", {"deck": str(home.root), "op": "list"}))
    assert [r["id"] for r in f_rows] == [f_card.id]
    assert [r["id"] for r in h_rows] == [h_card.id]


async def test_query_matches_list(db, seed):
    card = seed(title="Wheelbarrow", summary="garden tool")
    result = _one(
        await mcp.call_tool(
            "read",
            {
                "deck": str(db.root),
                "op": "query",
                "sql": "SELECT id, title FROM entries",
            },
        )
    )
    assert result["columns"] == ["id", "title"]
    assert result["rows"] == [[card.id, "Wheelbarrow"]]


async def test_query_fts_with_params(db, seed):
    card = seed(title="Shed", summary="store", body="the wheelbarrow lives here\n")
    seed(title="Boiler", summary="heating", body="hot water\n")
    result = _one(
        await mcp.call_tool(
            "read",
            {
                "deck": str(db.root),
                "op": "query",
                "sql": "SELECT id FROM entries WHERE rowid IN "
                "(SELECT rowid FROM entries_fts WHERE entries_fts MATCH ?)",
                "params": '["wheelbarrow"]',
            },
        )
    )
    assert result["rows"] == [[card.id]]


async def test_query_default_params(db, seed):
    seed(title="A", summary="a")
    result = _one(
        await mcp.call_tool(
            "read",
            {
                "deck": str(db.root),
                "op": "query",
                "sql": "SELECT count(*) FROM entries",
            },
        )
    )
    assert result["rows"] == [[1]]


async def test_query_cannot_write_cache(db, seed):
    seed(title="A", summary="a")
    for sql in (
        "INSERT INTO meta(key, value) VALUES ('x', 'y')",
        "UPDATE entries SET title = 'z'",
        "CREATE TABLE evil(x)",
    ):
        with pytest.raises(ToolError):
            await mcp.call_tool(
                "read", {"deck": str(db.root), "op": "query", "sql": sql}
            )


async def test_query_sees_committed_write(db, seed):
    seed(title="First", summary="one")
    with db.editor(rationale="add second") as e:
        e.create(title="Second", summary="two")
    result = _one(
        await mcp.call_tool(
            "read",
            {
                "deck": str(db.root),
                "op": "query",
                "sql": "SELECT count(*) FROM entries",
            },
        )
    )
    assert result["rows"] == [[2]]


async def test_blob_returns_path_and_bytes(db):
    with db.editor(rationale="seed blob") as e:
        card = e.create(
            title="Floorplan", summary="kitchen", blob=b"PNGDATA", blob_ext=".png"
        )
    result = _one(
        await mcp.call_tool("read", {"deck": str(db.root), "op": "blob", "id": card.id})
    )
    from pathlib import Path

    assert Path(result["path"]).read_bytes() == b"PNGDATA"
    got = _one(
        await mcp.call_tool("read", {"deck": str(db.root), "op": "get", "id": card.id})
    )
    assert got["blob_relpath"] == "floorplan.png"


async def test_blob_missing_raises(db, seed):
    card = seed(title="Plain", summary="no blob")
    with pytest.raises(ToolError):
        await mcp.call_tool("read", {"deck": str(db.root), "op": "blob", "id": card.id})
