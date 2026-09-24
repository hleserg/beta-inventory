"""One walk through the main path: box → card → search → take → clear."""
import io
import json

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
    c.post(f"/b/{box}", data={"name": "JST", "place": "Антресоль"})  # a new place right from the box
    assert 'value="Антресоль"' in c.get(f"/b/{box}").text and "Антресоль" in c.get("/places").text

    # module: photo and pinout required; resistor: photo optional (type overrides the common field)
    assert c.post("/items/new?type=module", data={"name": "MP1584"}).status_code == 400
    assert c.post("/items/new?type=resistor", data={"name": "10k", "value": "10 кОм"},
                  follow_redirects=False).status_code == 303
    r = c.post("/items/new?type=module", files=photo(), follow_redirects=False,
               data={"name": "MP1584 mini buck", "aliases": "понижайка", "pinout": "IN+ IN- OUT+ OUT-",
                     "box": box, "qty": "5"})
    assert r.headers["location"] == f"/b/{box}"  # put in a box: back to the box, the next item goes in
    iid = newest()
    pic = json.loads(core.db().execute("SELECT fields FROM items WHERE id=?", (iid,)).fetchone()["fields"])["photo"]
    r = c.post(f"/i/{iid}/edit?type=module", follow_redirects=False, data={
        "name": "MP1584 mini buck", "aliases": "понижайка", "pinout": "IN+ IN- OUT+ OUT-", "photo__keep": pic,
        "description": "до 3 А"})
    assert r.status_code == 303 and "до 3 А" in c.get(f"/i/{iid}").text and pic in c.get(f"/i/{iid}").text
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


def test_old_stock_table_migrates(tmp_path, monkeypatch):
    """A database from before «не считал» keeps its stock and takes qty NULL after a restart."""
    monkeypatch.setattr(core, "DATA", tmp_path)
    old = core.SCHEMA.replace("qty INTEGER CHECK (qty IS NULL OR qty > 0)", "qty INTEGER NOT NULL CHECK (qty > 0)")
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
