"""One walk through the main path: box → card → search → take → clear."""
import io

from fastapi.testclient import TestClient
from PIL import Image

from inventory import core
from inventory.app import app

c = TestClient(app)


def photo():
    b = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(b, "PNG")
    return {"photo": ("p.png", b.getvalue(), "image/png")}


def test_main_path():
    assert core.PROFILE["terms"]["items"] in c.get("/items").text  # t.items once rendered dict.items
    assert c.post("/places", data={"name": "Шкаф"}).status_code == 200
    box = c.post("/boxes", data={"n": 1}, follow_redirects=False).headers["location"].split("=")[1]
    assert f"http://testserver/b/{box.lower()}" in c.get(f"/B/{box.lower()}").text  # QR URL, any case; NFC link
    assert c.get(f"/b/{box}/label.png").headers["content-type"] == "image/png"

    # module: photo and pinout required; resistor: photo optional (type overrides the common field)
    assert c.post("/items/new?type=module", data={"name": "MP1584"}).status_code == 400
    assert c.post("/items/new?type=resistor", data={"name": "10k", "value": "10 кОм"},
                  follow_redirects=False).status_code == 303
    r = c.post("/items/new?type=module", files=photo(), follow_redirects=False,
               data={"name": "MP1584 mini buck", "aliases": "понижайка", "pinout": "IN+ IN- OUT+ OUT-",
                     "box": box, "qty": "5"})
    assert r.status_code == 303
    iid = r.headers["location"].rsplit("/", 1)[1]
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
    assert c.get("/manifest.webmanifest").json()["display"] == "standalone"
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
