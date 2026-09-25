"""Web pages. Plain HTML forms, no JS framework: works on any phone in the LAN."""
import io
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from types import SimpleNamespace

import qrcode
from fastapi import FastAPI, Form, Header, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup
from PIL import Image, ImageDraw, ImageFont
from starlette.exceptions import HTTPException

from . import core
from .core import MOVES, PROFILE, db
from .mcp_server import server as mcp_server

core.init()
app = FastAPI(title="beta-inventory", lifespan=lambda _: mcp_server.session_manager.run())
# Agents: MCP at /mcp, same port and data as the site. Host 0.0.0.0 turns the localhost-only Host check
# off: agents come by LAN name, and the site has no auth by design (LAN only).
app.router.routes.extend(mcp_server.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0").routes)
app.mount("/u", StaticFiles(directory=core.UPLOADS), name="uploads")
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")
T = Jinja2Templates(directory=Path(__file__).parent / "templates")
T.env.globals.update(
    # namespace, not dict: in Jinja t.items on a dict is dict.items, not the term
    t=SimpleNamespace(**PROFILE["terms"]), categories=PROFILE["categories"], type_label=core.type_label, kinds=core.KINDS,
    trash_days=core.TRASH_DAYS,
    pk=next((f["key"] for f in PROFILE["item_fields"] if f["type"] == "photo"), None))


def all_boxes():  # for box fields: named first, by name; each with its place — the top box's (№39: filter, grey hint)
    with db() as c:
        rows = {r["id"]: dict(r) for r in c.execute(
            "SELECT b.id, b.name, b.parent_id, b.place_id, p.name AS place FROM boxes b LEFT JOIN places p ON p.id=b.place_id WHERE b.kind!='hands'")}
    for r in rows.values():
        t, seen = r, {r["id"]}
        while t["parent_id"] in rows and t["parent_id"] not in seen:
            seen.add(t["parent_id"])
            t = rows[t["parent_id"]]
        r["top"], r["where"] = t["place_id"], t["place"]
    return sorted(rows.values(), key=lambda r: (not r["name"], r["name"] or "", r["id"]))


T.env.globals["all_boxes"] = all_boxes
_md = MarkdownIt("commonmark", {"html": False}).enable("table")  # raw HTML off: cards come from agents too
T.env.filters["md"] = lambda s: Markup(_md.render(s or ""))


def qty_text(q, uncounted=False):
    """Stock as people read it. None is «есть, не считал»; uncounted: the sum left some piles out."""
    if q is None or uncounted and not q:
        return "есть, не считал"
    return f"{q} {PROFILE['terms']['unit']}" + (" + не считал" if uncounted else "")


T.env.filters["qty"] = qty_text
AUTHOR = "человек"  # ponytail: no accounts; MCP calls will pass the agent's name


def page(req, tpl, status=200, **ctx):
    return T.TemplateResponse(req, tpl, ctx, status_code=status, headers={"Cache-Control": "no-store"})  # back button refetches: counts change


def go(url):
    return RedirectResponse(url, 303)


@app.exception_handler(ValueError)
def bad_input(req, e):
    return page(req, "error.html", 400, msg=str(e))


@app.exception_handler(HTTPException)
def http_error(req, e):
    return page(req, "error.html", e.status_code, msg=e.detail)


def get_box(c, box_id):
    b = c.execute("SELECT * FROM boxes WHERE id=?", (box_id.strip().upper(),)).fetchone()
    if not b:
        raise HTTPException(404, f"Нет коробки {box_id.upper()}")
    return b


def get_item(c, item_id):
    it = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not it:
        raise HTTPException(404, "Нет такой позиции")
    return it


# --- search / home ---

@app.get("/")
def index(req: Request, q: str = "", put: str = ""):
    items, boxes = core.search(q)
    with db() as c:
        recent = [] if q else c.execute(MOVES + " ORDER BY m.id DESC LIMIT 15").fetchall()
    return page(req, "index.html", q=q, put=put.upper(), items=items, boxes=boxes, recent=recent)


# --- boxes ---

@app.get("/boxes")
def boxes(req: Request, new: str = "", loose: str = ""):
    with db() as c:
        rows = [dict(b, where=core.box_where(c, b["id"])) for b in c.execute(
            "SELECT b.*, COUNT(s.item_id) AS n FROM boxes b LEFT JOIN stock s ON s.box_id=b.id "
            "WHERE b.kind='box' GROUP BY b.id ORDER BY b.created_at DESC, b.id")]
    if loose:  # №28: boxes standing nowhere — no place, theirs or their outer box's
        rows = [r for r in rows if not r["where"]]
    return page(req, "boxes.html", boxes=rows, new=[x for x in new.split(",") if x], loose=loose)


@app.post("/boxes")
def boxes_create(n: int = Form(1)):
    return go("/boxes?new=" + ",".join(core.new_boxes(max(1, min(n, 100)))))


@app.get("/boxes/new")
def box_new(req: Request):
    """«+ Коробка»: the page of a box not made yet. Leave untouched and there is no box; box_save makes it."""
    return box_page(req, dict(id=core.free_box_id(), name="", kind="box", place_id=None, parent_id=None), draft=True)


def valid_box_id(s):
    """A box the site knows or could have handed out (a «+ Коробка» page not saved yet)."""
    if not core.is_box_id(s := s.strip().upper()):
        raise HTTPException(404, f"Нет коробки {s}")
    return s


@app.get("/b/{box_id}")
@app.get("/B/{box_id}")  # QR codes carry upper-case URLs
def box(req: Request, box_id: str):
    with db() as c:
        b = get_box(c, box_id)
    if b["kind"] == "hands":  # no label, place or delete for it: the list of what is in hand
        return go("/items?hands=1")
    return box_page(req, b)


def box_page(req, b, draft=False):
    with db() as c:
        contents = core.box_contents(c, b["id"])
        children = c.execute("SELECT * FROM boxes WHERE parent_id=? ORDER BY id", (b["id"],)).fetchall()
        places = c.execute("SELECT * FROM places ORDER BY name").fetchall()
        return page(req, "box.html", b=b, where=core.box_where(c, b["id"]), contents=contents, top=box_top(b["id"]),
                    children=children, places=places, projects=core.projects(c), draft=draft,
                    nfc=f"{public_base(req)}/b/{b['id']}".lower())  # NFC Tools writes it as typed


def box_top(box_id):  # the place a box stands in: its own, or its top box's
    return next((x["top"] for x in all_boxes() if x["id"] == box_id), None)


@app.post("/b/{box_id}")
def box_save(box_id: str, name: str = Form(""), place: str = Form(""), parent_id: str = Form(""),
             x_autosave: str = Header("")):
    bid = valid_box_id(box_id)
    parent = core.find_box(parent_id) if parent_id.strip() else None
    if parent == core.HANDS:  # «на руках» holds things, not boxes (№28)
        raise ValueError(f"«{PROFILE['terms'].get('hands', 'На руках')}» — не коробка")
    with db() as c:  # an error rolls the insert back: a draft stays a draft
        c.execute("INSERT OR IGNORE INTO boxes(id) VALUES (?)", (bid,))
        b = get_box(c, bid)
        p = parent
        while p:  # parent must exist and must not sit inside this box
            if p == b["id"]:
                raise ValueError("Коробка не может лежать сама в себе")
            p = get_box(c, p)["parent_id"]
        place_id = None
        if place.strip() and not parent:  # chosen from the list, never made from typed text (a half-typed «Мой» became a place)
            place_id = int(place) if place.strip().isdigit() else 0
            if not c.execute("SELECT 1 FROM places WHERE id=?", (place_id,)).fetchone():
                raise ValueError(f"Нет такого места — добавьте его в «{PROFILE['terms']['places']}»")
        if b["kind"] == "shelf":  # a shelf stays in its cabinet: only the name changes
            c.execute("UPDATE boxes SET name=? WHERE id=?", (name.strip() or b["name"], bid))
        else:
            c.execute("UPDATE boxes SET name=?, place_id=?, parent_id=? WHERE id=?", (name.strip(), place_id, parent, bid))
        where = core.box_where(c, bid)
    if x_autosave:  # box.html saves as you type and redraws the heading
        return {"id": bid, "name": name.strip(), "where": where, "parent": parent, "place": box_top(bid)}
    return go(f"/b/{bid}")


@app.post("/b/{box_id}/clear")
def box_clear(box_id: str):
    with db() as c:
        b = get_box(c, box_id)
    core.clear_box(b["id"], AUTHOR)
    return go(f"/b/{b['id']}")


@app.post("/b/{box_id}/delete")
def box_delete(box_id: str):
    with db() as c:
        b = get_box(c, box_id)
    core.trash("box", b["id"])
    return go(f"/places#p{b['place_id']}" if b["kind"] == "shelf" else "/boxes")


@app.get("/b/{box_id}/label.png")
def label(req: Request, box_id: str):  # a «+ Коробка» page shows it before the box is saved
    return Response(label_png(valid_box_id(box_id), public_base(req)), media_type="image/png")


def public_base(req):
    """Address in labels: PUBLIC_BASE_URL from .env, else whatever the browser used."""
    return (os.environ.get("PUBLIC_BASE_URL") or str(req.base_url)).rstrip("/")


def label_png(box_id, base):
    w_mm, h_mm, dpi = (float(os.environ.get(k, d)) for k, d in
                       (("LABEL_W_MM", 25), ("LABEL_H_MM", 15), ("LABEL_DPI", 300)))
    W, H = round(w_mm / 25.4 * dpi), round(h_mm / 25.4 * dpi)
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, border=1)
    qr.add_data(f"{base}/B/{box_id}".upper())  # upper case = alphanumeric mode = smaller QR
    qr.make(fit=True)
    qr.box_size = max(1, H // (qr.modules_count + 2))
    code = qr.make_image().get_image().convert("1")
    img = Image.new("1", (W, H), 1)
    img.paste(code, (0, (H - code.height) // 2))
    d, room = ImageDraw.Draw(img), W - code.width
    for size in range(H, 7, -2):
        font = ImageFont.load_default(size=size)
        l, t, r, bt = d.textbbox((0, 0), box_id, font=font)
        if r - l <= room * 0.9 and bt - t <= H * 0.6:
            break
    d.text((code.width + (room - (r - l)) // 2 - l, (H - (bt - t)) // 2 - t), box_id, font=font, fill=0)
    buf = io.BytesIO()
    img.save(buf, "PNG", dpi=(dpi, dpi))
    return buf.getvalue()


# --- items ---

def save_upload(up, photo=False):
    return core.save_bytes(up.file.read(), Path(up.filename).suffix.lower()[:10], photo)


async def read_fields(req, old, fields):
    """Card form → (name, fields, errors, form), driven entirely by the profile."""
    form = await req.form()
    f = dict(old)
    for fd in fields:
        k, typ = fd["key"], fd["type"]
        if typ == "photo":
            keep = [x for x in map(core.own_upload, form.getlist(k + "__keep")) if x]  # the ones not ✕-ed, in order
            f[k] = keep + [save_upload(up, photo=True) for up in form.getlist(k) if getattr(up, "filename", "")]
        elif typ == "files":
            for up in form.getlist(k):
                if getattr(up, "filename", ""):
                    f.setdefault(k, []).append({"name": up.filename, "file": save_upload(up)})
        else:
            f[k] = str(form.get(k, "")).strip()
    name = str(form.get("name", "")).strip()
    return name, f, core.clean_fields(name, fields, f), form


@app.get("/items")
def items(req: Request, type: str = "", cat: str = "", q: str = "", hands: str = ""):
    if type in core.TYPES:
        cat = core.TYPES[type]["category"]["key"]
    with db() as c:
        rows = [dict(r, fields=json.loads(r["fields"])) for r in c.execute(
            "SELECT i.*, COALESCE(SUM(s.qty), 0) AS total, MAX(s.box_id IS NOT NULL AND s.qty IS NULL) AS uncounted, MAX(s.box_id=?) AS hands FROM items i LEFT JOIN stock s ON s.item_id=i.id "
            "WHERE ?='' OR i.type=? GROUP BY i.id ORDER BY i.name", (core.HANDS, type, type))]
    if hands:  # №28: taken and not put back, or not put away yet
        rows = [r for r in rows if r["hands"]]
    if cat:
        rows = [r for r in rows if r["type"] in core.TYPES and core.TYPES[r["type"]]["category"]["key"] == cat]
    if q.strip():  # same search as the main page, kept in its rank order
        rank = {i["id"]: n for n, i in enumerate(core.search(q)[0])}
        rows = sorted((r for r in rows if r["id"] in rank), key=lambda r: rank[r["id"]])
    return page(req, "items.html", items=rows, type=type, cat=cat, q=q, hands=hands)


def card_form(req, status=200, it=None, type="", name="", vals=None, errors=(), box="", qty="", dups=()):
    return page(req, "item_form.html", status, it=it, type=type, fields=core.fields_for(type), name=name,
                vals=vals or {}, errors=errors, box=box, qty=qty, dups=dups)


def check_type(type):
    if type not in core.TYPES:
        raise ValueError(f"Нет такого типа: {type}")
    return type


@app.get("/items/new")
def item_new(req: Request, box: str = "", type: str = ""):
    if not type:
        return page(req, "type_pick.html", box=box.upper())
    return card_form(req, type=check_type(type), box=box.upper())


@app.post("/items/new")
async def item_create(req: Request, type: str):
    fields = core.fields_for(check_type(type))
    name, f, errors, form = await read_fields(req, {}, fields)
    box, qty, box_id = str(form.get("box", "")).strip(), str(form.get("qty", "")).strip(), None
    try:
        box_id = core.find_box(box) if box else None
    except ValueError as e:
        errors.append(str(e))
    if qty and not qty.isdigit():
        errors.append("Количество — целое число; пусто — «не считал»")
    if errors:
        return card_form(req, 400, type=type, name=name, vals=f, errors=errors, box=box, qty=qty)
    if not form.get("dup_ok") and (dups := core.lookalikes(name)):  # asked, not refused: two alike things are real too
        return card_form(req, 409, type=type, name=name, vals=f, box=box, qty=qty, dups=dups)
    iid = core.save_item(None, name, type, f)
    if box_id and (not qty or int(qty) > 0):  # empty: «есть, не считал»
        core.move(iid, box_id, int(qty) if qty else None, "put", AUTHOR)
    elif not box_id and qty and int(qty) > 0:  # no box yet: brought home, in hand until put away (№28)
        core.move(iid, core.HANDS, int(qty), "buy", AUTHOR)
    return go(f"/b/{box_id}" if box_id else f"/i/{iid}")  # filling a box: back to it for the next item


@app.get("/i/{item_id}")
def item(req: Request, item_id: int):
    with db() as c:
        it = get_item(c, item_id)
        return page(req, "item.html", it=it, f=json.loads(it["fields"]), fields=core.fields_for(it["type"]),
                    stock=core.stock_of_item(c, item_id), projects=core.projects(c),
                    nfc=f"{public_base(req)}/i/{item_id}".lower(),  # №37: the tag opens this card
                    history=c.execute(MOVES + " AND m.item_id=? ORDER BY m.id DESC LIMIT 50", (item_id,)).fetchall())


@app.post("/i/{item_id}/delete")
def item_delete(item_id: int):
    core.trash("item", item_id)
    return go("/items")


@app.get("/i/{item_id}/edit")
def item_edit(req: Request, item_id: int, type: str = ""):
    """`?type=` switches the form to another type; values of fields it lacks stay stored, just hidden."""
    with db() as c:
        it = get_item(c, item_id)
    return card_form(req, it=it, type=check_type(type) if type else it["type"], name=it["name"],
                     vals=json.loads(it["fields"]))


@app.post("/i/{item_id}/edit")
async def item_update(req: Request, item_id: int, type: str):
    with db() as c:
        it = get_item(c, item_id)
    name, f, errors, _ = await read_fields(req, json.loads(it["fields"]), core.fields_for(check_type(type)))
    if errors:
        return card_form(req, 400, it=it, type=type, name=name, vals=f, errors=errors)
    core.save_item(item_id, name, type, f)
    return go(f"/i/{item_id}")


@app.post("/i/{item_id}/rotate")
def item_rotate(item_id: int, photo: str = Form(), deg: int = Form()):
    return {"photo": core.rotate_photo(item_id, photo, deg)}


# --- stock ---

@app.post("/stock")
def stock(box: str = Form(), item: int = Form(), action: str = Form(), qty: int | None = Form(None),
          kind: str = Form("put"), project: str = Form(""), back: str = Form("/"), src: str = Form("")):
    if kind == "move" and src:  # the item card: a scanned box takes all of it from the one box it lay in
        core.transfer(item, src, box, AUTHOR)
    else:
        if action != "take":
            action = "count" if action == "set" else kind if kind in ("put", "return", "buy") else "put"
        core.change_stock(box, item, action, qty, AUTHOR, int(project) if project and action == "take" else None)
    return go(back if back.startswith("/") and not back.startswith("//") else "/")


# --- phone: installable app, NFC tags ---

@app.get("/manifest.webmanifest")
def manifest():
    return JSONResponse({"name": PROFILE["name"], "short_name": PROFILE["name"], "start_url": "/", "id": "/", "scope": "/", "display": "standalone",
                         "background_color": "#f7f6f2", "theme_color": "#1f6feb",
                         "icons": [{"src": f"/static/icon-{n}.png", "sizes": f"{n}x{n}", "type": "image/png",
                                    "purpose": "any maskable"} for n in (192, 512)]},
                        media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():  # served from the root: a worker only controls pages under its own path
    return FileResponse(STATIC / "sw.js", media_type="text/javascript")


@app.get("/offline")
def offline(req: Request):
    return page(req, "offline.html")


@app.get("/phone")
def phone(req: Request):
    """Setup steps: Chrome writes NFC tags only on a 'secure' site, so plain-HTTP LAN needs one flag."""
    u = urlsplit(public_base(req))
    return page(req, "phone.html", origin=f"{u.scheme}://{u.netloc}".lower())


# --- places, projects, history ---

@app.get("/places")
def places(req: Request):
    with db() as c:
        inside = lambda bid: c.execute("SELECT * FROM boxes WHERE parent_id=? ORDER BY name='', name, id", (bid,)).fetchall()
        rows = [dict(p, boxes=c.execute("SELECT * FROM boxes WHERE place_id=? AND parent_id IS NULL AND kind='box' "
                                        "ORDER BY name='', name, id", (p["id"],)).fetchall(),
                     shelves=[dict(s, boxes=inside(s["id"])) for s in c.execute(
                         "SELECT * FROM boxes WHERE place_id=? AND kind='shelf' ORDER BY rowid", (p["id"],))],
                     gone=c.execute("SELECT count(*) FROM stock JOIN boxes b ON b.id=box_id "  # things on its shelves
                                    "WHERE b.place_id=? AND b.kind='shelf'", (p["id"],)).fetchone()[0])
                for p in c.execute("SELECT * FROM places ORDER BY name")]
    return page(req, "places.html", places=rows)


@app.post("/places")
def place_add(name: str = Form(), note: str = Form("")):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO places(name, note) VALUES (?, ?)", (name.strip(), note.strip()))
    return go("/places")


@app.post("/places/{place_id}/shelves")
def shelf_add(place_id: int, name: str = Form(""), label: str = Form("")):
    """Shelves come one at a time, from their cabinet; with a label, straight to its page to print and write it."""
    with db() as c:
        if not c.execute("SELECT 1 FROM places WHERE id=?", (place_id,)).fetchone():
            raise HTTPException(404, "Нет такого места")
        n = c.execute("SELECT count(*) FROM boxes WHERE place_id=? AND kind='shelf'", (place_id,)).fetchone()[0]
        bid = core.free_box_id()
        c.execute("INSERT INTO boxes(id, name, kind, place_id) VALUES (?, ?, 'shelf', ?)",
                  (bid, name.strip() or f"{PROFILE['terms']['shelf']} {n + 1}", place_id))
    return go(f"/b/{bid}" if label else f"/places#p{place_id}")


@app.post("/places/{place_id}")
def place_save(place_id: int, name: str = Form(), note: str = Form("")):
    with db() as c:
        c.execute("UPDATE places SET name=?, note=? WHERE id=?", (name.strip(), note.strip(), place_id))
    return go("/places")


@app.post("/places/{place_id}/delete")
def place_delete(place_id: int):
    core.trash("place", place_id)
    return go("/places")


@app.get("/trash")
def trash_page(req: Request):
    return page(req, "trash.html", rows=core.trash_list())


@app.post("/trash/{trash_id}")
def trash_restore(trash_id: int):
    return go(core.restore(trash_id))


@app.get("/projects")
def projects_page(req: Request):
    with db() as c:
        rows = c.execute("SELECT * FROM projects ORDER BY status, name").fetchall()
    return page(req, "projects.html", projects=rows)


@app.post("/projects")
def project_add(name: str = Form(), description: str = Form(""), git_url: str = Form("")):
    with db() as c:
        c.execute("INSERT INTO projects(name, description, git_url) VALUES (?, ?, ?)",
                  (name.strip(), description.strip(), git_url.strip()))
    return go("/projects")


@app.post("/projects/{project_id}")
def project_save(project_id: int, name: str = Form(), description: str = Form(""), git_url: str = Form(""),
                 status: str = Form("active")):
    with db() as c:
        c.execute("UPDATE projects SET name=?, description=?, git_url=?, status=? WHERE id=?",
                  (name.strip(), description.strip(), git_url.strip(), status, project_id))
    return go("/projects")


@app.get("/history")
def history(req: Request):
    with db() as c:
        rows = c.execute(MOVES + " ORDER BY m.id DESC LIMIT 300").fetchall()
    return page(req, "history.html", moves=rows)
