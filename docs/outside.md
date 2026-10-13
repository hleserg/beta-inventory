# Agents from the internet: ChatGPT, Claude on the web

The site stays LAN-only and has no login. For agents outside the LAN there is
one door: a Cloudflare Tunnel to `/mcp`, locked by Cloudflare Access. Through
it an agent gets the MCP tools and nothing else: no pages, no `/backup`, no
`/settings`, no uploads.

```
ChatGPT ──OAuth──▶ Cloudflare Access ──▶ Tunnel ──▶ cloudflared container ──▶ inventory:8000/mcp
                   (your login, your rule)          (dials out, no open port)   (checks the Access token again)
```

What the server does with an outside request (`inventory/gate.py`):

- A request is "outside" when it carries a `Cf-*` header or its Host is
  `OUTSIDE_HOST`. LAN requests never do, so the LAN works as before.
- An outside request may open `/mcp` only; anything else is 404.
- It must carry `Cf-Access-Jwt-Assertion` signed by your team for this
  application (`CF_ACCESS_AUD`), or it gets 403. With `CF_ACCESS_*` unset,
  every outside request gets 403: a half-made setup stays shut.
- Photos and files an outside agent gives by URL are downloaded from public
  addresses only, every redirect hop checked: an agent fooled by a page it read
  can't make the server fetch your router, NAS or this very site.
- History names the author as `ChatGPT (извне, you@example.com)`: the agent's
  name plus whose Access login it came through.

You need a domain on Cloudflare (free plan is enough) and a Zero Trust
organization (free for a few users). Dashboard names below are as of
October 2026; Cloudflare moves things around, so look for the nearest match.

## 1. Tunnel

Zero Trust → **Networks → Tunnels → Create a tunnel** → Cloudflared. Name it,
pick Docker, copy the token from the command it shows (the long string after
`--token`). Don't run that command: the compose file runs cloudflared for you.

In `.env`:

```sh
COMPOSE_PROFILES=tunnel
TUNNEL_TOKEN=eyJh...
```

Then the tunnel's **Public hostname** (or **Published application routes**):

| Field | Value |
|---|---|
| Subdomain + domain | `sklad` + `example.com` |
| Path | `^/mcp` |
| Service | `HTTP` → `inventory:8000` |

`inventory:8000` is the site's name inside the compose network. Already run
cloudflared elsewhere in the LAN (say, next to n8n)? Skip the token, add this
hostname to that tunnel with service `http://<server IP>:8000` instead.

The path rule is the first fence; `gate.py` is the second, so a mistake in one
doesn't open the site.

## 2. Access application

Zero Trust → **Access → Applications → Add an application → Self-hosted**.

- **Domain:** `sklad.example.com`, path empty: the whole hostname, so Access
  also answers the OAuth discovery under `/.well-known/`.
- **Policy:** Allow → Include → Emails → your email. Only you.
- **Login methods:** One-time PIN (a code to your email) works out of the box;
  Google or GitHub if you have them set up.
- **Advanced settings → Managed OAuth:** on. Dynamic client registration: on.
  Allowed redirect URIs:
  `https://chatgpt.com/*` and `https://claude.ai/*`.
  Access token lifetime 15 minutes, grant session 1–2 weeks (Cloudflare's own
  advice for agents).

Save, open the application again and copy its **Application Audience (AUD)
tag**. Your team name is in **Settings → Team name and domain**
(`myteam.cloudflareaccess.com`).

In `.env`:

```sh
OUTSIDE_HOST=sklad.example.com
CF_ACCESS_TEAM=myteam
CF_ACCESS_AUD=4714c1358e65fe4b408ad6d432a5f878f08194bdb4752441fd56faefa9b2b6f2
```

Apply: `git pull && ./install.sh` (or `docker compose up -d`).
`docker compose ps` shows two containers, `inventory` and `tunnel`.

## 3. Check from outside

From a phone on mobile data, or any machine outside your LAN:

```sh
curl -i https://sklad.example.com/mcp      # 401 + WWW-Authenticate: Access wants a login
curl -i https://sklad.example.com/backup   # 404 or the Access login page, never a zip
```

## 4. Connect the agents

**ChatGPT:** Settings → **Apps & Connectors → Advanced settings → Developer
mode** on, then **Create**: URL `https://sklad.example.com/mcp`, auth OAuth.
A browser window opens with the Access login; after it the tools appear.
Developer mode is what allows write tools (`change_stock`, `create_item`) for a
custom connector; OpenAI labels it Elevated Risk for good reason: see below.

**Claude (claude.ai, the apps):** Settings → **Connectors → Add custom
connector** → the same URL.

**LAN agents** (Claude Code, Codex on your machines) keep using
`http://inv.lan/mcp` with no login.

If the client shows its OAuth callback URL and it isn't covered by the
allowed redirect URIs above, add it there.

## What stays on you

- **Prompt injection.** An agent that reads web pages can be told by one of
  them to change your stock. Every change is in history with its author,
  and `update_item` is marked destructive so clients ask before it, but read
  what you approve.
- **Photo links** in tool results point to `PUBLIC_BASE_URL`, your LAN
  address: an outside agent sees them, but can't open them.
- **DNS rebinding.** The download guard resolves a name, checks the
  addresses, then downloads; a name that changes its answer between the two
  could slip through. It needs a hostile DNS server and luck; the Access login
  in front makes it someone you let in.
- **Keys.** Lost the phone you log in with? Zero Trust → **My Team → Users**
  → revoke the session.
