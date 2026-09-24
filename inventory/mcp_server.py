"""MCP for agents: the same inventory as the site. Mounted by app.py at /mcp (streamable HTTP)."""
import os
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError  # plain exceptions reach the agent without their text
from mcp.types import ToolAnnotations

from . import core
from .core import PROFILE, db

BASE = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")  # empty: links come out as site paths
T = PROFILE["terms"]
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

server = MCPServer("inventory", instructions=(
    f"Home inventory «{PROFILE['name']}». {T['items']} (items) lie in {T['boxes']} (boxes); a box has a 5-char id "
    f"printed on its label, may sit in another box and stands in a {T['place']} (place). Quantity belongs to the "
    "box×item pair. Start with search; card fields are defined by the profile, see card_template. "
    "Data is in the profile's language: answer the user in it."))


def link(path):
    return BASE + path


def with_links(fields, type_key):
    """Photo and file fields hold upload names; give agents URLs instead."""
    f = dict(fields)
    for fd in core.fields_for(type_key):
        v = f.get(fd["key"])
        if v and fd["type"] == "photo":
            f[fd["key"]] = link(f"/u/{v}")
        elif v and fd["type"] == "files":
            f[fd["key"]] = [{"name": x["name"], "url": link(f"/u/{x['file']}")} for x in v]
    return f


@server.tool(annotations=READ)
def search(query: str) -> dict[str, Any]:
    """Find items and boxes by words (name, other names, description, searchable card fields) and by meaning.

    Items come with where they lie: box id, place path, qty. Word matches rank first; `similar: true`
    marks items found only by meaning. Take an item id to get_item for the full card.
    """
    items, boxes = core.search(query)
    return {"items": [dict(id=i["id"], name=i["name"], type=core.type_label(i["type"]), similar=i["similar"],
                           stock=i["stock"], url=link(f"/i/{i['id']}")) for i in items],
            "boxes": [dict(b, url=link(f"/b/{b['id']}")) for b in boxes]}


@server.tool(annotations=READ)
def get_item(item_id: int) -> dict[str, Any]:
    """Full card of an item: fields (keys as in card_template), stock per box, last 10 movements."""
    with db() as c:
        it = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not it:
            raise ToolError(f"No item {item_id}. Find item ids with search.")
        moves = c.execute(core.MOVES + " WHERE m.item_id=? ORDER BY m.id DESC LIMIT 10", (item_id,))
        return dict(id=it["id"], name=it["name"], type=it["type"], type_label=core.type_label(it["type"]),
                    fields=with_links(core.item_fields(it), it["type"]), stock=core.stock_of_item(c, item_id),
                    history=[dict(m) for m in moves], url=link(f"/i/{item_id}"))


@server.tool(annotations=READ)
def get_box(box_id: str) -> dict[str, Any]:
    """What lies in a box: items with qty, boxes inside it, where it stands. box_id is the label id, any case."""
    with db() as c:
        b = c.execute("SELECT * FROM boxes WHERE id=?", (box_id.strip().upper(),)).fetchone()
        if not b:
            raise ToolError(f"No box {box_id.upper()}. Find boxes with search (by id, name or place).")
        return dict(id=b["id"], name=b["name"], where=core.box_where(c, b["id"]), url=link(f"/b/{b['id']}"),
                    contents=[dict(item_id=r["id"], name=r["name"], type=core.type_label(r["type"]), qty=r["qty"])
                              for r in core.box_contents(c, b["id"])],
                    boxes=[dict(id=x["id"], name=x["name"]) for x in
                           c.execute("SELECT id, name FROM boxes WHERE parent_id=? ORDER BY id", (b["id"],))])


@server.tool(annotations=READ)
def card_template(type: str = "") -> dict[str, Any]:
    """Card fields of an item type (key, label, type, required, hint), to fill a card right.

    Without type: the categories and their types to pick from.
    """
    if not type:
        return {"categories": [dict(key=c["key"], label=c["label"],
                                    types=[dict(key=t["key"], label=t["label"]) for t in c.get("types", [])])
                               for c in PROFILE["categories"]]}
    if type not in core.TYPES:
        raise ToolError(f"Unknown type {type!r}. Call card_template without type for the list.")
    return {"type": type, "label": core.type_label(type), "fields": core.fields_for(type)}
