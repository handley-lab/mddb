"""FastMCP server exposing mddb decks to cross-process agents (Codex, Clawde).

Optional extra: ``pip install mddb[mcp]`` then run ``mcp-mddb``. One stateless
server serves many decks — every tool takes ``deck`` (an absolute path to an
mddb root) as its first argument and opens that deck per call, so the same
server addresses finance/home/work decks side by side. The core ``import mddb``
never imports this module; it is reached only via the ``mcp-mddb`` console
script.
"""

import json
import re
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from pydantic import Field

import mddb

mcp = FastMCP("mddb")

_READ_DESCRIPTION = f"""Read from an mddb deck. Dispatches on `op`:

- `list`: every card's {{id, title, summary, blob_relpath}} (cheap overview).
- `get` (needs `id`): one card's {{id, yaml, body, blob_relpath}}.
- `history` (needs `id`): commit history newest-first, [{{sha, author, timestamp, message}}].
- `query` (needs `sql`; optional `params`, a JSON-array string of positional `?`
  bindings, default "[]"): runs raw read-only SQL against the deck's SQLite
  cache and returns {{columns, rows}}. There is NO row cap — write your own
  LIMIT or large results will flood the agent context. Schema:

{mddb._index.SCHEMA_DOC}

- `blob` (needs `id`): {{path}} — the absolute on-disk path of the card's binary
  blob (bytes are not inlined); errors if the card has no blob.

`deck` is the absolute path to the mddb root. `deck` and blob paths are
server-local: any path the server process can read/write."""

_EDITOR_DESCRIPTION = """Apply a batch of edits to an mddb deck. `ops` is a
JSON-array string of operations; the whole batch runs in one editor block and
lands as ONE git commit (message = `rationale`) on success. An error while
building the batch rolls it back with no disk change; a failure during the
commit itself propagates and may leave the working tree/cache dirty. There is
no batch-size cap. Operations (dispatched on `op`):

- {"op":"create","title","summary","body"?,"relpath"?,"tags"?,"yaml"?,"blob_path"?,"blob_ext"?}
- {"op":"update","id","summary","tags"?,"body"?,"yaml"?}   (yaml is shallow-merged)
- {"op":"delete","id"}
- {"op":"move","id","new_relpath"}   (new_relpath must end in .md)
- {"op":"edit","id","old","new","replace_all"?}   (body find/replace)

Any `id` field may be "$prev[N]" to reference the id returned by the Nth earlier
op in this batch (0-indexed) — e.g. create a card then move it. Returns
{"results": [{op, id, ...}]} with the (possibly auto-generated) id per op."""

_PREV = re.compile(r"^\$prev\[(\d+)\]$")


def _resolve_prev(value, results):
    match = _PREV.match(value)
    if not match:
        return value
    return results[int(match.group(1))]["id"]


@mcp.tool(description=_READ_DESCRIPTION)
def read(
    deck: str = Field(..., description="Absolute path to the mddb deck root."),
    op: str = Field(..., description="One of: list, get, history, query, blob."),
    id: str = Field("", description="Card id; required for get, history, blob."),
    sql: str = Field("", description="SQL SELECT; required for query."),
    params: str = Field(
        "[]", description="JSON array of positional SQL params for query."
    ),
):
    db = mddb.MDDB(deck)
    if op == "list":
        return db.list()
    if op == "get":
        card = db.read(id)
        blob_relpath = (
            str(card.blob.relative_to(db.root)) if card.blob is not None else None
        )
        return {
            "id": card.id,
            "yaml": card.yaml,
            "body": card.body,
            "blob_relpath": blob_relpath,
        }
    if op == "history":
        return db.history(id)
    if op == "query":
        ro = mddb._index.open_index_readonly(db.root)
        cur = ro.execute(sql, json.loads(params))
        columns = [c[0] for c in cur.description] if cur.description else []
        return {"columns": columns, "rows": [list(row) for row in cur.fetchall()]}
    if op == "blob":
        card = db.read(id)
        if card.blob is None:
            raise ValueError(f"card has no blob: {id}")
        return {"path": str(card.blob)}
    raise ValueError(f"unknown op: {op}")


@mcp.tool(description=_EDITOR_DESCRIPTION)
def editor(
    deck: str = Field(..., description="Absolute path to the mddb deck root."),
    rationale: str = Field(..., description="Commit message for the whole batch."),
    ops: str = Field(..., description="JSON array of operation objects."),
):
    db = mddb.MDDB(deck)
    results = []
    with db.editor(rationale=rationale) as e:
        for op in json.loads(ops):
            kind = op["op"]
            if kind == "create":
                kwargs = {
                    k: op[k]
                    for k in ("body", "relpath", "tags", "yaml", "blob_ext")
                    if k in op
                }
                if "blob_path" in op:
                    kwargs["blob"] = Path(op["blob_path"])
                card = e.create(title=op["title"], summary=op["summary"], **kwargs)
                results.append({"op": "create", "id": card.id})
            elif kind == "update":
                cid = _resolve_prev(op["id"], results)
                card = e.read(cid)
                if "body" in op:
                    card.body = op["body"]
                if "yaml" in op:
                    card.yaml.update(op["yaml"])
                if "tags" in op:
                    e.update(card, summary=op["summary"], tags=op["tags"])
                else:
                    e.update(card, summary=op["summary"])
                results.append({"op": "update", "id": cid})
            elif kind == "delete":
                cid = _resolve_prev(op["id"], results)
                e.delete(cid)
                results.append({"op": "delete", "id": cid})
            elif kind == "move":
                cid = _resolve_prev(op["id"], results)
                e.move(cid, op["new_relpath"])
                results.append({"op": "move", "id": cid})
            elif kind == "edit":
                cid = _resolve_prev(op["id"], results)
                if "replace_all" in op:
                    count = e.edit(
                        cid, op["old"], op["new"], replace_all=op["replace_all"]
                    )
                else:
                    count = e.edit(cid, op["old"], op["new"])
                results.append({"op": "edit", "id": cid, "count": count})
            else:
                raise ValueError(f"unknown op: {kind}")
    return {"results": results}


if __name__ == "__main__":
    mcp.run()
