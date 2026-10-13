"""The door to the internet: agents outside the LAN (ChatGPT, Claude on the web) reach /mcp through a Cloudflare
Tunnel, behind Cloudflare Access. See docs/outside.md.

The site has no login by design, so the gate never trusts the network a request came from. A request that came
through Cloudflare (it carries a Cf-* header, or its Host is OUTSIDE_HOST) is an outside one: it may open /mcp and
nothing else, and only with a token Access signed for this application. Everything else passes as before.
Fails closed: an outside request while CF_ACCESS_TEAM / CF_ACCESS_AUD are unset gets 403.
"""
import contextvars
import ipaddress
import json
import os
import socket
import urllib.request

import anyio
import jwt

OUTSIDE = contextvars.ContextVar("outside", default=None)  # the Access identity (email) of an outside request
CF_HEADERS = (b"cf-ray", b"cf-connecting-ip", b"cf-access-jwt-assertion", b"cf-ipcountry")
_jwks = {}


def team():
    t = os.environ.get("CF_ACCESS_TEAM", "").strip().lower().removeprefix("https://").rstrip("/")
    return t if not t or "." in t else f"{t}.cloudflareaccess.com"  # «myteam» or «myteam.cloudflareaccess.com»


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
        if path != "/mcp" and not path.startswith("/mcp/"):
            return await deny(send, 404, "only /mcp is open from outside")
        token = dict(scope["headers"]).get(b"cf-access-jwt-assertion", b"").decode()
        try:
            claims = await anyio.to_thread.run_sync(verify, token)
        except PermissionError as e:
            return await deny(send, 403, str(e))
        mark = OUTSIDE.set(claims.get("email") or claims.get("sub") or "?")
        try:
            await self.app(scope, receive, send)
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
