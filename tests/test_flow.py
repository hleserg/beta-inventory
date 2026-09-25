"""One walk through the main path: box → card → search → take → clear."""
import io
import json
import re
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from inventory import core
from inventory.app import app

c = TestClient(app)


def photo():
    b = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(b, "PNG")
    return {"photo": ("p.png", b.getvalue(), "image/png")}


def newest():
    return core.db().execute("SELECT max(id) FROM items").fetchone()[0]


def test_main_path():
    assert core.PROFILE["terms"]["items"] in c.get("/items").text  # t.items once rendered dict.items
    assert c.post("/places", data={"name": "Шкаф"}).status_code == 200
    box = c.post("/boxes", data={"n": 1}, follow_redirects=False).headers["location"].split("=")[1]
    assert f"http://testserver/b/{box.lower()}" in c.get(f"/B/{box.lower()}").text  # QR URL, any case; NFC link
    assert c.get(f"/b/{box}/label.png").headers["content-type"] == "image/png"
    shelf = core.db().execute("SELECT id FROM places WHERE name='Шкаф'").fetchone()[0]
    c.post(f"/b/{box}", data={"name": "JST", "place": shelf})
    assert f'<option value="{shelf}" selected>Шкаф' in c.get(f"/b/{box}").text
    n = core.db().execute("SELECT count(*) FROM places").fetchone()[0]
    assert c.post(f"/b/{box}", data={"name": "JST", "place": "Мой"}).status_code == 400  # a place is chosen, never typed
    assert c.post(f"/b/{box}", data={"name": "JST", "place": "9999"}).status_code == 400
    assert core.db().execute("SELECT count(*) FROM places").fetchone()[0] == n

    # module: photo and pinout required; resistor: photo optional (type overrides the common field)
    assert c.post("/items/new?type=module", data={"name": "MP1584"}).status_code == 400
    assert c.post("/items/new?type=resistor", data={"name": "10k", "value": "10 кОм"},
                  follow_redirects=False).status_code == 303
    r = c.post("/items/new?type=module", files=[*photo().items(), *photo().items()], follow_redirects=False,
               data={"name": "MP1584 mini buck", "aliases": "понижайка", "pinout": "IN+ IN- OUT+ OUT-",
                     "box": box, "qty": "5"})
    assert r.headers["location"] == f"/b/{box}"  # put in a box: back to the box, the next item goes in
    iid = newest()
    pics = lambda: json.loads(core.db().execute("SELECT fields FROM items WHERE id=?", (iid,)).fetchone()["fields"])["photo"]
    a, b = pics()  # several photos on one card
    r = c.post(f"/i/{iid}/edit?type=module", follow_redirects=False, files=photo(), data={
        "name": "MP1584 mini buck", "aliases": "понижайка", "pinout": "IN+ IN- OUT+ OUT-",
        "photo__keep": [b, "../inventory.db"], "description": "до 3 А"})  # a removed, one added; only own uploads kept
    assert r.status_code == 303 and "до 3 А" in c.get(f"/i/{iid}").text
    assert pics()[0] == b and len(pics()) == 2 and a not in c.get(f"/i/{iid}").text
    r = c.post(f"/i/{iid}/rotate", data={"photo": b, "deg": 90})  # №21: a turned photo is a new file, the old one may be cached
    assert r.json()["photo"] == pics()[0] != b and Image.open(core.UPLOADS / pics()[0]).size == (30, 40)
    assert c.post(f"/i/{iid}/rotate", data={"photo": a, "deg": -90}).status_code == 400  # not this card's photo
    assert f"/i/{iid}/label.png" in c.get(f"/i/{iid}").text  # №22: a thing gets its own printed label; its QR is upper case
    assert c.get(f"/i/{iid}/label.png").headers["content-type"] == "image/png"
    assert "MP1584" in c.get("/?q=ПОНИЖАЙКА").text
    assert "10k" in c.get("/?q=резистор").text  # type label is searchable
    assert "10k" in c.get("/items?cat=electronics").text and "10k" not in c.get("/items?cat=tools").text
    found = c.get("/items?q=резистор").text  # the list has its own search line (№33)
    assert 'name="q" value="резистор"' in found and "10k" in found and "MP1584" not in found
    assert "10k" in c.get("/?type=electronics").text and "10k" not in c.get("/?type=tools").text  # a category from the type list (№57)

    c.post("/projects", data={"name": "Метеостанция"})
    assert c.post("/stock", data={"box": box, "item": iid, "action": "take", "qty": 2, "project": 1}).status_code == 200
    assert "3 шт" in c.get(f"/b/{box}").text
    assert c.post("/stock", data={"box": box, "item": iid, "action": "take", "qty": 99}).status_code == 400

    c.post(f"/b/{box}/clear")
    assert "Что кладём" in c.get(f"/b/{box}").text
    assert "Метеостанция" in c.get("/history").text


class StubModel:
    """Two 'meanings': voltage converters and everything else."""
    def embed(self, texts):
        for t in texts:
            yield [1.0, 0.1] if any(w in t.lower() for w in ("понижайка", "step-down")) else [0.1, 1.0]


def test_meaning():
    core._sem["model"] = StubModel()
    try:
        c.post("/items/new?type=module", files=photo(), data={"name": "LM2596", "description": "DC-DC step-down",
                                                              "pinout": "IN OUT"})
        page = c.get("/?q=понижайка").text
        assert "Похоже по смыслу" in page and "LM2596" in page
        assert page.index("MP1584") < page.index("Похоже по смыслу")  # keyword hit (alias) ranks first
    finally:
        core._sem["model"] = None


def test_phone_app():
    """Installable app + writing NFC tags from the phone, with setup steps per platform."""
    m = c.get("/manifest.webmanifest").json()
    assert m["display"] == "standalone" and m["scope"] == "/"  # labels' /b/ links fall in the app's scope
    assert "nfcScan" in c.get("/").text and "nfcScan()" in c.get("/phone").text  # a scanned label opens its box
    assert all('id="scan"' in c.get(u).text for u in ("/", "/places", "/history"))  # the QR scan button on every page
    assert c.get("/more").status_code == 200 and 'class="bar"' in c.get("/more").text  # №54
    assert 'class="bar"' not in c.get("/items/new?type=module").text  # a new thing: its own .save, no bar
    assert "data-draft" in c.get("/items/new?type=module").text  # unsaved card edits survive leaving the page
    box = core.new_boxes(1)[0]
    new = c.get(f"/items/new?type=module&box={box}").text
    assert f'value="{box}"' in new and f'<option value="{box}" ' in new  # opened from a box: that box, its name shown
    assert "serviceWorker" in c.get("/").text and "/offline" in c.get("/sw.js").text
    assert c.get("/offline").status_code == 200
    box = core.new_boxes(1)[0]
    page = c.get(f"/b/{box}").text
    assert "NDEFReader" in page and f"http://testserver/b/{box.lower()}" in page
    phone = c.get("/phone").text
    assert "chrome://flags/#unsafely-treat-insecure-origin-as-secure" in phone and "http://testserver" in phone
    assert "apps.apple.com/app/nfc-tools/id1252962749" in phone and 'id="asktake"' in phone  # №43: «Взять?» can be turned back on


def test_address_by_ip(monkeypatch):
    """NFC link and the Chrome flag origin follow PUBLIC_BASE_URL, IP and port included."""
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://192.168.1.5:8000")
    box = core.new_boxes(1)[0]
    assert f"http://192.168.1.5:8000/b/{box.lower()}" in c.get(f"/b/{box}").text
    assert 'value="http://192.168.1.5:8000"' in c.get("/phone").text


def test_uncounted():
    """«Есть, не считал» (manifesto 4): loose parts go in without a number, stay out of sums, can still be taken."""
    box = core.new_boxes(1)[0]
    r = c.post("/items/new?type=resistor", data={"name": "1k россыпь", "value": "1 кОм", "box": box, "qty": ""},
               follow_redirects=False)
    rid = newest()
    assert "есть, не считал" in c.get(f"/b/{box}").text and "есть, не считал" in c.get("/items").text
    assert c.post("/stock", data={"box": box, "item": rid, "action": "take", "qty": 3}).status_code == 200
    assert "есть, не считал" in c.get(f"/i/{rid}").text  # taking some leaves the pile uncounted
    c.post("/stock", data={"box": box, "item": rid, "action": "set", "qty": 40})
    assert "40 шт" in c.get(f"/b/{box}").text  # counted: a number again
    c.post("/stock", data={"box": box, "item": rid, "action": "add", "kind": "put", "qty": ""})
    assert "43 шт" in c.get(f"/b/{box}").text  # no number, 3 in hand (№28): those 3 go back
    c.post("/stock", data={"box": box, "item": rid, "action": "add", "kind": "put", "qty": ""})
    assert "есть, не считал" in c.get(f"/b/{box}").text  # nothing in hand: a handful more, uncounted
    c.post("/stock", data={"box": box, "item": rid, "action": "add", "kind": "return", "qty": 2})
    assert "2 шт" in c.get(f"/b/{box}").text  # «вернул 2» to an uncounted pair: now there are 2 (№34)
    c.post(f"/b/{box}/clear")
    assert "Что кладём" in c.get(f"/b/{box}").text


def test_lookalike_asks():
    """№32: a second card with the same name is asked about, not refused."""
    data = {"name": "Клещи для зачистки", "value": "1", "box": "", "qty": ""}
    c.post("/items/new?type=resistor", data=data, follow_redirects=False)
    first = newest()
    r = c.post("/items/new?type=resistor", data={**data, "name": "клещи  для ЗАЧИСТКИ"}, follow_redirects=False)
    assert r.status_code == 409 and f'href="/i/{first}"' in r.text and newest() == first
    r = c.post("/items/new?type=resistor", data={**data, "dup_ok": "1"}, follow_redirects=False)
    assert r.status_code == 303 and newest() != first


def test_box_by_name():
    """A box field takes what people know: part of the name in any case, «Name (ID)» from the list, or the ID."""
    a, b, d = core.new_boxes(3)
    c.post(f"/b/{a}", data={"name": "Клеммники Phoenix"})
    c.post(f"/b/{b}", data={"name": "Клеммники WAGO"})
    for typed in ("phoenix", "КЛЕММНИКИ phoenix", f"Клеммники Phoenix ({a})", a.lower()):
        r = c.post("/items/new?type=resistor", follow_redirects=False,
                   data={"dup_ok": "1", "name": "220R", "value": "220 Ом", "box": typed, "qty": "1"})
        assert r.headers["location"] == f"/b/{a}", typed
    r = c.post("/items/new?type=resistor", data={"name": "220R", "value": "220 Ом", "box": "клеммники", "qty": "1"})
    assert r.status_code == 400 and "Клеммники WAGO" in r.text and 'value="клеммники"' in r.text  # which one? typed text kept

    assert c.post("/stock", data={"box": "wago", "item": newest(), "action": "add", "kind": "put", "qty": 2}).status_code == 200
    assert "2 шт" in c.get(f"/b/{b}").text
    assert c.post("/stock", data={"box": "нет такой", "item": newest(), "action": "add", "qty": 2}).status_code == 400
    c.post(f"/b/{d}", data={"name": "Ящик", "parent_id": "wago"})
    assert core.db().execute("SELECT parent_id FROM boxes WHERE id=?", (d,)).fetchone()[0] == b

    page = c.get(f"/b/{d}").text  # names first, the ID last; QR next to the field, NFC needs no button (№29)
    assert f'value="Клеммники WAGO ({b})"' in page and "data-nfc>" not in page and "data-nfc " not in page and "data-qr" in page
    assert f'<option value="Клеммники Phoenix ({a})" ' in c.get("/items/new?type=module").text
    assert f'<option value="Клеммники Phoenix ({a})" ' in c.get(f"/i/{newest()}").text

    named = f'<b>Клеммники WAGO</b> <span class="mut">{b}</span>'  # the name first, the ID grey at the end
    for url in (f"/i/{newest()}", "/history", "/?q=wago"):
        assert named in c.get(url).text, url
    places = c.get("/places").text  # №54: the tree names the box, its ID leads the grey line
    assert '<span class="nm">Клеммники WAGO</span>' in places and f'<span class="path">{b} ·' in places
    page = c.get(f"/b/{b}").text
    assert f'<h1>Клеммники WAGO <span class="mut">{b}</span></h1>' in page and f'<b>Ящик</b> <span class="mut">{d}</span>' in page


def test_new_box_and_shelves():
    """«+ Коробка» makes a box only once something is done with it; shelves come one at a time from a place."""
    count = lambda: core.db().execute("SELECT count(*) FROM boxes").fetchone()[0]
    before = count()
    page = c.get("/boxes/new").text
    bid = re.search(r"/b/(\w{5})/label\.png", page)[1]
    assert count() == before and 'id="boxf"' in page  # looked and went back: no box
    assert c.get(f"/b/{bid}/label.png").headers["content-type"] == "image/png"  # the label shows before the box exists
    assert c.get(f"/b/{bid}").status_code == 404
    r = c.post(f"/b/{bid}", data={"name": "Макетки"}, headers={"X-Autosave": "1"})  # the first typed name saves it
    assert r.status_code == 200 and count() == before + 1 and "Макетки" in c.get("/places").text
    assert c.post("/b/NOPE!", data={"name": "x"}).status_code == 404  # only IDs the site hands out

    c.post("/places", data={"name": "Стеллаж"})
    pid = core.db().execute("SELECT id FROM places WHERE name='Стеллаж'").fetchone()[0]
    r = c.post(f"/places/{pid}/shelves", data={"name": "Верхняя"}, follow_redirects=False)
    assert r.headers["location"].startswith("/places")  # no label: stay among the places
    r = c.post(f"/places/{pid}/shelves", data={"label": "1"}, follow_redirects=False)
    shelf = r.headers["location"].split("/")[-1]  # with a label: its page, to print and write the tag
    page = c.get(f"/b/{shelf}").text
    assert "Полка 2" in page and "label.png" in page and "Где лежит" not in page  # shelves do not move

    j = c.post(f"/b/{bid}", data={"name": "Макетки", "parent_id": shelf}, headers={"X-Autosave": "1"}).json()
    assert j["parent"] == shelf and j["place"] == pid  # №39: on a shelf, the place is the cabinet's
    assert j["crumbs"] == [["Стеллаж", f"/places#p{pid}"], ["Полка 2", f"/b/{shelf}"]]  # №44: autosave redraws them
    places = c.get("/places").text
    assert places.index("Стеллаж") < places.index("Верхняя") < places.index("Полка 2") < places.index("Макетки")  # №54: one tree
    assert "Макетки" in c.get(f"/b/{shelf}").text  # a box on a shelf is on the shelf's page
    page = c.get(f"/b/{bid}").text
    assert f'<option value="Полка 2 ({shelf})" data-place="{pid}" data-where="Стеллаж">' in page  # «Полка 2» is in every cabinet
    assert f'<a href="/places#p{pid}">Стеллаж</a> › <a href="/b/{shelf}">Полка 2</a>' in page  # №44: the way up, as links
    assert '<div id="placeview">Стеллаж</div>' in page and '<div id="placesel" hidden>' in page  # №39: the place is text
    c.post("/items/new?type=hand_tool", files=photo(), data={"name": "макетка", "box": bid})
    assert (f'<div class="crumbs"><a href="/places#p{pid}">Стеллаж</a> › <a href="/b/{shelf}">Полка 2</a> › '
            f'<a href="/b/{bid}">Макетки</a></div>') in c.get(f"/i/{newest()}").text  # №44: a thing in one place shows the way to it
    other = core.db().execute("SELECT id FROM places WHERE name='Шкаф'").fetchone()[0]
    assert "макетка" in c.get(f"/?place={pid}").text and "макетка" not in c.get(f"/?place={other}").text  # №57: box on a shelf → the cabinet
    j = c.post(f"/b/{bid}", data={"name": "Макетки", "place": str(pid)}, headers={"X-Autosave": "1"}).json()
    assert j["parent"] is None and j["place"] == pid  # out of the shelf, it stays in the cabinet


def test_old_stock_table_migrates(tmp_path, monkeypatch):
    """A database from before «не считал» and shelves keeps its stock and takes qty NULL and shelves after a restart."""
    monkeypatch.setattr(core, "DATA", tmp_path)
    old = core.SCHEMA.replace("qty INTEGER CHECK (qty IS NULL OR qty > 0)", "qty INTEGER NOT NULL CHECK (qty > 0)"
                              ).replace("kind TEXT NOT NULL DEFAULT 'box',", "")  # and boxes from before shelves
    assert old != core.SCHEMA
    with core.db() as d:
        d.executescript(old)
        d.execute("INSERT INTO boxes(id) VALUES ('OLD01')")
        d.executemany("INSERT INTO items(id, name) VALUES (?, ?)", [(1, "a"), (2, "b")])
        d.execute("INSERT INTO stock(box_id, item_id, qty) VALUES ('OLD01', 1, 7)")
    core.init()
    with core.db() as d:
        d.execute("INSERT INTO stock(box_id, item_id, qty) VALUES ('OLD01', 2, NULL)")
        assert [tuple(r) for r in d.execute("SELECT item_id, qty FROM stock ORDER BY item_id")] == [(1, 7), (2, None)]
        assert d.execute("SELECT kind FROM boxes").fetchone()[0] == "box"


def test_box_photos():
    """№45: a box has photos like an item; a POST without the form's marker keeps them."""
    box = c.post("/boxes", data={"n": 1}, follow_redirects=False).headers["location"].split("=")[1]
    c.post(f"/b/{box}", data={"name": "С фото", "photos_on": "1"}, files={"photos": photo()["photo"]})
    [a] = json.loads(core.db().execute("SELECT photos FROM boxes WHERE id=?", (box,)).fetchone()[0])
    assert f"/u/{a}" in c.get(f"/b/{box}").text and f"/u/{a}" in c.get("/places").text
    c.post(f"/b/{box}", data={"name": "С фото"})  # tests and scripts post bare fields
    b = c.post(f"/b/{box}/rotate", data={"photo": a, "deg": 90}).json()["photo"]
    assert b != a and f"/u/{b}" in c.get(f"/b/{box}").text
    c.post(f"/b/{box}", data={"name": "С фото", "photos_on": "1"})  # ✕ on the last one
    assert f"/u/{b}" not in c.get(f"/b/{box}").text


def test_old_single_photo():
    """Cards from before several photos held one name, not a list."""
    iid = core.save_item(None, "старое", "module", {"photo": "x.jpg", "pinout": "A"})
    core.init()
    assert core.item_fields(core.db().execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone())["photo"] == ["x.jpg"]


def test_trash():
    """№18/№27/№32: an item, a box, a place go to the trash at once and come back whole with «Вернуть»."""
    q = lambda sql, *a: core.db().execute(sql, a).fetchone()[0]
    c.post("/places", data={"name": "Антресоль"})
    pid = q("SELECT id FROM places WHERE name='Антресоль'")
    shelf = c.post(f"/places/{pid}/shelves", data={"label": "1"}, follow_redirects=False).headers["location"].split("/")[-1]
    box, inner, loose = core.new_boxes(3)
    c.post(f"/b/{box}", data={"name": "Паяльное", "parent_id": shelf})
    c.post(f"/b/{inner}", data={"name": "Жала", "parent_id": box})
    c.post(f"/b/{loose}", data={"name": "Разное", "place": pid})
    c.post("/items/new?type=resistor", data={"dup_ok": "1", "name": "Флюс ЛТИ", "value": "1", "box": box, "qty": "3"})
    item = newest()
    assert f'action="/places/{pid}/delete"' in c.get("/places").text

    r = c.post(f"/i/{item}/delete", follow_redirects=False)  # an item: gone from lists, history and search
    assert r.status_code == 303 and "Флюс ЛТИ" not in c.get("/items").text + c.get(f"/b/{box}").text + c.get("/history").text
    assert c.get(f"/i/{item}").status_code == 404 and "Флюс ЛТИ" in c.get("/trash").text
    back = c.post(f"/trash/{q('SELECT max(id) FROM trash')}", follow_redirects=False).headers["location"]
    assert back == f"/i/{item}" and "3 шт" in c.get(f"/b/{box}").text and "Флюс ЛТИ" in c.get("/history").text

    c.post(f"/b/{box}/delete")  # a box: its stock goes with it, boxes inside move up a level
    assert c.get(f"/b/{box}").status_code == 404 and "Нигде нет" in c.get(f"/i/{item}").text
    assert q("SELECT parent_id FROM boxes WHERE id=?", inner) == shelf
    c.post("/places/%d/delete" % pid)  # a place: its shelves go too, boxes on it lose the place (№27)
    assert "Антресоль" not in c.get("/places").text and q("SELECT place_id FROM boxes WHERE id=?", loose) is None
    assert c.get(f"/b/{shelf}").status_code == 404 and q("SELECT parent_id FROM boxes WHERE id=?", inner) is None

    for t in core.db().execute("SELECT id FROM trash ORDER BY id DESC LIMIT 2").fetchall():  # place, then box
        c.post(f"/trash/{t[0]}")
    assert q("SELECT place_id FROM boxes WHERE id=?", loose) == pid and q("SELECT parent_id FROM boxes WHERE id=?", inner) == box
    assert q("SELECT parent_id FROM boxes WHERE id=?", box) == shelf and "3 шт" in c.get(f"/b/{box}").text

    core.db().execute("UPDATE trash SET at=datetime('now','-31 days')").connection.commit()
    c.post(f"/i/{item}/delete")  # fresh stays, older than TRASH_DAYS goes for good
    assert "Флюс ЛТИ" in c.get("/trash").text and q("SELECT count(*) FROM trash WHERE at < datetime('now','-30 days')") == 0


def test_scan_moves_to_box():
    """№30: a box scanned on an item card that lies in one box moves all of it there, counted or not."""
    a, b = core.new_boxes(2)
    for name, qty in (("клеммник", 5), ("стяжки россыпь", "")):
        c.post("/items/new?type=resistor", data={"name": name, "value": "x", "box": a, "qty": qty})
        iid = newest()
        c.post("/stock", data={"box": b, "item": iid, "action": "add", "kind": "move", "src": a})
        with core.db() as db:
            rows = db.execute("SELECT box_id, qty FROM stock WHERE item_id=?", (iid,)).fetchall()
            kinds = [r[0] for r in db.execute("SELECT kind FROM movements WHERE item_id=? ORDER BY id", (iid,))]
        assert [tuple(r) for r in rows] == [(b, qty or None)]  # nothing left behind, the count travels as is
        assert kinds[-2:] == ["move", "move"]
    assert "переложил" in c.get(f"/i/{iid}").text


def test_item_tag():
    """№37: a thing's card writes its own link to an NFC tag, like a box page does."""
    c.post("/items/new?type=resistor", data={"name": "мультиметр", "value": "x"})
    iid = newest()
    assert f'data-nfcw="http://testserver/I/{iid}"' in c.get(f"/i/{iid}").text  # №43: a tag lands on /I/ = a scan
    box = core.new_boxes(1)[0]
    assert f'data-nfcw="http://testserver/b/{box.lower()}"' in c.get(f"/b/{box}").text
    # №42: a tag at creation too — to the card, not back to the box, and the card starts writing (base.html)
    assert 'name="nfc"' in c.get("/items/new?type=resistor").text
    r = c.post("/items/new?type=resistor", follow_redirects=False, data={"name": "шуруповёрт", "value": "x", "box": box, "nfc": "1"})
    assert r.headers["location"] == f"/i/{newest()}?nfc=1"
    r = c.post("/items/new?type=resistor", follow_redirects=False, data={"name": "шуруповёрт", "value": "x", "nfc": "1"})
    assert r.status_code == 409 and 'name="nfc" value="1"' in r.text  # «Всё равно создать» keeps the wish


def test_single():
    """№43: a one-of-a-kind thing (a tool, anything with a tag) is taken and put back without a count, and lies in one place."""
    def stock(iid):
        with core.db() as db:
            return {r[0]: r[1] for r in db.execute("SELECT box_id, qty FROM stock WHERE item_id=?", (iid,))}
    flag = lambda iid: core.db().__enter__().execute("SELECT single FROM items WHERE id=?", (iid,)).fetchone()[0]
    a, b = core.new_boxes(2)
    c.post("/items/new?type=hand_tool", files=photo(), data={"name": "бокорезы", "box": a})
    iid = newest()
    assert stock(iid) == {a: 1}  # tools are one of a kind by the profile: no count is still one
    card = c.get(f"/i/{iid}").text
    assert 'value="1" min="0"' not in card and "Пересчитать" not in card and 'id="single" checked' in card
    c.post("/stock", data={"box": a, "item": iid, "action": "take"})
    assert stock(iid) == {core.HANDS: 1}
    c.post("/stock", data={"box": b, "item": iid, "action": "add", "kind": "put"})
    assert stock(iid) == {b: 1}
    c.post("/stock", data={"box": a, "item": iid, "action": "add", "kind": "put"})
    assert stock(iid) == {a: 1}  # put elsewhere = moved, not a second one
    c.post("/stock", data={"box": a, "item": iid, "action": "add", "kind": "put"})
    assert stock(iid) == {a: 1}
    assert 'data-take' in c.get(f"/i/{iid}?scan=1").text

    c.post("/items/new?type=resistor", data={"name": "паяльник из коробки", "value": "x", "box": a})
    rid = newest()
    assert flag(rid) is None and stock(rid) == {a: None}
    r = c.get(f"/I/{rid}", follow_redirects=False)  # a scanned tag: the thing is one of a kind from now on
    assert r.headers["location"] == f"/i/{rid}?scan=1" and flag(rid) == 1 and stock(rid) == {a: 1}
    c.post(f"/i/{rid}/single")  # unticked by hand
    assert flag(rid) == 0
    c.get(f"/I/{rid}")
    assert flag(rid) == 0  # a rescan keeps the hand's choice
    assert c.post(f"/i/{rid}/tag").status_code == 200 and flag(rid) == 0  # written from the site: same rule

    # №48: a new card of one of a kind hides its count, reorder level and unit; ticked by hand, it is one
    assert re.search(r'name="single" value="1" checked.*data-many hidden', c.get("/items/new?type=hand_tool").text, re.S)
    new = c.get("/items/new?type=resistor").text
    assert "data-many hidden" not in new and 'name="qty" value="1"' in new  # №47: one by default
    c.post("/items/new?type=resistor", data={"name": "осциллограф", "value": "x", "box": b, "qty": "5", "single": "1"})
    assert flag(newest()) == 1 and stock(newest()) == {b: 1}
    c.post("/items/new?type=hand_tool", files=photo(), data={"name": "клеевой пистолет", "box": b, "qty": "3", "single": "0"})
    assert flag(newest()) == 0 and stock(newest()) == {b: 3}  # unticked: counted like anything else


def test_for_agent():
    """№41: «Передать агенту» — the card waits for an agent to fill it in (agent_queue); an agent's update clears it."""
    flag = lambda iid: core.db().__enter__().execute("SELECT for_agent FROM items WHERE id=?", (iid,)).fetchone()[0]
    assert 'name="for_agent"' in c.get("/items/new?type=resistor").text
    c.post("/items/new?type=resistor", data={"name": "плата без надписей", "value": "x", "for_agent": "1"})
    iid = newest()
    assert flag(iid) == 1 and 'name="on" value="1" checked' in c.get(f"/i/{iid}").text
    assert c.post(f"/i/{iid}/agent", follow_redirects=False).headers["location"] == f"/i/{iid}"
    assert flag(iid) == 0 and "Передать агенту" in c.get(f"/i/{iid}").text
    c.post(f"/i/{iid}/agent", data={"on": "1"})
    assert flag(iid) == 1


def test_hands():
    """№28: a thing with a count and no box is «на руках»; a take puts it there, a put or return takes it back out."""
    def stock(iid):
        with core.db() as db:
            return {r[0]: r[1] for r in db.execute("SELECT box_id, qty FROM stock WHERE item_id=?", (iid,))}
    box, placed = core.new_boxes(2)
    with core.db() as db:
        pid = db.execute("INSERT INTO places(name) VALUES ('стол')").lastrowid
        db.execute("UPDATE boxes SET place_id=? WHERE id=?", (pid, placed))
    c.post("/items/new?type=resistor", data={"name": "модуль с ПВЗ", "value": "x", "qty": 3})
    iid = newest()
    assert stock(iid) == {core.HANDS: 3} and "На руках" in c.get(f"/i/{iid}").text
    assert "модуль с ПВЗ" in c.get("/items?hands=1").text
    c.post("/stock", data={"box": box, "item": iid, "action": "add", "kind": "put", "qty": 2})
    assert stock(iid) == {core.HANDS: 1, box: 2}
    c.post("/stock", data={"box": box, "item": iid, "action": "take", "qty": 1})
    assert stock(iid) == {core.HANDS: 2, box: 1} and f"взят из {box}" in c.get(f"/i/{iid}").text
    c.post("/stock", data={"box": core.HANDS, "item": iid, "action": "take", "qty": 2})
    assert stock(iid) == {box: 1} and "списал" in c.get(f"/i/{iid}").text
    assert "модуль с ПВЗ" not in c.get("/items?hands=1").text
    places = c.get("/places").text  # a box with no place is under «Без места», a placed one under its place
    loose = places[places.index('id="loose"'):]
    assert box in loose and placed in places and placed not in loose and core.HANDS not in places
    assert c.get(f"/b/{core.HANDS}", follow_redirects=False).headers["location"] == "/?hands=1"
    assert f"({core.HANDS})" not in c.get(f"/i/{iid}").text  # not offered as a box to put into
    n = c.get(f"/i/{iid}").text.count("положил")
    c.post("/stock", data={"box": core.HANDS, "item": iid, "action": "add", "kind": "put", "qty": 1})
    assert stock(iid) == {box: 1, core.HANDS: 1} and c.get(f"/i/{iid}").text.count("положил") == n + 1  # not a mirror leg
    assert c.post("/stock", data={"box": "руках", "item": iid, "action": "add", "qty": 1}).status_code == 400  # not by name


def test_take_box():
    """№56: a whole box goes «на руки» — gone from its place, listed with what's in hand, back where it was on «положить»."""
    def row(bid):
        with core.db() as db:
            return dict(db.execute("SELECT parent_id, place_id, back FROM boxes WHERE id=?", (bid,)).fetchone())
    outer, box, inner = core.new_boxes(3)
    with core.db() as db:
        pid = db.execute("INSERT INTO places(name) VALUES ('антресоль')").lastrowid
        db.execute("UPDATE boxes SET name='ящик', place_id=? WHERE id=?", (pid, outer))
        db.execute("UPDATE boxes SET name='крепёж', place_id=? WHERE id=?", (pid, box))
        db.execute("UPDATE boxes SET name='винты', parent_id=? WHERE id=?", (outer, inner))
    assert "Взять на руки" in c.get(f"/b/{box}").text
    c.post(f"/b/{box}/take")
    where = lambda bid: core.box_where(core.db(), bid)
    assert where(box) == "На руках" and f"/b/{box}" in c.get("/items?hands=1").text
    page = c.get(f"/b/{box}").text
    assert "Положить на место" in page and "антресоль" in page  # says where it goes back to
    r = c.post(f"/b/{box}", data={"name": "крепёж М3", "parent_id": core.HANDS}, headers={"X-Autosave": "1"})
    assert r.status_code == 200 and row(box)["parent_id"] == core.HANDS  # autosaving the name keeps it in hand
    assert c.post(f"/b/{outer}", data={"name": "ящик", "parent_id": core.HANDS}).status_code == 400  # a box is taken by the button
    c.post(f"/b/{box}/back")
    assert row(box) == {"parent_id": None, "place_id": pid, "back": None} and where(box) == "антресоль"
    assert f"/b/{box}" not in c.get("/items?hands=1").text
    c.post(f"/b/{inner}/take")
    assert row(inner)["parent_id"] == core.HANDS and f"/b/{inner}" not in c.get(f"/b/{outer}").text
    c.post(f"/b/{inner}/back")
    assert row(inner)["parent_id"] == outer


def test_transit():
    """Manifesto 3: ordered things lie «в пути» — out of the total, in the «докупить» check; «пришло» moves them in."""
    def stock(iid):
        with core.db() as db:
            return {r[0]: r[1] for r in db.execute("SELECT box_id, qty FROM stock WHERE item_id=?", (iid,))}
    box = core.new_boxes(1)[0]
    c.post("/items/new?type=resistor", data={"name": "защита АКБ", "value": "x", "reorder_at": "5", "box": box, "qty": 3})
    iid = newest()
    assert "защита АКБ" in c.get("/items?reorder=1").text
    c.post("/stock", data={"box": "в пути", "item": iid, "action": "add", "kind": "put", "qty": 10})
    assert stock(iid) == {box: 3, core.TRANSIT: 10}
    assert "защита АКБ" not in c.get("/items?reorder=1").text  # ordered: no need to buy again
    tr = c.get("/?transit=1").text  # №57: the «в пути» filter; the count shown leaves it out — not 13, it is not here yet
    assert "защита АКБ" in tr and "3 шт" in tr and "13 шт" not in tr and "10k" not in tr
    card = c.get(f"/i/{iid}").text
    assert "В пути" in card and "заказано" in card and "Пришло" in card
    c.post("/stock", data={"box": box, "item": iid, "action": "add", "kind": "move", "src": core.TRANSIT, "qty": 12})
    assert stock(iid) == {box: 15}  # 12 came of the 10 ordered
    assert core.TRANSIT not in c.get("/places").text
    c.post(f"/b/{core.TRANSIT}/delete")
    assert c.get(f"/b/{core.TRANSIT}").status_code == 200


def test_unit():
    """Manifesto 4: a thing counts in its own unit — the card's choice, else its type's default, else the profile's."""
    box = core.new_boxes(1)[0]
    assert "<option selected>м</option>" in c.get("/items/new?type=wire").text  # wire: metres unless changed
    c.post("/items/new?type=wire", data={"name": "МГТФ 0.12", "box": box, "qty": 7})  # left empty: still metres
    wire = newest()
    c.post("/items/new?type=solder", data={"name": "припой ПОС-61", "unit": "г", "box": box, "qty": 100})
    c.post("/items/new?type=resistor", data={"name": "1к", "value": "1 кОм", "box": box, "qty": 3})
    assert "7 м" in c.get(f"/i/{wire}").text and "7 м" in c.get("/?q=МГТФ").text
    page = c.get(f"/b/{box}").text
    assert "7 м" in page and "100 г" in page and "3 шт" in page
    assert "100 г" in c.get("/items?q=припой").text


def test_diy_fields():
    """Manifesto 5: a module says its bus and I2C address and is found by them; power modules and batteries are types."""
    box = core.new_boxes(1)[0]
    assert "Адрес I2C" in c.get("/items/new?type=module").text and "Flash / PSRAM" in c.get("/items/new?type=mcu_module").text
    assert "Выход, В" in c.get("/items/new?type=power_module").text and "Ёмкость" in c.get("/items/new?type=battery").text
    c.post("/items/new?type=module", files=photo(), data={
        "name": "BME280", "pinout": "VCC GND SCL SDA", "interface": "I2C, SPI", "i2c_address": "0x76", "box": box, "qty": 1})
    assert "BME280" in c.get("/?q=0x76").text and "BME280" in c.get("/?q=spi").text


def test_pick_new():
    """№19: a picker's «+ Место» / «+ Коробка» opens a page that makes one and hands its id back to the field."""
    new = c.get("/places/new?from=/b/K7M2Q&field=place&name=Антресоль").text
    assert 'value="Антресоль"' in new and "Назад" in new  # what was typed in the search comes along
    r = c.post("/places", data={"name": "Антресоль"}, headers={"X-Autosave": "1"})
    pid = core.db().execute("SELECT id FROM places WHERE name='Антресоль'").fetchone()[0]
    assert r.json() == {"id": pid}
    assert c.post("/places", data={"name": "Антресоль"}, headers={"X-Autosave": "1"}).json() == {"id": pid}  # a twin: the old one
    assert 'data-new="/places/new"' in c.get("/boxes/new").text and 'data-new="/boxes/new"' in c.get("/items/new?type=resistor").text
    box = c.get("/boxes/new?from=/items/new&field=box&name=JST").text
    assert 'value="JST"' in box and "<button>Создать" in box
    assert "<button>Создать" not in c.get("/boxes/new").text  # opened on its own: saves itself, no buttons


def test_project_needs():
    """A project lists what it needs; one line or «всё недостающее» goes «в пути», and the page says how much is there."""
    box = core.new_boxes(1)[0]
    c.post("/items/new?type=resistor", data={"dup_ok": "1", "name": "10к часы", "value": "10 кОм", "box": box, "qty": 3})
    a = newest()
    c.post("/items/new?type=resistor", data={"dup_ok": "1", "name": "1к часы", "value": "1 кОм", "box": box, "qty": 1})
    b = newest()
    c.post("/projects", data={"name": "Часы с нуждами"})
    pid = core.db().execute("SELECT id FROM projects WHERE name='Часы с нуждами'").fetchone()[0]
    assert "Нужно для проекта" in c.get(f"/i/{a}").text
    c.post(f"/i/{a}/need", data={"project": pid, "qty": 10})
    c.post(f"/i/{b}/need", data={"project": pid, "qty": 4})
    need = lambda: {r["item_id"]: (r["need"], r["have"], r["transit"], r["short"]) for r in core.project_needs(pid)}
    assert need() == {a: (10, 3, 0, 7), b: (4, 1, 0, 3)}

    c.post("/stock", data={"box": core.TRANSIT, "item": a, "action": "add", "kind": "buy", "qty": 5, "project": pid})
    assert need()[a] == (10, 3, 5, 2)
    assert core.db().execute("SELECT project_id FROM movements WHERE item_id=? AND box_id=?", (a, core.TRANSIT)
                             ).fetchone()[0] == pid  # ordered for this project: history says so
    page = c.get(f"/p/{pid}").text
    assert "Часы с нуждами" in page and "в пути 5" in page and 'min="1" value="2"' in page and f'href="/p/{pid}"' in c.get("/projects").text
    c.post(f"/p/{pid}/order")
    assert need() == {a: (10, 3, 7, 0), b: (4, 1, 3, 0)}  # all the rest ordered at once

    core.trash("item", a)
    assert list(need()) == [b]
    core.restore(core.trash_list()[0]["id"])
    assert set(need()) == {a, b}  # the need came back with the thing
    c.post(f"/i/{b}/need", data={"project": pid, "qty": 0})
    assert list(need()) == [a]


def test_label_sheet():
    """A batch of labels: one sheet at the label's size to print, and each label's NFC link to copy or write."""
    c = TestClient(app)
    ids = c.post("/boxes", data={"n": 3}, follow_redirects=False).headers["location"].split("=")[1].split(",")
    page = c.get("/places?new=" + ",".join(ids)).text
    assert f'data-nfcw="http://testserver/b/{ids[0].lower()}"' in page and "data-copy" in page
    sheet = c.get("/boxes/sheet?ids=" + ",".join(ids)).text
    assert sheet.count("label.png") == 3 and "width:25mm;height:15mm" in sheet
    assert c.get("/boxes/sheet?ids=../x").status_code == 404


def test_backup():
    """One button: the database and every upload in one zip, and the database in it opens."""
    c = TestClient(app)
    c.post("/items/new?type=resistor", data={"dup_ok": "1", "name": "Бэкапный", "value": "1 Ом"}, files=photo())
    assert "/backup" in c.get("/trash").text
    r = c.get("/backup")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert "attachment" in r.headers["content-disposition"]
    assert any(n.startswith("uploads/") for n in z.namelist())
    db = Path(tempfile.mkdtemp()) / "inventory.db"
    db.write_bytes(z.read("inventory.db"))
    assert sqlite3.connect(db).execute("SELECT 1 FROM items WHERE name='Бэкапный'").fetchone()


def test_reader():
    """Manifesto «Умная коробка»: a reader sends what it read; a box becomes its current one, a thing goes in it."""
    tap = lambda code, r="AA:01": c.post("/api/tap", json={"reader": r, "code": code})
    stock = lambda iid: {r[0]: r[1] for r in core.db().execute("SELECT box_id, qty FROM stock WHERE item_id=?", (iid,))}
    q = lambda sql, *a: core.db().execute(sql, a).fetchone()[0]
    a, b = core.new_boxes(2)
    r = tap(f"http://inv.lan/b/{a.lower()}")
    assert r.status_code == 403 and not r.json()["ok"]  # new: shows up on the page, does nothing till accepted
    assert "Новые считыватели" in c.get("/readers").text and "AA:01" in c.get("/readers").text
    c.post("/places", data={"name": "Шкаф со считывателем"})
    pid = q("SELECT id FROM places WHERE name='Шкаф со считывателем'")
    c.post("/readers", data={"id": "AA:01", "name": "Шкаф", "place": pid})
    c.post("/items/new?type=hand_tool", files=photo(), data={"name": "клещи", "box": b, "dup_ok": "1"})
    tool = newest()

    assert tap(f"HTTP://INV.LAN/I/{tool}").status_code == 409 and stock(tool) == {b: 1}  # no box yet: error beep
    r = tap(f"HTTP://INV.LAN/B/{a}")
    assert r.status_code == 200 and r.json()["box"] == a
    assert q("SELECT place_id FROM boxes WHERE id=?", a) == pid  # put in the cabinet: the cabinet is written
    r = tap(f"http://inv.lan/I/{tool}")
    assert r.status_code == 200 and stock(tool) == {a: 1}  # one of a kind: moved, not a second one
    assert q("SELECT author FROM movements WHERE item_id=? ORDER BY id DESC", tool) == "Шкаф"
    page = c.get("/readers").text
    assert "Новые считыватели" not in page and f'href="/b/{a}"' in page  # its current box
    assert tap("http://inv.lan/i/999999").status_code == 404 and tap("что-то чужое").status_code == 404

    def old():  # the last tap was 11 minutes ago
        with core.db() as d:
            d.execute("UPDATE readers SET tapped_at=datetime('now','localtime','-11 minutes') WHERE id='AA:01'")
    old()
    assert tap(f"http://inv.lan/I/{tool}").status_code == 200  # a stationary one never forgets
    c.post("/readers", data={"id": "AA:01", "name": "Шкаф", "place": pid, "portable": "1"})
    old()
    assert tap(f"http://inv.lan/I/{tool}").status_code == 409  # a portable one forgets after READER_FORGET_MIN
    c.post("/readers/delete", data={"id": "AA:01"})
    assert "AA:01" not in c.get("/readers").text


def test_settings():
    """№53: /settings beats .env — kept in DATA/settings.json, works at once, «Сбросить» brings .env back."""
    f = core.DATA / "settings.json"
    saved = lambda: json.loads(f.read_text())
    try:
        page = c.post("/settings", data={"TRASH_DAYS": "7"}).text
        assert core.TRASH_DAYS == 7 and saved() == {"TRASH_DAYS": "7"} and "изменено здесь" in page
        assert "здесь 7 дней" in c.get("/trash").text
        for bad in ({"TRASH_DAYS": "abc"}, {"TRASH_DAYS": "-1"}, {"PUBLIC_BASE_URL": "inv.lan"}, {"SCAN_DEFAULT": "x"}):
            assert c.post("/settings", data=bad).status_code == 400
        assert core.TRASH_DAYS == 7 and saved() == {"TRASH_DAYS": "7"}  # a bad value changes nothing
        assert c.post("/settings", data={"PUBLIC_BASE_URL": "HTTP://INV.LAN"}).status_code == 200  # capitals, as on a label
        c.post("/settings", data={"reset": "PUBLIC_BASE_URL"})
        c.post("/settings", data={"reset": "TRASH_DAYS"})
        assert core.TRASH_DAYS == 30 and saved() == {}
        c.post("/settings", data={"TRASH_DAYS": "30", "PHOTO_QUALITY": "90"})  # the whole form comes back unchanged
        assert saved() == {}

        c.post("/settings", data={"GITHUB_TOKEN": "ghp_secret123"})
        assert core.GITHUB_TOKEN == "ghp_secret123" and "ghp_secret123" not in c.get("/settings").text
        c.post("/settings", data={"GITHUB_TOKEN": ""})  # an empty field keeps the token
        assert core.GITHUB_TOKEN == "ghp_secret123"

        assert 'id="scan"' in c.get("/").text
        c.post("/settings", data={"SCAN_NFC": "0", "SCAN_QR": "0"})
        assert 'id="scan"' not in c.get("/").text and 'data-nfcw="' not in c.get(f"/i/{newest()}").text
    finally:
        core.save_settings(dict.fromkeys(saved() if f.exists() else [], None))
    r = c.get("/phone", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/settings#phone"
