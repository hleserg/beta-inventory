# beta-inventory

A tiny self-hosted inventory for boxes of electronic parts.

Stick an NFC tag (plus a QR code) on a box. Tap it with your phone and the box
page opens on your LAN: what's inside, how many, a photo, a plain-language
spec sheet with pinout, and attached datasheets. Take a part for a project and
the history remembers where it went.

AI agents (Claude Code, Codex, any MCP client) can check stock, run an
inventory and write new part cards for you.

> **Status: v0 prototype.** Boxes, labels, cards, stock moves, search by
> words and by meaning, and projects work. The UI is Russian for now. The MCP
> server and GitHub sync are next.

## Run

```sh
git clone https://github.com/hleserg/beta-inventory && cd beta-inventory
docker compose up -d --build     # http://localhost:8000
```

Settings go in a `.env` file next to `compose.yaml`:

| Variable | Default | What it does |
|---|---|---|
| `PORT` | `8000` | Port on the host |
| `PUBLIC_BASE_URL` | the URL you opened | Address printed into label QR codes, e.g. `HTTP://INV.LAN` (uppercase keeps the QR small) |
| `TZ` | `UTC` | Time zone for the history, POSIX form such as `MSK-3` |
| `LABEL_W_MM`, `LABEL_H_MM`, `LABEL_DPI` | `25`, `15`, `300` | Label image size |
| `SEMANTIC_MODEL` | multilingual MiniLM-L12 | Local model for search by meaning (fastembed); empty turns it off |
| `SEMANTIC_MIN` | `0.35` | How close in meaning an item must be to show up |
| `PIP_INDEX_URL` | PyPI | Package mirror for the build |

Data (SQLite, photos, files, the search model) lives in `./data`. On first
start the search model (~470 MB) downloads there; until it is ready, search
works by words only.

## Profiles: what a card looks like

Card fields are not in the code. They come from a YAML profile:
[`profiles/default.yaml`](profiles/default.yaml) is set up for electronics.
To use your own, copy it to `data/profile.yaml` (or set `PROFILE`) and edit.

Items have a two-level type: a category (Electronics) and a type inside it
(MCU module, resistor, connector…). A card gets the common fields, then the
category's, then the type's; a deeper level can override a field, e.g. a
resistor makes the photo optional and adds a required value, while only
modules require a pinout. Each field can be required and can carry a search
weight.

## What works

- **Boxes** with printable labels (QR + short ID); boxes nest and stand in
  places. An empty box asks what goes in.
- **Items** with photo, markdown description, files and per-type fields.
  Quantity lives on the box + item pair.
- **Take / return / restock / recount / empty box** — every change is a
  movement with author and project.
- **Search** by name, other names, description and typed fields, ranked by
  relevance; box IDs and places too. Below the word matches, a small local
  model adds items close in meaning, so "step-down" finds a buck converter.
- **Projects** with a git link, to charge takes against.

## Planned

- **MCP server** so agents can read stock and write cards, plus a card-writing
  skill.
- Projects synced from a GitHub account into an inbox.
- Markdown editor with toolbar, batch label printing, English UI.

## Non-goals

- No login, no HTTPS, no multi-user. It is meant for a home LAN. If you expose
  it, put it behind a reverse proxy with auth.
- Not an ERP: no suppliers, prices, BOMs or purchase orders.

## License

GPL-3.0 — see [LICENSE](LICENSE).
