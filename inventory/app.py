"""Web pages. Plain HTML forms, no JS framework: works on any phone in the LAN."""
import asyncio
import io
import json
import os
import re
import tempfile
from datetime import date
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from types import SimpleNamespace

import qrcode
from fastapi import Body, FastAPI, Form, Header, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup
from PIL import Image, ImageDraw, ImageFont
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException

from . import core
from .core import MOVES, PROFILE, db
from .mcp_server import server as mcp_server

core.init()


async def github_sync():
    while True:
        if core.GITHUB_OWNER or core.GITHUB_TOKEN:  # set on /settings any time: picked up next round
            try:
                if n := await asyncio.to_thread(lambda: core.sync_projects(core.fetch_repos())):
                    print(f"github: {n} new repos in the projects inbox", flush=True)
            except Exception as e:  # network down, token revoked: try again next round
                print(f"github sync failed: {e}", flush=True)
        await asyncio.sleep(core.GITHUB_SYNC_MIN * 60)


@asynccontextmanager
async def lifespan(_):
    sync = asyncio.create_task(github_sync())
    async with mcp_server.session_manager.run():
        yield
    sync.cancel()


app = FastAPI(title="beta-inventory", lifespan=lifespan)


class ReplaceNav:
    """№67: a form base.html sends by fetch (X-Replace) gets its 303 as 204 + X-Location, and the page replaces itself:
    back goes where you came from, not through every form. fetch can't do it alone: it follows the redirect and drops #p12."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not any(k == b"x-replace" for k, _ in scope["headers"]):
            return await self.app(scope, receive, send)

        async def swap(m):
            if m["type"] == "http.response.start" and m["status"] == 303:
                m = {**m, "status": 204, "headers": [(b"x-location", v) for k, v in m["headers"] if k == b"location"]}
            await send(m)
        await self.app(scope, receive, swap)


app.add_middleware(ReplaceNav)
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
    unit_of=core.unit_of,
    core=core,  # settings change at run time (/settings): templates read core.TRASH_DAYS, core.SCAN_NFC…
    pk=next((f["key"] for f in PROFILE["item_fields"] if f["type"] == "photo"), None), TRANSIT=core.TRANSIT,
    rk=(RK := next((f["key"] for f in PROFILE["item_fields"] if f.get("reorder")), None)))  # the «докупить» threshold


def all_boxes():  # for box fields: named first, by name; each with its place — the top box's (№39: filter, grey hint)
    with db() as c:
        rows = {r["id"]: dict(r) for r in c.execute(
            "SELECT b.id, b.name, b.parent_id, b.place_id, p.name AS place FROM boxes b LEFT JOIN places p ON p.id=b.place_id WHERE b.kind!='hands'")}
    for r in rows.values():
        t, seen = r, {r["id"]}
        while t["parent_id"] in rows and t["parent_id"] not in seen:
            seen.add(t["parent_id"])
            t = rows[t["parent_id"]]
        # №56: a box taken in hand stands nowhere — its place_id only remembers where it goes back to
        r["top"], r["where"] = (None, PROFILE["terms"].get("hands", "На руках")) if t["parent_id"] == core.HANDS else (t["place_id"], t["place"])
    return sorted(rows.values(), key=lambda r: (not r["name"], r["name"] or "", r["id"]))


T.env.globals["all_boxes"] = all_boxes
_md = MarkdownIt("commonmark", {"html": False}).enable("table")  # raw HTML off: cards come from agents too
T.env.filters["md"] = lambda s: Markup(_md.render(s or ""))
T.env.filters["host"] = lambda s: (urlsplit(s).hostname or s).removeprefix("www.")  # a shop link reads as ozon.ru


def qty_text(q, uncounted=False, unit=None):
    """Stock as people read it. None is «есть, не считал»; uncounted: the sum left some piles out; unit: unit_of the thing."""
    if q is None or uncounted and not q:
        return "есть, не считал"
    return f"{q} {unit or PROFILE['terms']['unit']}" + (" + не считал" if uncounted else "")


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
def index(req: Request, q: str = "", put: str = "", type: str = "", cat: str = "", place: str = "",
          reorder: str = "", transit: str = "", hands: str = ""):
    if q:
        items, boxes = core.search(q)
        return page(req, "index.html", q=q, put=put.upper(), items=items, boxes=boxes)
    # №51/№57: no query — what is where. The type list's group heading picks a whole category
    if any(c["key"] == type for c in PROFILE["categories"]):
        cat, type = type, ""
    elif type in core.TYPES:
        cat = core.TYPES[type]["category"]["key"]
    top = {b["id"]: b["top"] for b in all_boxes()}
    with db() as c:  # total leaves «в пути» out (not here yet); have counts it, so an ordered thing leaves «докупить»
        rows = [dict(r, fields=json.loads(r["fields"]), boxes=[], tr=0, inhand=None) for r in c.execute(
            "SELECT i.*, COALESCE(SUM(CASE WHEN s.box_id!=? THEN s.qty END), 0) AS total, COALESCE(SUM(s.qty), 0) AS have, "
            "MAX(s.box_id IS NOT NULL AND s.qty IS NULL) AS uncounted, MAX(s.box_id=?) AS hands FROM items i LEFT JOIN stock s ON s.item_id=i.id "
            "GROUP BY i.id ORDER BY i.name", (core.TRANSIT, core.HANDS))]
        by_id, where = {r["id"]: r for r in rows}, {}
        for s in c.execute("SELECT s.item_id, s.box_id, s.qty, b.name FROM stock s JOIN boxes b ON b.id=s.box_id ORDER BY b.name, b.id"):
            r = by_id[s["item_id"]]
            if s["box_id"] == core.TRANSIT:
                r["tr"] = s["qty"] or 0
            elif s["box_id"] == core.HANDS:
                r["inhand"] = s["qty"]
            else:
                if s["box_id"] not in where:
                    where[s["box_id"]] = core.box_where(c, s["box_id"])
                r["boxes"].append(dict(s, where=where[s["box_id"]], top=top.get(s["box_id"])))
        taken = c.execute("SELECT * FROM boxes WHERE parent_id=? ORDER BY name='', name, id", (core.HANDS,)).fetchall()
        places = c.execute("SELECT id, name FROM places ORDER BY name").fetchall()
        n_boxes = c.execute("SELECT COUNT(*) FROM boxes WHERE kind NOT IN ('hands','transit')").fetchone()[0]
        # the last thing put somewhere and still there — «where did I just leave it»
        recent = c.execute(
            "SELECT r.item_id, r.box_id FROM (SELECT item_id, box_id, MAX(id) AS mid FROM movements "
            "WHERE kind IN ('put','return','buy','move') AND delta>=0 AND box_id NOT IN (?,?) GROUP BY item_id) r "
            "JOIN stock s ON s.item_id=r.item_id AND s.box_id=r.box_id ORDER BY r.mid DESC LIMIT 3", (core.HANDS, core.TRANSIT)).fetchall()
    for r in rows:  # manifesto 3: fewer than the card's threshold; «не считал» is never flagged
        r["low"] = bool(RK and not r["uncounted"] and str(r["fields"].get(RK, "")).isdigit() and r["have"] < int(r["fields"][RK]))
    counts = {"reorder": sum(r["low"] for r in rows), "transit": sum(bool(r["tr"]) for r in rows),
              "hands": sum(bool(r["hands"]) for r in rows) + len(taken)}
    recent = [(by_id[m["item_id"]], next(b for b in by_id[m["item_id"]]["boxes"] if b["box_id"] == m["box_id"])) for m in recent]
    shown = rows
    if hands:  # №28: taken and not put back, or not put away yet; №56: whole boxes too
        shown = [r for r in shown if r["hands"]]
    if reorder:
        shown = [r for r in shown if r["low"]]
    if transit:
        shown = [r for r in shown if r["tr"]]
    if cat:
        shown = [r for r in shown if r["type"] in core.TYPES and core.TYPES[r["type"]]["category"]["key"] == cat and (not type or r["type"] == type)]
    if place.isdigit():  # anything of it in a box standing there
        shown = [r for r in shown if any(b["top"] == int(place) for b in r["boxes"])]
    filtered = bool(hands or reorder or transit or cat or place)
    return page(req, "index.html", q=q, put=put.upper(), rows=shown, n_items=len(rows), n_boxes=n_boxes, places=places,
                taken=taken if not filtered or hands and not (reorder or transit or cat or place) else [],
                recent=[] if filtered else recent, counts=counts, filtered=filtered,
                f=dict(type=type, cat=cat, place=place, reorder=reorder, transit=transit, hands=hands))


# --- boxes ---

@app.get("/boxes")
def boxes(req: Request):  # №54: boxes live in the places tree now; old links and bookmarks land there
    return go("/places" + (f"?{req.url.query}" if req.url.query else ""))


@app.get("/backup")
def backup():
    fd, f = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    core.backup(f)
    return FileResponse(f, filename=f"inventory-{date.today()}.zip", background=BackgroundTask(os.unlink, f))


@app.get("/boxes/sheet")
def label_sheet(req: Request, ids: str = ""):
    """A batch on one sheet, each label at its real size: for a printer that takes paper, not a roll."""
    w, h, _ = core.LABEL
    return page(req, "sheet.html", ids=[valid_box_id(i) for i in ids.split(",") if i], w=w, h=h)


@app.post("/boxes")
def boxes_create(n: int = Form(1)):
    return go("/places?new=" + ",".join(core.new_boxes(max(1, min(n, 100)))))


@app.get("/boxes/new")
def box_new(req: Request):
    """«+ Коробка»: the page of a box not made yet. Leave untouched and there is no box; box_save makes it."""
    return box_page(req, dict(id=core.free_box_id(), name="", kind="box", place_id=None, parent_id=None, photos="[]"), draft=True)


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
        return go("/?hands=1")
    return box_page(req, b)


def box_page(req, b, draft=False):
    with db() as c:
        contents = core.box_contents(c, b["id"])
        children = [dict(k, pics=json.loads(k["photos"] or "[]")) for k in c.execute(
            "SELECT b.*, (SELECT count(*) FROM stock WHERE box_id=b.id) AS n, "
            "(SELECT count(*) FROM boxes k WHERE k.parent_id=b.id) AS kids "
            "FROM boxes b WHERE parent_id=? ORDER BY name='', name, id", (b["id"],))]
        items = [dict(i, box=b["id"]) for i in contents]  # №54 screen 4: what's inside, one flat list to take from
        for k in children:  # ponytail: one level down; a box holding one thing shows the thing, recurse if deeper nests show up
            if k["n"] == 1 and not k["kids"]:
                items += [dict(i, box=k["id"], src=k["name"] or k["id"]) for i in core.box_contents(c, k["id"])]
        items.sort(key=lambda i: i["name"].lower())
        places = c.execute("SELECT * FROM places ORDER BY name").fetchall()
        return page(req, "box.html", b=b, pics=json.loads(b["photos"]), where=core.box_crumbs(c, b["id"]), contents=contents, top=box_top(b["id"]),
                    items=items, nested=[k for k in children if k["kids"] or k["n"] > 1], empty=[k for k in children if not k["kids"] and not k["n"]],
                    children=children, places=places, projects=core.projects(c), draft=draft, picking="from" in req.query_params,
                    nfc=f"{public_base(req)}/b/{b['id']}".lower())  # NFC Tools writes it as typed


def box_top(box_id):  # the place a box stands in: its own, or its top box's
    return next((x["top"] for x in all_boxes() if x["id"] == box_id), None)


@app.post("/b/{box_id}")
async def box_save(req: Request, box_id: str, name: str = Form(""), place: str = Form(""), parent_id: str = Form(""),
                   x_autosave: str = Header("")):
    bid = valid_box_id(box_id)
    form = await req.form()  # photos only when the page's form sent them: a bare POST keeps what the box has
    photos = photo_order(form, "photos") if form.get("photos_on") else None
    parent = core.find_box(parent_id) if parent_id.strip() else None
    with db() as c:  # an error rolls the insert back: a draft stays a draft
        c.execute("INSERT OR IGNORE INTO boxes(id) VALUES (?)", (bid,))
        b = get_box(c, bid)
        if parent == core.HANDS and b["parent_id"] != core.HANDS:  # «на руках» holds things; a box goes there by «взять» (№28, №56)
            raise ValueError(f"«{PROFILE['terms'].get('hands', 'На руках')}» — не коробка")
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
        elif parent == core.HANDS:  # a box in hand stays in hand: «положить на место» takes it back
            c.execute("UPDATE boxes SET name=? WHERE id=?", (name.strip(), bid))
        else:
            c.execute("UPDATE boxes SET name=?, place_id=?, parent_id=?, back=NULL WHERE id=?", (name.strip(), place_id, parent, bid))
        if photos is not None:
            c.execute("UPDATE boxes SET photos=? WHERE id=?", (json.dumps(photos), bid))
        crumbs = core.box_crumbs(c, bid)
    if x_autosave:  # box.html saves as you type and redraws the heading
        return {"id": bid, "name": name.strip(), "crumbs": crumbs, "parent": parent, "place": box_top(bid), "photos": photos}
    return go(f"/b/{bid}")


@app.post("/b/{box_id}/rotate")
def box_rotate(box_id: str, photo: str = Form(), deg: int = Form()):
    return {"photo": core.rotate_box_photo(valid_box_id(box_id), photo, deg)}


@app.post("/b/{box_id}/clear")
def box_clear(box_id: str):
    with db() as c:
        b = get_box(c, box_id)
    core.clear_box(b["id"], AUTHOR)
    return go(f"/b/{b['id']}")


@app.post("/b/{box_id}/take")
def box_take(box_id: str):  # №56: the whole box in hand — gone from its place, remembers where it stood
    with db() as c:
        b = get_box(c, box_id)
        c.execute("UPDATE boxes SET back=parent_id, parent_id=? WHERE id=? AND kind='box' AND parent_id IS NOT ?",
                  (core.HANDS, b["id"], core.HANDS))
    return go(f"/b/{b['id']}")


@app.post("/b/{box_id}/back")
def box_back(box_id: str, back: str = Form("")):  # into the box it came out of; that box gone meanwhile — into its own place
    with db() as c:
        b = get_box(c, box_id)
        c.execute("UPDATE boxes SET parent_id=(SELECT x.id FROM boxes x WHERE x.id=boxes.back), back=NULL WHERE id=? AND parent_id=?",
                  (b["id"], core.HANDS))
    return go(back if back.startswith("/") and not back.startswith("//") else f"/b/{b['id']}")


@app.post("/b/{box_id}/delete")
def box_delete(box_id: str):
    with db() as c:
        b = get_box(c, box_id)
    core.trash("box", b["id"])
    return go(f"/places#p{b['place_id']}" if b["kind"] == "shelf" else "/places")


@app.get("/b/{box_id}/label.png")
def label(req: Request, box_id: str, w: float = 0, h: float = 0):  # a «+ Коробка» page shows it before the box is saved
    b = valid_box_id(box_id)
    return Response(label_png(f"B/{b}", b, public_base(req), w, h), media_type="image/png")


def public_base(req):
    """Address in labels: PUBLIC_BASE_URL from /settings or .env, else whatever the browser used."""
    return (os.environ.get("PUBLIC_BASE_URL") or str(req.base_url)).rstrip("/")


def label_png(path, text, base, w=0, h=0):
    """w, h: the roll in the printer, mm (№61: «Печать» asks with the size the printer reported); 0 — from /settings."""
    w_mm, h_mm, dpi = core.LABEL
    if 5 <= w <= 200 and 5 <= h <= 200:
        w_mm, h_mm = w, h
    W, H = round(w_mm / 25.4 * dpi), round(h_mm / 25.4 * dpi)
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, border=1)
    qr.add_data(f"{base}/{path}".upper())  # upper case = alphanumeric mode = smaller QR
    qr.make(fit=True)
    qr.box_size = max(1, H // (qr.modules_count + 2))
    code = qr.make_image().get_image().convert("1")
    img = Image.new("1", (W, H), 1)
    img.paste(code, (0, (H - code.height) // 2))
    d, room = ImageDraw.Draw(img), W - code.width
    for size in range(H, 7, -2):
        font = ImageFont.load_default(size=size)
        l, t, r, bt = d.textbbox((0, 0), text, font=font)
        if r - l <= room * 0.9 and bt - t <= H * 0.6:
            break
    d.text((code.width + (room - (r - l)) // 2 - l, (H - (bt - t)) // 2 - t), text, font=font, fill=0)
    buf = io.BytesIO()
    img.save(buf, "PNG", dpi=(dpi, dpi))
    return buf.getvalue()


# --- items ---

def save_upload(up, photo=False):
    return core.save_bytes(up.file.read(), Path(up.filename).suffix.lower()[:10], photo)


def photo_order(form, k):
    """Photos as the form lays them out (№64): a kept name, or new:N for the N-th file picked; files it doesn't place go last."""
    ups = [u for u in form.getlist(k) if getattr(u, "filename", "")]
    out = []
    for x in [*map(str, form.getlist(k + "__keep")), *(f"new:{i}" for i in range(len(ups)))]:
        if x.startswith("new:"):
            i = int(x[4:]) if x[4:].isdigit() else len(ups)
            if i < len(ups) and ups[i]:
                out.append(save_upload(ups[i], photo=True))
                ups[i] = None
        elif n := core.own_upload(x):  # only own uploads: a form can't point at other files
            out.append(n)
    return out


async def read_fields(req, old, fields, loose=False):
    """Card form → (name, fields, errors, form), driven entirely by the profile. Loose or «Передать агенту»: required may wait."""
    form = await req.form()
    f = dict(old)
    for fd in fields:
        k, typ = fd["key"], fd["type"]
        if typ == "photo":
            f[k] = photo_order(form, k)
        elif typ == "files":
            for up in form.getlist(k):
                if getattr(up, "filename", ""):
                    f.setdefault(k, []).append({"name": up.filename, "file": save_upload(up)})
        else:
            f[k] = str(form.get(k, "")).strip()
    name = str(form.get("name", "")).strip()
    return name, f, core.clean_fields(name, fields, f, loose or bool(form.get("for_agent"))), form


@app.get("/items")
def items(req: Request):  # №57: the list lives on the main page now; old links and bookmarks land there
    return RedirectResponse("/?" + req.url.query if req.url.query else "/", 302)


def card_form(req, status=200, it=None, type="", name="", vals=None, errors=(), box="", qty="1", dups=(), nfc=False,
              agent=False, single=None):  # qty 1: what a new thing mostly is (№47); single None: the card's, else the type's
    if single is None:
        single = bool(it["single"]) if it and it["single"] is not None else core.TYPES.get(type, {}).get("single", False)
    return page(req, "item_form.html", status, it=it, type=type, fields=core.fields_for(type), name=name,
                vals=vals or {}, errors=errors, box=box, qty=qty, dups=dups, nfc=nfc, agent=agent, single=single)


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
    nfc, agent = bool(form.get("nfc")), bool(form.get("for_agent"))  # №42 «…и записать метку», №41 «Передать агенту»
    # №48: one of a kind — its count is hidden and ignored. The form sends 0 before the box's 1; no field: the type's
    single = form.get("single") == "1" if "single" in form else core.TYPES[type]["single"]
    try:
        box_id = core.find_box(box) if box else None
    except ValueError as e:
        errors.append(str(e))
    if qty and not qty.isdigit():
        errors.append("Количество — целое число; пусто — «не считал»")
    if errors:
        return card_form(req, 400, type=type, name=name, vals=f, errors=errors, box=box, qty=qty, nfc=nfc, agent=agent,
                         single=single)
    if not form.get("dup_ok") and (dups := core.lookalikes(name)):  # asked, not refused: two alike things are real too
        return card_form(req, 409, type=type, name=name, vals=f, box=box, qty=qty, dups=dups, nfc=nfc, agent=agent, single=single)
    iid = core.save_item(None, name, type, f)
    if agent:
        core.set_for_agent(iid, True)
    if single != core.TYPES[type]["single"]:
        core.set_single(iid, single)
    n = 1 if single else int(qty) if qty else None
    if box_id and n != 0:  # None: «есть, не считал»
        core.move(iid, box_id, n, "put", AUTHOR)
    elif not box_id and n:  # no box yet: brought home, in hand until put away (№28)
        core.move(iid, core.HANDS, n, "buy", AUTHOR)
    if nfc:  # the card writes its tag on arrival (base.html)
        return go(f"/i/{iid}?nfc=1")
    return go(f"/b/{box_id}" if box_id else f"/i/{iid}")  # filling a box: back to it for the next item


@app.get("/I/{item_id}")
def item_scan(item_id: int):  # №43: what a thing's tag and label hold — a scan makes it one of a kind
    core.tag_item(item_id)
    return go(f"/i/{item_id}?scan=1")


@app.get("/i/{item_id}")
def item(req: Request, item_id: int):
    with db() as c:
        it = get_item(c, item_id)
        return page(req, "item.html", it=it, f=json.loads(it["fields"]), fields=core.fields_for(it["type"]),
                    stock=core.stock_of_item(c, item_id), projects=core.projects(c),
                    needs=c.execute("SELECT p.id, p.name, n.qty FROM needs n JOIN projects p ON p.id=n.project_id "
                                    "WHERE n.item_id=? ORDER BY p.name", (item_id,)).fetchall(),
                    nfc=f"{public_base(req).lower()}/I/{item_id}", single=core.single_of(it),  # №37, №43: a scan
                    history=c.execute(MOVES + " AND m.item_id=? ORDER BY m.id DESC LIMIT 50", (item_id,)).fetchall())


@app.post("/i/{item_id}/agent")
async def item_for_agent(req: Request, item_id: int):
    core.set_for_agent(item_id, bool((await req.form()).get("on")))
    return go(f"/i/{item_id}")


@app.post("/i/{item_id}/single")
async def item_single(req: Request, item_id: int):
    core.set_single(item_id, bool((await req.form()).get("on")))
    return go(f"/i/{item_id}")


@app.post("/i/{item_id}/tag")
def item_tag(item_id: int):  # the card wrote its tag (base.html)
    core.tag_item(item_id)
    return go(f"/i/{item_id}")


@app.post("/i/{item_id}/need")
def item_need(item_id: int, project: int = Form(), qty: int = Form(0)):
    core.set_need(project, item_id, max(qty, 0))
    return go(f"/i/{item_id}")


@app.get("/i/{item_id}/label.png")
def item_label(req: Request, item_id: int, w: float = 0, h: float = 0):  # №22: a box of screws is a thing, not a box, and still gets a label
    return Response(label_png(f"I/{item_id}", str(item_id), public_base(req), w, h), media_type="image/png")


@app.post("/i/{item_id}/delete")
def item_delete(item_id: int):
    core.trash("item", item_id)
    return go("/")


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
    name, f, errors, _ = await read_fields(req, json.loads(it["fields"]), core.fields_for(check_type(type)), bool(it["for_agent"]))
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
    if kind == "move" and src:  # the item card: a scanned box takes it from the one box it lay in, or «пришло»
        core.transfer(item, src, box, AUTHOR, qty)
    else:
        if action != "take":
            action = "count" if action == "set" else kind if kind in ("put", "return", "buy") else "put"
        core.change_stock(box, item, action, qty, AUTHOR, int(project) if project and action in ("take", "buy") else None)
    return go(back if back.startswith("/") and not back.startswith("//") else "/")


# --- NFC readers (manifesto «Умная коробка»): a tap on a box makes it the reader's current one, a tap on a thing puts it there ---

def readers(c, rid=None):
    """Readers with the box each holds now: a portable one forgets it after READER_FORGET_MIN without a tap."""
    return c.execute("SELECT r.*, b.name box_name, CASE WHEN r.portable AND r.tapped_at < datetime('now','localtime',?) "
                     "THEN NULL ELSE r.box_id END box FROM readers r LEFT JOIN boxes b ON b.id=r.box_id "
                     "WHERE ? IS NULL OR r.id=? ORDER BY r.accepted, r.name='', r.name, r.id",
                     (f"-{core.READER_FORGET_MIN} minutes", rid, rid)).fetchall()


@app.post("/api/tap")
def tap(reader: str = Body(), code: str = Body()):
    """What a reader read. 200 ok, 403 not accepted yet, 404 not ours, 409 no box yet: the firmware beeps by it."""
    no = lambda status, error: JSONResponse({"ok": False, "error": error}, status)
    rid = reader.strip()
    if not rid or len(rid) > 64:
        return no(400, "reader: id считывателя, до 64 символов")
    with db() as c:
        r = next(iter(readers(c, rid)), None)
        if not r:  # a new one shows up on the page by itself
            c.execute("INSERT INTO readers(id) VALUES (?)", (rid,))
    if not r or not r["accepted"]:
        return no(403, "Новый считыватель — примите его на странице «Считыватели»")
    now = "UPDATE readers SET tapped_at=datetime('now','localtime') WHERE id=?"
    if m := re.search(r"/b/([0-9a-z]{5})\b", code, re.I):
        with db() as c:
            b = c.execute("SELECT id, name, kind FROM boxes WHERE id=? AND kind IN ('box', 'shelf')", (m[1].upper(),)).fetchone()
        if not b:
            return no(404, f"Нет коробки {m[1].upper()}")
        moved = r["place_id"] and b["kind"] == "box" and box_top(b["id"]) != r["place_id"]
        with db() as c:
            c.execute("UPDATE readers SET box_id=? WHERE id=?", (b["id"], rid))
            c.execute(now, (rid,))
            if moved:  # it stands where the reader is now
                c.execute("UPDATE boxes SET place_id=?, parent_id=NULL, back=NULL WHERE id=?", (r["place_id"], b["id"]))
        return {"ok": True, "box": b["id"], "name": b["name"]}
    if m := re.search(r"/i/(\d+)", code, re.I):
        with db() as c:
            it = c.execute("SELECT id, name FROM items WHERE id=?", (int(m[1]),)).fetchone()
        if not it:
            return no(404, f"Нет вещи {m[1]}")
        if not r["box"]:
            return no(409, "Сначала коснитесь коробки")
        core.tag_item(it["id"])
        try:
            core.change_stock(r["box"], it["id"], "put", None, r["name"] or rid)
        except ValueError as e:
            return no(400, str(e))
        with db() as c:
            c.execute(now, (rid,))
        return {"ok": True, "box": r["box"], "item": it["id"], "name": it["name"]}
    return no(404, "Это не метка коробки или вещи")


@app.get("/readers")
def readers_page(req: Request):
    with db() as c:
        return page(req, "readers.html", readers=readers(c), places=c.execute("SELECT * FROM places ORDER BY name").fetchall(),
                    forget=core.READER_FORGET_MIN)


@app.post("/readers")
def reader_save(id: str = Form(), name: str = Form(""), place: str = Form(""), portable: str = Form("")):
    with db() as c:
        c.execute("UPDATE readers SET name=?, place_id=?, portable=?, accepted=1 WHERE id=?",
                  (name.strip(), int(place) if place else None, int(bool(portable)), id))
    return go("/readers")


@app.post("/readers/delete")
def reader_delete(id: str = Form()):
    with db() as c:
        c.execute("DELETE FROM readers WHERE id=?", (id,))
    return go("/readers")


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
def phone():
    return RedirectResponse("/settings#phone", 302)  # old bookmarks: the phone steps live in settings now


def settings_page(req, status=200, posted=None, err=None):
    """№53: every setting with where its value comes from; values only .env can change are shown read-only."""
    base = public_base(req).lower()
    u = urlsplit(base)
    with db() as c:
        n = {k: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for k, t in (
            ("items", "items"), ("boxes", "boxes WHERE kind='box'"), ("places", "places"),
            ("projects", "projects"), ("moves", "movements"), ("readers", "readers"))}
    size = lambda b: f"{b / 2**20:.1f} МБ" if b < 2**30 else f"{b / 2**30:.1f} ГБ"
    photos = sum(f.stat().st_size for f in core.UPLOADS.rglob("*") if f.is_file())
    env = core.ENV0.get
    return page(req, "settings.html", status, vals={k: core.setting(k) for k in core.DEFAULTS} | (posted or {}),
                over=core.overrides(), env0=core.ENV0, base=base, origin=f"{u.scheme}://{u.netloc}", n=n, err=err,
                dbsize=size((core.DATA / "inventory.db").stat().st_size), photos=size(photos),
                envonly=[("Порт", env("PORT", "8000")), ("Часовой пояс", env("TZ")), ("Профиль", env("PROFILE")),
                         ("Зеркало pip", env("PIP_INDEX_URL")), ("Модель поиска", core.SEM_MODEL)])


@app.get("/settings")
def settings(req: Request):
    return settings_page(req)


@app.post("/settings")
async def settings_save(req: Request):
    """One form for all. A value equal to .env's drops the override; an empty token field keeps the token."""
    form = await req.form()
    posted = {k: v.strip() for k, v in form.multi_items() if k in core.DEFAULTS}  # checkbox: hidden 0, then 1 — last wins
    changes = {k: None if v == core.ENV0.get(k, core.DEFAULTS[k]) else v for k, v in posted.items()
               if v != core.setting(k) and not (k == "GITHUB_TOKEN" and not v)}
    if form.get("reset") in core.DEFAULTS:
        changes[form["reset"]] = None
    try:
        core.save_settings(changes)
    except ValueError as e:
        k, _, msg = str(e).partition(": ")
        return settings_page(req, 400, posted, (k, msg))
    return go("/settings")


# --- places, projects, history ---

@app.get("/places")
def places(req: Request, new: str = ""):
    """№54: one tree, place › shelf › box; then boxes standing nowhere, then empty ones. A box in hand is in «На руках»."""
    with db() as c:
        boxes = [dict(b, pics=json.loads(b["photos"] or "[]")) for b in c.execute(
            "SELECT b.*, (SELECT count(*) FROM stock WHERE box_id=b.id) AS n, "
            "(SELECT count(*) FROM boxes k WHERE k.parent_id=b.id) AS kids, "
            "(SELECT i.name FROM stock s JOIN items i ON i.id=s.item_id WHERE s.box_id=b.id) AS one "
            "FROM boxes b WHERE kind='box' ORDER BY name='', name, id")]
        under = {}
        for b in boxes:
            under.setdefault(b["parent_id"], []).append(b)
        shelves = c.execute("SELECT b.*, (SELECT count(*) FROM stock WHERE box_id=b.id) AS n "
                            "FROM boxes b WHERE kind='shelf' ORDER BY rowid").fetchall()
        rows = [dict(p, shelves=[dict(s, boxes=under.get(s["id"], [])) for s in shelves if s["place_id"] == p["id"]],
                     boxes=[b for b in under.get(None, []) if b["place_id"] == p["id"]],
                     gone=c.execute("SELECT count(*) FROM stock JOIN boxes b ON b.id=box_id "  # things on its shelves
                                    "WHERE b.place_id=? AND b.kind='shelf'", (p["id"],)).fetchone()[0])
                for p in c.execute("SELECT * FROM places ORDER BY name")]
    nowhere = [b for b in under.get(None, []) if not b["place_id"]]
    return page(req, "places.html", places=rows, shelves=len(shelves), boxes=len(boxes), hands=under.get(core.HANDS, []),
                loose=[b for b in nowhere if b["n"] or b["kids"]], empty=[b for b in nowhere if not (b["n"] or b["kids"])],
                new=[x for x in new.split(",") if x], nfc_base=public_base(req).lower())


@app.get("/places/new")
def place_new(req: Request):  # №19: from a picker's «+ Место»
    return page(req, "places.html", places=[], add=True)


@app.post("/places")
def place_add(name: str = Form(), note: str = Form(""), x_autosave: str = Header("")):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO places(name, note) VALUES (?, ?)", (name.strip(), note.strip()))
        pid = c.execute("SELECT id FROM places WHERE name=?", (name.strip(),)).fetchone()[0]
    return JSONResponse({"id": pid}) if x_autosave else go("/places")


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


@app.get("/more")
def more_page(req: Request):  # №54: whatever the bar has no room for
    return page(req, "more.html")


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
    by = lambda *status: [r for r in rows if r["status"] in status]
    return page(req, "projects.html", projects=by("active", "archived"), inbox=by("inbox"), skipped=by("skipped"))


@app.post("/projects")
def project_add(name: str = Form(), description: str = Form(""), git_url: str = Form("")):
    core.accept_project(git_url, name, description)  # a repo already in the inbox is taken, not doubled
    return go("/projects")


@app.post("/projects/{project_id}")
def project_save(project_id: int, name: str = Form(), description: str = Form(""), git_url: str = Form(""),
                 status: str = Form("active")):
    with db() as c:
        c.execute("UPDATE projects SET name=?, description=?, git_url=?, status=? WHERE id=?",
                  (name.strip(), description.strip(), git_url.strip(), status, project_id))
    return go("/projects")


@app.get("/p/{project_id}")
def project_page(req: Request, project_id: int):
    with db() as c:
        p = c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not p:
        raise HTTPException(404, "Нет такого проекта")
    needs = [dict(n, unit=core.unit_of(n["type"], json.loads(n["fields"]))) for n in core.project_needs(project_id)]
    return page(req, "project.html", p=p, needs=needs)


@app.post("/p/{project_id}/order")
def project_order(project_id: int):
    """Everything the project is short of goes «в пути», each line for this project."""
    for n in core.project_needs(project_id):
        if n["short"]:
            core.change_stock(core.TRANSIT, n["item_id"], "buy", n["short"], AUTHOR, project_id)
    return go(f"/p/{project_id}")


@app.get("/history")
def history(req: Request):
    with db() as c:
        rows = c.execute(MOVES + " ORDER BY m.id DESC LIMIT 300").fetchall()
    return page(req, "history.html", moves=rows)
