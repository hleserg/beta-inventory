"""One walk through the main path: box → card → search → take → clear."""
import io
import json
import re

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
    assert "MP1584" in c.get("/?q=ПОНИЖАЙКА").text
    assert "10k" in c.get("/?q=резистор").text  # type label is searchable
    assert "10k" in c.get("/items?cat=electronics").text and "10k" not in c.get("/items?cat=tools").text

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
    assert "data-draft" in c.get("/items/new?type=module").text  # unsaved card edits survive leaving the page
    box = core.new_boxes(1)[0]
    new = c.get(f"/items/new?type=module&box={box}").text
    assert f'value="{box}"' in new and f'<option value="{box}">' in new  # opened from a box: that box, its name shown
    assert "serviceWorker" in c.get("/").text and "/offline" in c.get("/sw.js").text
    assert c.get("/offline").status_code == 200
    box = core.new_boxes(1)[0]
    page = c.get(f"/b/{box}").text
    assert "NDEFReader" in page and f"http://testserver/b/{box.lower()}" in page
    phone = c.get("/phone").text
    assert "chrome://flags/#unsafely-treat-insecure-origin-as-secure" in phone and "http://testserver" in phone
    assert "apps.apple.com/app/nfc-tools/id1252962749" in phone


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
    assert "есть, не считал" in c.get(f"/b/{box}").text  # a handful more, uncounted
    c.post("/stock", data={"box": box, "item": rid, "action": "add", "kind": "return", "qty": 2})
    assert "2 шт" in c.get(f"/b/{box}").text  # «вернул 2» to an uncounted pair: now there are 2 (№34)
    c.post(f"/b/{box}/clear")
    assert "Что кладём" in c.get(f"/b/{box}").text


def test_box_by_name():
    """A box field takes what people know: part of the name in any case, «Name (ID)» from the list, or the ID."""
    a, b, d = core.new_boxes(3)
    c.post(f"/b/{a}", data={"name": "Клеммники Phoenix"})
    c.post(f"/b/{b}", data={"name": "Клеммники WAGO"})
    for typed in ("phoenix", "КЛЕММНИКИ phoenix", f"Клеммники Phoenix ({a})", a.lower()):
        r = c.post("/items/new?type=resistor", follow_redirects=False,
                   data={"name": "220R", "value": "220 Ом", "box": typed, "qty": "1"})
        assert r.headers["location"] == f"/b/{a}", typed
    r = c.post("/items/new?type=resistor", data={"name": "220R", "value": "220 Ом", "box": "клеммники", "qty": "1"})
    assert r.status_code == 400 and "Клеммники WAGO" in r.text and 'value="клеммники"' in r.text  # which one? typed text kept

    assert c.post("/stock", data={"box": "wago", "item": newest(), "action": "add", "kind": "put", "qty": 2}).status_code == 200
    assert "2 шт" in c.get(f"/b/{b}").text
    assert c.post("/stock", data={"box": "нет такой", "item": newest(), "action": "add", "qty": 2}).status_code == 400
    c.post(f"/b/{d}", data={"name": "Ящик", "parent_id": "wago"})
    assert core.db().execute("SELECT parent_id FROM boxes WHERE id=?", (d,)).fetchone()[0] == b

    page = c.get(f"/b/{d}").text  # names first, the ID last; scan buttons next to the field
    assert f'value="Клеммники WAGO ({b})"' in page and "data-nfc" in page and "data-qr" in page
    assert f'<option value="Клеммники Phoenix ({a})">' in c.get("/items/new?type=module").text
    assert f'<option value="Клеммники Phoenix ({a})">' in c.get(f"/i/{newest()}").text

    named = f'<b>Клеммники WAGO</b> <span class="mut">{b}</span>'  # the name first, the ID grey at the end
    for url in (f"/i/{newest()}", "/boxes", "/history", "/?q=wago"):
        assert named in c.get(url).text, url
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
    assert r.status_code == 200 and count() == before + 1 and "Макетки" in c.get("/boxes").text
    assert c.post("/b/NOPE!", data={"name": "x"}).status_code == 404  # only IDs the site hands out

    c.post("/places", data={"name": "Стеллаж"})
    pid = core.db().execute("SELECT id FROM places WHERE name='Стеллаж'").fetchone()[0]
    r = c.post(f"/places/{pid}/shelves", data={"name": "Верхняя"}, follow_redirects=False)
    assert r.headers["location"].startswith("/places")  # no label: stay among the places
    r = c.post(f"/places/{pid}/shelves", data={"label": "1"}, follow_redirects=False)
    shelf = r.headers["location"].split("/")[-1]  # with a label: its page, to print and write the tag
    page = c.get(f"/b/{shelf}").text
    assert "Полка 2" in page and "label.png" in page and "Стоит внутри другой" not in page  # shelves do not move
    boxes = c.get("/boxes").text
    assert "Верхняя" not in boxes and "Полка 2" not in boxes  # shelves live in places, not among boxes

    c.post(f"/b/{bid}", data={"name": "Макетки", "parent_id": shelf}, headers={"X-Autosave": "1"})
    places = c.get("/places").text
    assert places.index("Стеллаж") < places.index("Верхняя") < places.index("Полка 2") < places.index("Макетки")
    assert f'<option value="Стеллаж › Полка 2 ({shelf})">' in c.get(f"/b/{bid}").text  # «Полка 2» is in every cabinet


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


def test_old_single_photo():
    """Cards from before several photos held one name, not a list."""
    iid = core.save_item(None, "старое", "module", {"photo": "x.jpg", "pinout": "A"})
    core.init()
    assert core.item_fields(core.db().execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone())["photo"] == ["x.jpg"]
