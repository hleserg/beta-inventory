"""MCP for agents: the same inventory as the site. Mounted by app.py at /mcp (streamable HTTP)."""
import asyncio
import io
import os
import urllib.request
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError  # plain exceptions reach the agent without their text
from mcp.types import ToolAnnotations
from PIL import Image

from . import core
from .core import PROFILE, db

BASE = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")  # empty: links come out as site paths
T = PROFILE["terms"]
FETCH_LIMIT = 20 * 1024 * 1024
ENRICH = Path(__file__).parent.parent / "skills" / "inventory-enrich-card" / "SKILL.md"
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
LOGGED = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)  # history keeps every change

server = MCPServer("inventory", instructions=(
    f"Home inventory «{PROFILE['name']}». {T['items']} (items) lie in {T['boxes']} (boxes); a box has a 5-char id "
    f"printed on its label, may sit in another box and stands in a {T['place']} (place). Quantity belongs to the "
    "box×item pair. Start with search; card fields are defined by the profile, see card_template. "
    "Search before create_item: the item may exist. agent_queue: cards a person handed to an agent to fill in. Each card_template field's hint says what goes in it. Stock changes go through change_stock under your name. "
    "Data is in the profile's language: answer the user in it."))


def link(path):
    return BASE + path


def with_links(fields, type_key):
    """Photo and file fields hold upload names; give agents URLs instead."""
    f = dict(fields)
    for fd in core.fields_for(type_key):
        v = f.get(fd["key"])
        if v and fd["type"] == "photo":
            f[fd["key"]] = [link(f"/u/{x}") for x in v]
        elif v and fd["type"] == "files":
            f[fd["key"]] = [{"name": x["name"], "url": link(f"/u/{x['file']}")} for x in v]
    return f


@server.tool(annotations=READ)
def search(query: str) -> dict[str, Any]:
    """Find items and boxes by words (name, other names, description, searchable card fields) and by meaning.

    Items come with where they lie: box id, place path, qty (null: «есть, не считал») in the item's unit (шт, м, г…).
    Word matches rank first; `similar: true`
    marks items found only by meaning. Take an item id to get_item for the full card.
    """
    items, boxes = core.search(query)
    return {"items": [dict(id=i["id"], name=i["name"], type=core.type_label(i["type"]), similar=i["similar"],
                           unit=core.unit_of(i["type"], i["fields"]), stock=i["stock"], url=link(f"/i/{i['id']}")) for i in items],
            "boxes": [dict(b, url=link(f"/b/{b['id']}")) for b in boxes]}


@server.tool(annotations=READ)
def get_item(item_id: int) -> dict[str, Any]:
    """Full card of an item: fields (keys as in card_template), unit its qty counts, stock per box, last 10 movements.
    Box HANDS in stock: in hand, not put away; its where says the box it was taken from, if any.
    Box TRANS («в пути»): ordered, not come yet; its where says when it was put there («заказано ДД.ММ»)."""
    return card(item_id)


def card(item_id):
    with db() as c:
        it = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not it:
            raise ToolError(f"No item {item_id}. Find item ids with search.")
        moves = c.execute(core.MOVES + " AND m.item_id=? ORDER BY m.id DESC LIMIT 10", (item_id,))
        return dict(id=it["id"], name=it["name"], type=it["type"], type_label=core.type_label(it["type"]),
                    fields=with_links(core.item_fields(it), it["type"]),
                    unit=core.unit_of(it["type"], core.item_fields(it)), stock=core.stock_of_item(c, item_id),
                    history=[dict(m) for m in moves], url=link(f"/i/{item_id}"), for_agent=bool(it["for_agent"]))


@server.tool(annotations=READ)
def agent_queue() -> dict[str, Any]:
    """Cards a person ticked «Передать агенту»: fill each in (identify, datasheet, right type, every field)
    as `skill` says, then update_item takes it off this list."""
    with db() as c:
        items = [dict(r, url=link(f"/i/{r['id']}")) for r in c.execute(
            "SELECT id, name, type FROM items WHERE for_agent ORDER BY updated_at")]
    return dict(items=items, skill=ENRICH.read_text(encoding="utf-8"))


@server.tool(annotations=READ)
def get_box(box_id: str) -> dict[str, Any]:
    """What lies in a box: items with qty, boxes inside it, where it stands. box_id is the label id, any case.

    qty null: some are there, nobody counted them (loose small parts).
    """
    with db() as c:
        b = c.execute("SELECT * FROM boxes WHERE id=?", (box_id.strip().upper(),)).fetchone()
        if not b:
            raise ToolError(f"No box {box_id.upper()}. Find boxes with search (by id, name or place).")
        return dict(id=b["id"], name=b["name"], where=core.box_where(c, b["id"]), url=link(f"/b/{b['id']}"),
                    contents=[dict(item_id=r["id"], name=r["name"], type=core.type_label(r["type"]), qty=r["qty"],
                                   unit=core.unit_of(r["type"], r["fields"])) for r in core.box_contents(c, b["id"])],
                    boxes=[dict(id=x["id"], name=x["name"]) for x in
                           c.execute("SELECT id, name FROM boxes WHERE parent_id=? ORDER BY id", (b["id"],))])


@server.tool(annotations=READ)
def card_template(type: str = "") -> dict[str, Any]:
    """Card fields of an item type (key, label, type, required, hint), to fill a card right.

    Without type: the categories and their types to pick from.
    """
    if not type:
        return {"categories": [dict(key=c["key"], label=c["label"],
                                    types=[dict(key=t["key"], label=t["label"], hint=t.get("hint", "")) for t in c.get("types", [])])
                               for c in PROFILE["categories"]]}
    if type not in core.TYPES:
        raise ToolError(f"Unknown type {type!r}. Call card_template without type for the list.")
    return {"type": type, "label": core.type_label(type), "fields": core.fields_for(type)}


def author(agent):
    if not agent.strip():
        raise ToolError("agent is empty: pass your name, it is shown as the author in history.")
    return agent.strip()


@server.tool(annotations=READ)
def list_projects() -> dict[str, Any]:
    """Active projects, for project_id when stock is taken for a project.

    inbox: new repos from the user's GitHub, not projects yet. Each goes to accept_project or skip_project.
    """
    with db() as c:
        inbox = c.execute("SELECT id, name, description, git_url FROM projects WHERE status='inbox' ORDER BY name")
        return {"projects": [dict(p) for p in core.projects(c)], "inbox": [dict(p) for p in inbox]}


@server.tool(annotations=LOGGED)
def accept_project(git_url: str, name: str, description: str) -> dict[str, Any]:
    """Make a repo an active project: one from list_projects' inbox, or any other (a private repo the sync can't see).

    name and description are what the user reads: short, in the profile's language, what it is for in plain words,
    not the repo's slug and English blurb.
    """
    if not git_url.strip() or not name.strip():
        raise ToolError("git_url and name are needed.")
    return {"project_id": core.accept_project(git_url, name, description)}


@server.tool(annotations=LOGGED)
def skip_project(git_url: str) -> dict[str, Any]:
    """An inbox repo that is not a project to take stock for: it leaves the inbox for good. accept_project undoes it."""
    try:
        core.skip_project(git_url)
    except ValueError as e:
        raise ToolError(f"{e}. See list_projects' inbox.") from None
    return {"skipped": git_url}


@server.tool(annotations=LOGGED)
def change_stock(box_id: str, item_id: int, action: Literal["put", "return", "buy", "take", "count"], qty: int | None,
                 agent: str, project_id: int | None = None) -> dict[str, Any]:
    """Change how many of an item lie in a box. Every change goes to history with agent as the author.

    put / return / buy: qty more in the box; take: qty out, project_id says what for (list_projects);
    count: the box holds exactly qty now (stocktaking). qty counts in the item's unit (get_item).
    agent: your name as the user knows you.
    put / return / buy with qty null: some went in, not counted (loose resistors and the like); the box then
    holds qty null, «есть, не считал», left out of totals. take and count need a number.
    box_id "" or HANDS is «на руках»: a take from a box lands there, a put or return into a box takes from there
    first, a take from HANDS writes it off (used up, gone), put/buy on HANDS: brought home, not put away yet.
    box_id TRANS is «в пути»: ordered — put there; it came — take from TRANS, then put into the real box.
    Returns the new qty in the box.
    """
    if project_id is not None:
        with db() as c:
            if not c.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise ToolError(f"No project {project_id}. Call list_projects.")
    try:
        qty = core.change_stock(box_id, item_id, action, qty, author(agent), project_id)
    except ValueError as e:
        raise ToolError(f"{e}. Check the box with get_box, the item with get_item.") from None
    return {"box_id": box_id.strip().upper() or core.HANDS, "item_id": item_id, "qty": qty}


def fetch(url):
    """Download what an agent found online. ponytail: no private-address guard, the site is LAN-only anyway."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "beta-inventory"}),
                                    timeout=30) as r:
            data = r.read(FETCH_LIMIT + 1)
    except (OSError, ValueError) as e:
        raise ToolError(f"Can't download {url}: {e}") from None
    if len(data) > FETCH_LIMIT:
        raise ToolError(f"{url} is over {FETCH_LIMIT >> 20} MB.")
    return data


async def upload(url, photo):
    if not str(url).lower().startswith(("http://", "https://")):
        raise ToolError(f"{url!r} is not a link: photos and files are given as http(s) URLs.")
    data = await asyncio.to_thread(fetch, url)
    if photo:
        try:
            Image.open(io.BytesIO(data)).verify()
        except Exception:
            raise ToolError(f"{url} is not a picture: give a direct link to the image file.") from None
    return await asyncio.to_thread(core.save_bytes, data, Path(urlparse(url).path).suffix.lower()[:10], photo)


async def fill(type_key, old, given):
    """Agent's field values → stored card fields, merged over old. Unknown keys fail, hidden stored ones stay."""
    defs = {fd["key"]: fd for fd in core.fields_for(type_key)}
    f = dict(old)
    for k, v in given.items():
        fd = defs.get(k)
        if not fd:
            if k in old:
                continue  # a field of the card's former type, kept but hidden: get_item returns it too
            raise ToolError(f"No field {k!r} for type {type_key!r}. See card_template({type_key!r}).")
        if fd["type"] == "photo":
            pics = [v] if isinstance(v, str) else v or []  # one photo as a plain string is fine too
            f[k] = [core.own_upload(x) or await upload(x, photo=True) for x in pics if x]
        elif fd["type"] == "files":
            have = [x["file"] for x in f.get(k, [])]
            for x in v or []:
                src = x.get("url") or x.get("file", "") if isinstance(x, dict) else x
                if core.own_upload(src) in have:
                    continue  # already on the card
                name = x.get("name") if isinstance(x, dict) else ""
                f.setdefault(k, []).append({"name": name or Path(urlparse(src).path).name or "file",
                                            "file": core.own_upload(src) or await upload(src, photo=False)})
        else:
            f[k] = v if isinstance(v, (int, float)) else str(v or "").strip()
    return f


def check(name, type_key, f):
    errors = core.clean_fields(name, core.fields_for(type_key), f)
    if errors:
        raise ToolError("; ".join(errors) + f". See card_template({type_key!r}).")


@server.tool(annotations=LOGGED)
async def create_item(type: str, name: str, fields: dict[str, Any], agent: str, box_id: str = "",
                      qty: int | None = 0) -> dict[str, Any]:
    """New card. Fields by card_template(type), keys as there; photo: a list of direct image URLs (one string is fine), the server downloads them;
    files: [{name, url}]. With box_id and qty, puts qty into that box (history author: agent, your name);
    qty null puts some in uncounted, «есть, не считал». With qty and no box_id, the qty is in hand (box HANDS),
    not put away yet.

    Search first: the item may already exist. Returns the card as get_item does.
    """
    if type not in core.TYPES:
        raise ToolError(f"Unknown type {type!r}. Call card_template without type for the list.")
    box_id = box_id.strip().upper()
    if box_id:
        with db() as c:
            if not c.execute("SELECT 1 FROM boxes WHERE id=?", (box_id,)).fetchone():
                raise ToolError(f"No box {box_id}. Find boxes with search.")
    agent, f = author(agent), await fill(type, {}, fields)
    check(name, type, f)
    iid = core.save_item(None, name.strip(), type, f)
    if box_id and (qty is None or qty > 0):
        core.move(iid, box_id, qty, "put", agent)
    elif not box_id and qty:  # no box yet: in hand until put away
        core.move(iid, core.HANDS, qty, "buy", agent)
    return card(iid)


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False))
async def update_item(item_id: int, fields: dict[str, Any], name: str = "", type: str = "") -> dict[str, Any]:
    """Change card fields: only the keys given change, "" clears one. photo: the whole list, get_item's links keep a photo,
    new image URLs add one; files: new [{name, url}] are added, ones already on the card are kept.
    type: move the card to another type (card_template), its required fields must then be given. Returns the card.
    """
    with db() as c:
        it = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not it:
        raise ToolError(f"No item {item_id}. Find item ids with search.")
    if type and type not in core.TYPES:
        raise ToolError(f"Unknown type {type!r}. Call card_template without type for the list.")
    name, type = name.strip() or it["name"], type or it["type"]
    f = await fill(type, core.item_fields(it), fields)
    check(name, type, f)
    core.save_item(item_id, name, type, f)
    core.set_for_agent(item_id, False)  # an agent filled it in: off agent_queue
    return card(item_id)
