"""Web pages. Plain HTML forms, no JS framework: works on any phone in the LAN."""
import io
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from types import SimpleNamespace

import qrcode
from fastapi import FastAPI, Form, Request
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
    pk=next((f["key"] for f in PROFILE["item_fields"] if f["type"] == "photo"), None))
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
    return T.TemplateResponse(req, tpl, ctx, status_code=status)


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
def boxes(req: Request, new: str = ""):
    with db() as c:
        rows = [dict(b, where=core.box_where(c, b["id"])) for b in c.execute(
            "SELECT b.*, COUNT(s.item_id) AS n FROM boxes b LEFT JOIN stock s ON s.box_id=b.id "
            "GROUP BY b.id ORDER BY b.created_at DESC, b.id")]
    return page(req, "boxes.html", boxes=rows, new=[x for x in new.split(",") if x])


@app.post("/boxes")
def boxes_create(n: int = Form(1)):
    return go("/boxes?new=" + ",".join(core.new_boxes(max(1, min(n, 100)))))


@app.get("/b/{box_id}")
@app.get("/B/{box_id}")  # QR codes carry upper-case URLs
def box(req: Request, box_id: str):
    with db() as c:
        b = get_box(c, box_id)
        contents = core.box_contents(c, b["id"])
        children = c.execute("SELECT * FROM boxes WHERE parent_id=? ORDER BY id", (b["id"],)).fetchall()
        places = c.execute("SELECT * FROM places ORDER BY name").fetchall()
        return page(req, "box.html", b=b, where=core.box_where(c, b["id"]), contents=contents,
                    children=children, places=places, projects=core.projects(c),
                    nfc=f"{public_base(req)}/b/{b['id']}".lower())  # NFC Tools writes it as typed


@app.post("/b/{box_id}")
def box_save(box_id: str, name: str = Form(""), place: str = Form(""), parent_id: str = Form("")):
    parent = parent_id.strip().upper() or None
    with db() as c:
        b = get_box(c, box_id)
        p = parent
        while p:  # parent must exist and must not sit inside this box
            if p == b["id"]:
                raise ValueError("Коробка не может лежать сама в себе")
            p = get_box(c, p)["parent_id"]
        place_id = None
        if place.strip() and not parent:  # a new name makes the place, so a first box needs no trip to places
            c.execute("INSERT OR IGNORE INTO places(name) VALUES (?)", (place.strip(),))
            place_id = c.execute("SELECT id FROM places WHERE name=?", (place.strip(),)).fetchone()["id"]
        c.execute("UPDATE boxes SET name=?, place_id=?, parent_id=? WHERE id=?", (name.strip(), place_id, parent, b["id"]))
    return go(f"/b/{b['id']}")


@app.post("/b/{box_id}/clear")
def box_clear(box_id: str):
    with db() as c:
        b = get_box(c, box_id)
    core.clear_box(b["id"], AUTHOR)
    return go(f"/b/{b['id']}")


@app.get("/b/{box_id}/label.png")
def label(req: Request, box_id: str):
    with db() as c:
        b = get_box(c, box_id)
    return Response(label_png(b["id"], public_base(req)), media_type="image/png")


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
            up = form.get(k)
            if getattr(up, "filename", ""):
                f[k] = save_upload(up, photo=True)
            elif form.get(k + "__keep"):
                f[k] = str(form.get(k + "__keep"))
        elif typ == "files":
            for up in form.getlist(k):
                if getattr(up, "filename", ""):
                    f.setdefault(k, []).append({"name": up.filename, "file": save_upload(up)})
        else:
            f[k] = str(form.get(k, "")).strip()
    name = str(form.get("name", "")).strip()
    return name, f, core.clean_fields(name, fields, f), form


@app.get("/items")
def items(req: Request, type: str = "", cat: str = ""):
    if type in core.TYPES:
        cat = core.TYPES[type]["category"]["key"]
    with db() as c:
        rows = [dict(r, fields=json.loads(r["fields"])) for r in c.execute(
            "SELECT i.*, COALESCE(SUM(s.qty), 0) AS total, MAX(s.box_id IS NOT NULL AND s.qty IS NULL) AS uncounted FROM items i LEFT JOIN stock s ON s.item_id=i.id "
            "WHERE ?='' OR i.type=? GROUP BY i.id ORDER BY i.name", (type, type))]
    if cat:
        rows = [r for r in rows if r["type"] in core.TYPES and core.TYPES[r["type"]]["category"]["key"] == cat]
    return page(req, "items.html", items=rows, type=type, cat=cat)


def card_form(req, status=200, it=None, type="", name="", vals=None, errors=(), box="", qty=""):
    with db() as c:
        boxes = c.execute("SELECT id, name FROM boxes ORDER BY id").fetchall() if not it else []
    return page(req, "item_form.html", status, it=it, type=type, fields=core.fields_for(type), name=name,
                vals=vals or {}, errors=errors, box=box, qty=qty, boxes=boxes)


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
    box_id, qty = str(form.get("box", "")).strip().upper(), str(form.get("qty", "")).strip()
    if box_id:
        with db() as c:
            if not c.execute("SELECT 1 FROM boxes WHERE id=?", (box_id,)).fetchone():
                errors.append(f"Нет коробки {box_id}")
    if qty and not qty.isdigit():
        errors.append("Количество — целое число; пусто — «не считал»")
    if errors:
        return card_form(req, 400, type=type, name=name, vals=f, errors=errors, box=box_id, qty=qty)
    iid = core.save_item(None, name, type, f)
    if box_id and (not qty or int(qty) > 0):  # empty: «есть, не считал»
        core.move(iid, box_id, int(qty) if qty else None, "put", AUTHOR)
    return go(f"/b/{box_id}" if box_id else f"/i/{iid}")  # filling a box: back to it for the next item


@app.get("/i/{item_id}")
def item(req: Request, item_id: int):
    with db() as c:
        it = get_item(c, item_id)
        return page(req, "item.html", it=it, f=json.loads(it["fields"]), fields=core.fields_for(it["type"]),
                    stock=core.stock_of_item(c, item_id), projects=core.projects(c),
                    history=c.execute(MOVES + " WHERE m.item_id=? ORDER BY m.id DESC LIMIT 50", (item_id,)).fetchall())


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


# --- stock ---

@app.post("/stock")
def stock(box: str = Form(), item: int = Form(), action: str = Form(), qty: int | None = Form(None),
          kind: str = Form("put"), project: str = Form(""), back: str = Form("/")):
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
        rows = [dict(p, boxes=c.execute("SELECT * FROM boxes WHERE place_id=? AND parent_id IS NULL ORDER BY id",
                                        (p["id"],)).fetchall())
                for p in c.execute("SELECT * FROM places ORDER BY name")]
    return page(req, "places.html", places=rows)


@app.post("/places")
def place_add(name: str = Form(), note: str = Form("")):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO places(name, note) VALUES (?, ?)", (name.strip(), note.strip()))
    return go("/places")


@app.post("/places/{place_id}")
def place_save(place_id: int, name: str = Form(), note: str = Form("")):
    with db() as c:
        c.execute("UPDATE places SET name=?, note=? WHERE id=?", (name.strip(), note.strip(), place_id))
    return go("/places")


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
