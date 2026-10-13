"""Agents from the internet: /mcp only, only with a Cloudflare Access token, no downloads from the LAN."""
import json
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from inventory import core, gate

TEAM, AUD = "myteam.cloudflareaccess.com", "aud-tag-123"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
ACCEPT = {"Accept": "application/json, text/event-stream"}
SECRET = "k3f9Qx-_7bq2zzA1b2c3d4e5f6g7h8i9"
MCP = f"/mcp/{SECRET}"  # each connector has its own address, the key in it names the connector


def token(aud=AUD, iss=f"https://{TEAM}", key=KEY, **extra):
    claims = {"aud": [aud], "iss": iss, "email": "sergey@example.com", "exp": int(time.time()) + 600, **extra}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture
def access(monkeypatch):
    """Access set up for this app, the team's keys served from memory instead of /cdn-cgi/access/certs."""
    monkeypatch.setenv("CF_ACCESS_TEAM", "myteam")
    monkeypatch.setenv("CF_ACCESS_AUD", AUD)
    monkeypatch.setenv("OUTSIDE_KEYS", f"{SECRET}:chatgpt-дом, other-key-000000000000000000:claude")
    monkeypatch.setitem(gate._jwks, TEAM, SimpleNamespace(
        get_signing_key_from_jwt=lambda t: SimpleNamespace(key=KEY.public_key())))


def outside(c, path=MCP, body=INIT, tok=None, **headers):
    h = {**ACCEPT, "Host": "sklad.example.com", "Cf-Ray": "8f00-WAW", **headers}
    if tok:
        h["Cf-Access-Jwt-Assertion"] = tok
    return c.post(path, headers=h, json=body) if path.startswith("/mcp") else c.get(path, headers=h)


def tool(c, tool_name, **args):
    r = outside(c, body={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool_name, "arguments": args}},
                tok=token())
    assert r.status_code == 200, r.text
    return r.json()["result"]


def test_lan_unchanged(live):
    c = live
    assert c.get("/backup").status_code == 200  # the LAN keeps the whole site, no login
    assert c.post("/mcp", headers={**ACCEPT, "Host": "inv.lan"}, json=INIT).status_code == 200


def test_outside_sees_mcp_only(live, access):
    c = live
    for path in ("/", "/backup", "/settings", "/u/x.jpg", "/mcpx", "/mcp", "/mcp/", "/mcp/wrong-key", f"{MCP}/x", f"/mcp/{SECRET[:-1]}"):
        assert outside(c, path, tok=token()).status_code == 404, path


def test_fails_closed(live, access, monkeypatch):
    c = live
    monkeypatch.delenv("CF_ACCESS_TEAM")
    r = outside(c, tok=token())
    assert r.status_code == 403 and "CF_ACCESS" in r.json()["error"]
    monkeypatch.setenv("CF_ACCESS_TEAM", "myteam")
    monkeypatch.delenv("OUTSIDE_KEYS")  # no connector registered: no address works
    r = outside(c, tok=token())
    assert r.status_code == 404 and "OUTSIDE_KEYS" in r.json()["error"]


def test_token_checked(live, access):
    c = live
    assert outside(c).status_code == 403  # Access skipped (the hostname is not behind an application)
    assert outside(c, tok=token(aud="other-app")).status_code == 403
    assert outside(c, tok=token(iss="https://evil.cloudflareaccess.com")).status_code == 403
    assert outside(c, tok=token(exp=int(time.time()) - 10)).status_code == 403
    alien = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert outside(c, tok=token(key=alien)).status_code == 403
    r = outside(c, tok=token())
    assert r.status_code == 200 and "inventory" in r.text


def test_outside_by_host(live, access, monkeypatch):
    """Even if a tunnel strips the Cf-* headers, the public hostname alone makes a request an outside one."""
    monkeypatch.setenv("OUTSIDE_HOST", "sklad.example.com")
    c = live
    assert c.get("/backup", headers={"Host": "sklad.example.com:443"}).status_code == 404


def test_outside_agent(live, access):
    c = live
    box = core.new_boxes(1)[0]
    res = tool(c, "create_item", type="resistor", name="47R", agent="ChatGPT", box_id=box, qty=3,
               fields={"value": "47 Ом"})
    iid = res["structuredContent"]["id"]
    last = tool(c, "get_item", item_id=iid)["structuredContent"]["history"][0]
    assert "ChatGPT (извне: chatgpt-дом, sergey@example.com)" in json.dumps(last, ensure_ascii=False)

    for url in ("http://127.0.0.1:8000/backup", "http://192.168.1.1/", "http://localhost/x.png", "http://[::1]/x"):
        err = tool(c, "update_item", item_id=iid, fields={"photo": [url]})
        assert err["isError"] and "local address" in err["content"][0]["text"], url


def test_redirect_into_lan_blocked():
    gate.public_only("http://93.184.215.14/x.png")  # a public address passes
    with pytest.raises(PermissionError):
        gate.PublicRedirects().redirect_request(None, None, 302, "", {}, "http://10.0.0.5/admin")
