"""Agents see the same inventory as the site, through MCP."""
import asyncio

from fastapi.testclient import TestClient
from mcp import Client

from inventory import core
from inventory.app import app
from inventory.mcp_server import server


def call(tool, **args):
    async def go():
        async with Client(server) as cl:
            return await cl.call_tool(tool, args)
    return asyncio.run(go())


def test_read_tools():
    c = TestClient(app)
    box = core.new_boxes(1)[0]
    r = c.post("/items/new?type=resistor", follow_redirects=False,
               data={"name": "220R", "aliases": "токоограничительный", "value": "220 Ом", "box": box, "qty": "7"})
    iid = int(r.headers["location"].rsplit("/", 1)[1])

    hit = call("search", query="токоограничительный").structured_content["items"][0]
    assert hit["id"] == iid and hit["stock"][0]["qty"] == 7
    card = call("get_item", item_id=iid).structured_content
    assert card["fields"]["value"] == "220 Ом" and card["stock"][0]["box_id"] == box
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
    r = c.post("/items/new?type=resistor", follow_redirects=False, data={"name": "1k", "value": "1 кОм", "box": box, "qty": "7"})
    iid = int(r.headers["location"].rsplit("/", 1)[1])
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
    assert call("change_stock", box_id=box, item_id=iid, action="count", qty=10, agent="Мара"
                ).structured_content["qty"] == 10
