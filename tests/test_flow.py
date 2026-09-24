"""One walk through the main path: box → card → search → take → clear."""
import io
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()  # before the app import: it opens the DB on import

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from inventory.app import app  # noqa: E402

c = TestClient(app)


def photo():
    b = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(b, "PNG")
    return {"photo": ("p.png", b.getvalue(), "image/png")}


def test_main_path():
    assert c.post("/places", data={"name": "Шкаф"}).status_code == 200
    box = c.post("/boxes", data={"n": 1}, follow_redirects=False).headers["location"].split("=")[1]
    assert c.get(f"/B/{box.lower()}").status_code == 200  # QR URL, any case
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

    c.post("/projects", data={"name": "Метеостанция"})
    assert c.post("/stock", data={"box": box, "item": iid, "action": "take", "qty": 2, "project": 1}).status_code == 200
    assert "3 шт" in c.get(f"/b/{box}").text
    assert c.post("/stock", data={"box": box, "item": iid, "action": "take", "qty": 99}).status_code == 400

    c.post(f"/b/{box}/clear")
    assert "Что кладём" in c.get(f"/b/{box}").text
    assert "Метеостанция" in c.get("/history").text
