"""Web pages. Plain HTML forms, no JS framework: works on any phone in the LAN."""
import io
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import qrcode
from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup
from PIL import Image, ImageDraw, ImageFont, ImageOps
from starlette.exceptions import HTTPException

from . import core
from .core import PROFILE, db

core.init()
app = FastAPI(title="beta-inventory")
app.mount("/u", StaticFiles(directory=core.UPLOADS), name="uploads")
T = Jinja2Templates(directory=Path(__file__).parent / "templates")
T.env.globals.update(
    # namespace, not dict: in Jinja t.items on a dict is dict.items, not the term
    t=SimpleNamespace(**PROFILE["terms"]), categories=PROFILE["categories"], type_label=core.type_label, kinds=core.KINDS,
    pk=next((f["key"] for f in PROFILE["item_fields"] if f["type"] == "photo"), None))
_md = MarkdownIt("commonmark", {"html": False}).enable("table")  # raw HTML off: cards come from agents too
T.env.filters["md"] = lambda s: Markup(_md.render(s or ""))
AUTHOR = "человек"  # ponytail: no accounts; MCP calls will pass the agent's name
MOVES = ("SELECT m.*, i.name AS item, p.name AS project FROM movements m "
         "JOIN items i ON i.id=m.item_id LEFT JOIN projects p ON p.id=m.project_id")


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


def projects(c):
    return c.execute("SELECT id, name FROM projects WHERE status='active' ORDER BY name").fetchall()


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
        contents = [dict(r, fields=json.loads(r["fields"])) for r in c.execute(
            "SELECT s.qty, i.* FROM stock s JOIN items i ON i.id=s.item_id WHERE s.box_id=? ORDER BY i.name",
            (b["id"],))]
        children = c.execute("SELECT * FROM boxes WHERE parent_id=? ORDER BY id", (b["id"],)).fetchall()
        places = c.execute("SELECT * FROM places ORDER BY name").fetchall()
        return page(req, "box.html", b=b, where=core.box_where(c, b["id"]), contents=contents,
                    children=children, places=places, projects=projects(c))


@app.post("/b/{box_id}")
def box_save(box_id: str, name: str = Form(""), place_id: str = Form(""), parent_id: str = Form("")):
    parent = parent_id.strip().upper() or None
    with db() as c:
        b = get_box(c, box_id)
        p = parent
        while p:  # parent must exist and must not sit inside this box
            if p == b["id"]:
                raise ValueError("Коробка не может лежать сама в себе")
            p = get_box(c, p)["parent_id"]
        c.execute("UPDATE boxes SET name=?, place_id=?, parent_id=? WHERE id=?",
                  (name.strip(), None if parent or not place_id else int(place_id), parent, b["id"]))
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
    base = os.environ.get("PUBLIC_BASE_URL") or str(req.base_url)
    return Response(label_png(b["id"], base), media_type="image/png")


def label_png(box_id, base):
    w_mm, h_mm, dpi = (float(os.environ.get(k, d)) for k, d in
                       (("LABEL_W_MM", 25), ("LABEL_H_MM", 15), ("LABEL_DPI", 300)))
    W, H = round(w_mm / 25.4 * dpi), round(h_mm / 25.4 * dpi)
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, border=1)
    qr.add_data(f"{base.rstrip('/')}/B/{box_id}".upper())  # upper case = alphanumeric mode = smaller QR
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
    ext, data = Path(up.filename).suffix.lower()[:10], up.file.read()
    if photo:  # phones shoot 5-10 MB; shrink so pages stay fast
        try:
            im = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
            im.thumbnail((1600, 1600))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=85)
            data, ext = buf.getvalue(), ".jpg"
        except OSError:
            pass  # Pillow can't read it (HEIC…) — keep the original
    name = uuid.uuid4().hex + ext
    (core.UPLOADS / name).write_bytes(data)
    return name


async def read_fields(req, old, fields):
    """Card form → (name, fields, errors, form), driven entirely by the profile."""
    form = await req.form()
    f, errors = dict(old), []
    name = str(form.get("name", "")).strip()
    if not name:
        errors.append("Название: обязательно")
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
            v = str(form.get(k, "")).strip()
            if typ == "number" and v:
                try:
                    v = float(v.replace(",", "."))
                    v = int(v) if v.is_integer() else v
                except ValueError:
                    errors.append(f"{fd['label']}: нужно число")
            f[k] = v
        if fd.get("required") and f.get(k) in (None, "", []):
            errors.append(f"{fd['label']}: обязательно")
    return name, f, errors, form


@app.get("/items")
def items(req: Request, type: str = ""):
    with db() as c:
        rows = [dict(r, fields=json.loads(r["fields"])) for r in c.execute(
            "SELECT i.*, COALESCE(SUM(s.qty), 0) AS total FROM items i LEFT JOIN stock s ON s.item_id=i.id "
            "WHERE ?='' OR i.type=? GROUP BY i.id ORDER BY i.name", (type, type))]
    return page(req, "items.html", items=rows, type=type)


def card_form(req, status=200, it=None, type="", name="", vals=None, errors=(), box="", qty=0):
    return page(req, "item_form.html", status, it=it, type=type, fields=core.fields_for(type), name=name,
                vals=vals or {}, errors=errors, box=box, qty=qty)


def check_type(type):
    if type not in core.TYPES:
        raise ValueError(f"Нет такого типа: {type}")
    return type


@app.get("/items/new")
def item_new(req: Request, box: str = "", type: str = ""):
    if not type:
        return page(req, "type_pick.html", box=box.upper())
    return card_form(req, type=check_type(type), box=box.upper(), qty=1)


@app.post("/items/new")
async def item_create(req: Request, type: str):
    fields = core.fields_for(check_type(type))
    name, f, errors, form = await read_fields(req, {}, fields)
    box_id, qty = str(form.get("box", "")).strip().upper(), int(form.get("qty") or 0)
    if box_id:
        with db() as c:
            if not c.execute("SELECT 1 FROM boxes WHERE id=?", (box_id,)).fetchone():
                errors.append(f"Нет коробки {box_id}")
    if errors:
        return card_form(req, 400, type=type, name=name, vals=f, errors=errors, box=box_id, qty=qty)
    with db() as c:
        iid = c.execute("INSERT INTO items(name, type, fields) VALUES (?, ?, ?)",
                        (name, type, json.dumps(f, ensure_ascii=False))).lastrowid
    if box_id and qty > 0:
        core.move(iid, box_id, qty, "put", AUTHOR)
    return go(f"/i/{iid}")


@app.get("/i/{item_id}")
def item(req: Request, item_id: int):
    with db() as c:
        it = get_item(c, item_id)
        return page(req, "item.html", it=it, f=json.loads(it["fields"]), fields=core.fields_for(it["type"]),
                    stock=core.stock_of_item(c, item_id), projects=projects(c),
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
    with db() as c:
        c.execute("UPDATE items SET name=?, type=?, fields=?, updated_at=datetime('now','localtime') WHERE id=?",
                  (name, type, json.dumps(f, ensure_ascii=False), item_id))
    return go(f"/i/{item_id}")


# --- stock ---

@app.post("/stock")
def stock(box: str = Form(), item: int = Form(), action: str = Form(), qty: int = Form(1),
          kind: str = Form("put"), project: str = Form(""), back: str = Form("/")):
    box = box.strip().upper()
    if action == "set":
        with db() as c:
            row = c.execute("SELECT qty FROM stock WHERE box_id=? AND item_id=?", (box, item)).fetchone()
        core.move(item, box, max(qty, 0) - (row["qty"] if row else 0), "count", AUTHOR)
    elif qty < 1:
        raise ValueError("Количество должно быть больше нуля")
    elif action == "take":
        core.move(item, box, -qty, "take", AUTHOR, int(project) if project else None)
    else:
        core.move(item, box, qty, kind if kind in ("put", "return", "buy") else "put", AUTHOR)
    return go(back if back.startswith("/") and not back.startswith("//") else "/")


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
