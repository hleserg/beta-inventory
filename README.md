# beta-inventory

A tiny self-hosted inventory for boxes of electronic parts.

Stick an NFC tag (plus a QR code) on a box. Tap it with your phone and the box
page opens on your LAN: what's inside, how many, a photo, a plain-language
spec sheet with pinout, and attached datasheets. Take a part for a project and
the history remembers where it went.

AI agents (Claude Code, Codex, any MCP client) can check stock, run an
inventory and write new part cards for you.

> **Status: design.** Nothing to run yet. The design spec will land in
> `docs/superpowers/specs/`.

## Planned

- **Boxes** with NFC/QR labels; boxes can hold items and other boxes.
- **Items** (part types): photo, markdown description, pinout, attachments.
  Quantity lives on the box + item pair.
- **Projects** synced from a GitHub account; new repos land in an inbox to
  accept or ignore.
- **Take / return / restock** with a movement log per project.
- **Label generator**: new box IDs and a printable label image (QR + ID) sized
  for small thermal label printers.
- **Mobile-first editor** for cards, camera upload included.
- **MCP server** so agents can read stock and edit cards.
- One Docker container, SQLite. UI in English and Russian.

## Non-goals

- No login, no HTTPS, no multi-user. It is meant for a home LAN. If you expose
  it, put it behind a reverse proxy with auth.
- Not an ERP: no suppliers, prices, BOMs or purchase orders.

## License

GPL-3.0 — see [LICENSE](LICENSE).
