"""Agents see the same inventory as the site, through MCP."""
import asyncio
import io
import json
import os

from fastapi.testclient import TestClient
from mcp import Client

from inventory import core, mcp_server
from inventory.app import app
from inventory.mcp_server import server
from test_flow import newest, photo


def call(tool, **args):
    async def go():
        async with Client(server) as cl:
            return await cl.call_tool(tool, args)
    return asyncio.run(go())


def test_read_tools():
    c = TestClient(app)
    box = core.new_boxes(1)[0]
    c.post("/items/new?type=resistor", follow_redirects=False,
           data={"dup_ok": "1", "name": "220R", "aliases": "токоограничительный", "value": "220 Ом", "box": box, "qty": "7"})
    iid = newest()

    hit = call("search", query="токоограничительный").structured_content["items"][0]
    assert hit["id"] == iid and hit["stock"][0]["qty"] == 7
    card = call("get_item", item_id=iid).structured_content
    assert card["fields"]["value"] == "220 Ом" and card["stock"][0]["box_id"] == box
    assert card["unit"] == hit["unit"] == "шт"  # what qty counts: the card's, its type's or the profile's unit
    assert call("get_box", box_id=box.lower()).structured_content["contents"][0]["qty"] == 7  # label id, any case
    err = call("get_box", box_id="ZZZZZ")
    assert err.is_error and "search" in err.content[0].text  # the error says what to do next
    assert any(f["key"] == "value" for f in call("card_template", type="resistor").structured_content["fields"])
    types = call("card_template").structured_content["categories"]
    assert "resistor" in [t["key"] for cat in types for t in cat["types"]]


def test_http():
    with TestClient(app) as c:  # the lifespan starts the MCP session manager
        r = c.post("/mcp", headers={"Accept": "application/json, text/event-stream", "Host": "inv.lan"},
                   json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                       "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})
        assert r.status_code == 200 and "inventory" in r.text


def test_stock_tools():
    c = TestClient(app)
    box = core.new_boxes(1)[0]
    c.post("/items/new?type=resistor", follow_redirects=False, data={"dup_ok": "1", "name": "1k", "value": "1 кОм", "box": box, "qty": "7"})
    iid = newest()
    c.post("/projects", data={"name": "Часы"})
    pid = next(p["id"] for p in call("list_projects").structured_content["projects"] if p["name"] == "Часы")

    r = call("change_stock", box_id=box.lower(), item_id=iid, action="take", qty=2, agent="Мара", project_id=pid)
    assert r.structured_content["qty"] == 5
    last = call("get_item", item_id=iid).structured_content["history"][0]
    assert last["author"] == "Мара" and last["project"] == "Часы" and last["delta"] == -2
    err = call("change_stock", box_id=box, item_id=iid, action="take", qty=99, agent="Мара")
    assert err.is_error and "только 5" in err.content[0].text  # core's own words reach the agent
    err = call("change_stock", box_id=box, item_id=iid, action="take", qty=1, agent="Мара", project_id=9999)
    assert err.is_error and "list_projects" in err.content[0].text
    back = call("change_stock", box_id=box, item_id=iid, action="put", qty=None, agent="Мара")
    assert back.structured_content["qty"] == 7  # no number, the 2 taken are in hand (№28): they go back
    loose = call("change_stock", box_id=box, item_id=iid, action="put", qty=None, agent="Мара")
    assert loose.structured_content["qty"] is None  # nothing in hand: a handful more, uncounted: «есть, не считал»
    assert call("change_stock", box_id=box, item_id=iid, action="count", qty=10, agent="Мара"
                ).structured_content["qty"] == 10


def test_card_tools(monkeypatch):
    got = []
    monkeypatch.setattr(mcp_server, "fetch", lambda url: got.append(url) or photo()["photo"][1])
    box = core.new_boxes(1)[0]
    card = call("create_item", type="module", name="INA219", agent="Claude", box_id=box, qty=2, fields={
        "photo": ["https://example.com/ina.png"], "pinout": "VCC GND SCL SDA", "aliases": "датчик тока",
        "reorder_at": "1,5", "files": [{"name": "datasheet.pdf", "url": "https://example.com/ina.pdf"}]}
    ).structured_content
    assert card["stock"][0]["qty"] == 2 and card["history"][0]["author"] == "Claude"
    assert card["fields"]["photo"][0].endswith(".jpg") and card["fields"]["reorder_at"] == 1.5
    assert "INA219" in call("search", query="датчик тока").structured_content["items"][0]["name"]

    n = len(os.listdir(core.UPLOADS))  # the card goes back as get_item gave it: own /u/ links are not fetched again
    new = call("update_item", item_id=card["id"], fields=dict(card["fields"], description="Шунт 0,1 Ом")
               ).structured_content
    assert new["fields"]["photo"] == card["fields"]["photo"] and new["fields"]["files"] == card["fields"]["files"]
    assert new["fields"]["description"] == "Шунт 0,1 Ом" and len(os.listdir(core.UPLOADS)) == n and len(got) == 2

    err = call("update_item", item_id=card["id"], fields={"colour": "red"})
    assert err.is_error and "card_template" in err.content[0].text
    assert call("update_item", item_id=card["id"], fields={"photo": "../inventory.db"}).is_error  # only own uploads
    err = call("create_item", type="module", name="X", agent="Claude", fields={"pinout": "A B"})
    assert err.is_error and "Фото: обязательно" in err.content[0].text


def test_github_inbox():
    """New repos wait in «Новые из GitHub» until a person or an agent takes them, in their own words, or skips."""
    c = TestClient(app)
    c.post("/projects", data={"name": "Часы", "git_url": "https://github.com/me/Clock"})
    repo = lambda name, fork=False: {"name": name, "description": f"{name} repo", "fork": fork,
                                     "html_url": f"https://github.com/me/{name}"}
    assert core.sync_projects([repo("clock.git"), repo("Clock"), repo("upstream", fork=True), repo("feeder"),
                               repo("dotfiles"), repo("lamp")]) == 3  # «Часы» are known: .git and case aside
    assert core.sync_projects([repo("feeder")]) == 0  # hourly: known repos stay as they are
    with core.db() as db:
        assert "feeder" not in [p["name"] for p in core.projects(db)]  # not offered for «забрать» yet
        inbox = {p["name"]: p["id"] for p in db.execute("SELECT * FROM projects WHERE status='inbox'")}
    assert "Новые из GitHub" in c.get("/projects").text
    c.post(f"/projects/{inbox['dotfiles']}", data={"name": "dotfiles", "git_url": "https://github.com/me/dotfiles",
                                                   "status": "skipped"})

    assert [p["name"] for p in call("list_projects").structured_content["inbox"]] == ["feeder", "lamp"]
    call("accept_project", git_url="https://github.com/me/feeder.git", name="Страж миски", description="Кошка не ест")
    call("accept_project", git_url="https://github.com/me/private", name="Голова", description="")  # not listed
    call("skip_project", git_url="https://github.com/me/lamp")
    with core.db() as db:
        assert {"Голова", "Страж миски", "Часы"} <= {p["name"] for p in core.projects(db)}
        assert "lamp" not in {p["name"] for p in core.projects(db)}
    assert call("list_projects").structured_content["inbox"] == []
    assert call("skip_project", git_url="https://github.com/me/nope").is_error


def test_fetch_repos(monkeypatch):
    """All pages, and with a token the account's own list (private repos too)."""
    asked = []

    def urlopen(req, timeout):
        asked.append(req)
        return io.BytesIO(json.dumps([{"name": "r"}] * (100 if len(asked) == 1 else 3)).encode())
    monkeypatch.setattr(core.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(core, "GITHUB_TOKEN", "t")
    assert len(core.fetch_repos()) == 103
    assert asked[1].full_url.endswith("page=2") and "/user/repos" in asked[0].full_url
    assert asked[0].get_header("Authorization") == "Bearer t"
