"""The door to the internet: agents outside the LAN (ChatGPT, Claude on the web) reach /mcp through a Cloudflare
Tunnel, behind Cloudflare Access. See docs/outside.md.

The site has no login by design, so the gate never trusts the network a request came from. A request that came
through Cloudflare (it carries a Cf-* header, or its Host is OUTSIDE_HOST) is an outside one: it may open /mcp and
nothing else, and only with a token Access signed for this application. Everything else passes as before.
Each connector (a ChatGPT connector, Claude on the web) gets its own address /mcp/<key>; OUTSIDE_KEYS maps the keys to
names, so history says which one acted, and dropping a line cuts that connector off. Access can't tell them apart: it
hands the origin the same token whichever OAuth client logged in.
Fails closed: an outside request while CF_ACCESS_TEAM / CF_ACCESS_AUD are unset gets 403, with no OUTSIDE_KEYS 404.
"""
import contextvars
import hmac
import ipaddress
import json
import os
import socket
import urllib.request

import anyio
import jwt

OUTSIDE = contextvars.ContextVar("outside", default=None)  # (connector name, Access email) of an outside request
CF_HEADERS = (b"cf-ray", b"cf-connecting-ip", b"cf-access-jwt-assertion", b"cf-ipcountry")
_jwks = {}


def team():
    t = os.environ.get("CF_ACCESS_TEAM", "").strip().lower().removeprefix("https://").rstrip("/")
    return t if not t or "." in t else f"{t}.cloudflareaccess.com"  # «myteam» or «myteam.cloudflareaccess.com»


def connector(key):
    """The name OUTSIDE_KEYS («key:name,key:name») gives this key, or None."""
    name = None
    for pair in os.environ.get("OUTSIDE_KEYS", "").split(","):
        k, _, n = pair.strip().partition(":")
        if k and n.strip() and hmac.compare_digest(k.encode(), key.encode()):
            name = n.strip()
    return name


def is_outside(headers):
    host = os.environ.get("OUTSIDE_HOST", "").strip().lower()
    h = dict(headers)
    return any(k in h for k in CF_HEADERS) or bool(host) and h.get(b"host", b"").decode().split(":")[0].lower() == host


def verify(token):
    """Access JWT → its claims. Signature by the team's keys, audience = this application, issuer = the team."""
    t, aud = team(), os.environ.get("CF_ACCESS_AUD", "").strip()
    if not (t and aud):
        raise PermissionError("outside access is not set up: CF_ACCESS_TEAM and CF_ACCESS_AUD are empty (docs/outside.md)")
    if not token:
        raise PermissionError("no Cf-Access-Jwt-Assertion: put this hostname behind a Cloudflare Access application")
    keys = _jwks.setdefault(t, jwt.PyJWKClient(f"https://{t}/cdn-cgi/access/certs", cache_keys=True, lifespan=3600))
    try:
        key = keys.get_signing_key_from_jwt(token).key
        return jwt.decode(token, key, algorithms=["RS256"], audience=aud, issuer=f"https://{t}")
    except jwt.PyJWTError as e:
        raise PermissionError(f"bad Access token: {e}") from None


class Gate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not is_outside(scope["headers"]):
            return await self.app(scope, receive, send)
        path = scope["path"]
        if not path.startswith("/mcp/"):
            return await deny(send, 404, "only /mcp/<key> is open from outside")
        name = connector(path.removeprefix("/mcp/"))
        if not name:
            return await deny(send, 404, "unknown connector key: see OUTSIDE_KEYS in .env (docs/outside.md)")
        token = dict(scope["headers"]).get(b"cf-access-jwt-assertion", b"").decode()
        try:
            claims = await anyio.to_thread.run_sync(verify, token)
        except PermissionError as e:
            return await deny(send, 403, str(e))
        mark = OUTSIDE.set((name, claims.get("email") or claims.get("sub") or "?"))
        try:
            await self.app({**scope, "path": "/mcp", "raw_path": b"/mcp"}, receive, send)
        finally:
            OUTSIDE.reset(mark)


async def deny(send, status, text):
    body = json.dumps({"error": text}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


# Downloads an outside agent asks for (photos and files by URL) must not reach into the LAN: the router, the NAS,
# this very server. Every hop of a redirect is checked; LAN agents keep fetching from the LAN as before.
def public_only(url):
    host = urllib.request.urlparse(url).hostname
    if not host:
        raise PermissionError(f"no host in {url[:100]}")
    try:
        addrs = {a[4][0] for a in socket.getaddrinfo(host, None)}
    except OSError as e:
        raise PermissionError(f"can't resolve {host}: {e}") from None
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if not ip.is_global:
            raise PermissionError(f"{host} is a local address ({ip}): outside agents fetch from the internet only")


class PublicRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_only(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def opener(url):
    """urlopen for this request: guarded when the caller came from outside, plain otherwise."""
    if OUTSIDE.get() is None or url.lower().startswith("data:"):
        return urllib.request.urlopen
    public_only(url)
    return urllib.request.build_opener(PublicRedirects).open
