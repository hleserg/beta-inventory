"""Storage, profile, search and stock movements. Shared by the web pages and (later) MCP."""
import json
import os
import secrets
import sqlite3
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("DATA_DIR", ROOT / "data"))
UPLOADS = DATA / "uploads"
ID_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # no 0/O/1/I

SCHEMA = """
CREATE TABLE IF NOT EXISTS places(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, note TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS boxes(
  id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
  place_id INTEGER REFERENCES places(id) ON DELETE SET NULL,
  parent_id TEXT REFERENCES boxes(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL DEFAULT '', fields TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS stock(
  box_id TEXT NOT NULL REFERENCES boxes(id), item_id INTEGER NOT NULL REFERENCES items(id),
  qty INTEGER NOT NULL CHECK (qty > 0),
  updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  PRIMARY KEY (box_id, item_id));
CREATE TABLE IF NOT EXISTS projects(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  git_url TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active');
CREATE TABLE IF NOT EXISTS movements(
  id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id), box_id TEXT NOT NULL,
  delta INTEGER NOT NULL, kind TEXT NOT NULL, project_id INTEGER REFERENCES projects(id),
  author TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
  at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
"""

KINDS = {"put": "положил", "take": "забрал", "return": "вернул", "buy": "докупил",
         "count": "инвентаризация", "clear": "освободил"}


def load_profile():
    p = os.environ.get("PROFILE")
    path = Path(p) if p else DATA / "profile.yaml"
    if not path.exists():
        path = ROOT / "profiles" / "default.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


PROFILE = load_profile()
# type key -> type with its category; keys are what items.type stores
TYPES = {t["key"]: dict(t, category=c) for c in PROFILE["categories"] for t in c.get("types", [])}
assert len(TYPES) == sum(len(c.get("types", [])) for c in PROFILE["categories"]), "type keys must be unique"


def fields_for(type_key):
    """Card fields of a type: common, then category, then type; deeper levels override by key."""
    t = TYPES.get(type_key)
    levels = [PROFILE["item_fields"]] + ([t["category"].get("fields", []), t.get("fields", [])] if t else [])
    merged = {}
    for level in levels:
        for f in level:
            merged[f["key"]] = {**merged.get(f["key"], {}), **f}
    return list(merged.values())


def type_label(type_key):
    t = TYPES.get(type_key)
    return f"{t['category']['label']} › {t['label']}" if t else ""


def db():
    c = sqlite3.connect(DATA / "inventory.db")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init():
    UPLOADS.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.executescript(SCHEMA)


def norm(s):
    return str(s or "").lower().replace("ё", "е")


def item_fields(row):
    return json.loads(row["fields"])


def new_boxes(n):
    ids = []
    with db() as c:
        while len(ids) < n:
            bid = "".join(secrets.choice(ID_ALPHABET) for _ in range(5))
            if c.execute("INSERT OR IGNORE INTO boxes(id) VALUES (?)", (bid,)).rowcount:
                ids.append(bid)
    return ids


def box_where(c, box_id):
    """Human path of where a box stands: 'Шкаф › K3ABC'."""
    parts, seen = [], set()
    b = c.execute("SELECT * FROM boxes WHERE id=?", (box_id,)).fetchone()
    while b and b["parent_id"] and b["parent_id"] not in seen:
        seen.add(b["parent_id"])
        b = c.execute("SELECT * FROM boxes WHERE id=?", (b["parent_id"],)).fetchone()
        if b:
            parts.insert(0, b["id"] + (f" {b['name']}" if b["name"] else ""))
    if b and b["place_id"]:
        p = c.execute("SELECT name FROM places WHERE id=?", (b["place_id"],)).fetchone()
        parts.insert(0, p["name"])
    return " › ".join(parts)


def move(item_id, box_id, delta, kind, author, project_id=None, note=""):
    """The only way stock changes: updates the box×item quantity and logs a movement."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind}")
    with db() as c:
        if not c.execute("SELECT 1 FROM boxes WHERE id=?", (box_id,)).fetchone():
            raise ValueError(f"нет коробки {box_id}")
        if not c.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
            raise ValueError(f"нет позиции {item_id}")
        row = c.execute("SELECT qty FROM stock WHERE box_id=? AND item_id=?", (box_id, item_id)).fetchone()
        qty = (row["qty"] if row else 0) + delta
        if qty < 0:
            raise ValueError(f"в коробке только {row['qty'] if row else 0}")
        if delta == 0:
            return qty
        if qty == 0:
            c.execute("DELETE FROM stock WHERE box_id=? AND item_id=?", (box_id, item_id))
        else:
            c.execute("INSERT INTO stock(box_id, item_id, qty) VALUES (?,?,?) ON CONFLICT DO UPDATE "
                      "SET qty=excluded.qty, updated_at=datetime('now','localtime')", (box_id, item_id, qty))
        c.execute("INSERT INTO movements(item_id, box_id, delta, kind, project_id, author, note) "
                  "VALUES (?,?,?,?,?,?,?)", (item_id, box_id, delta, kind, project_id, author, note))
    return qty


def clear_box(box_id, author):
    """Empty the box for reuse: stock out with 'clear' movements, child boxes move up a level."""
    with db() as c:
        rows = c.execute("SELECT item_id, qty FROM stock WHERE box_id=?", (box_id,)).fetchall()
        b = c.execute("SELECT * FROM boxes WHERE id=?", (box_id,)).fetchone()
    for r in rows:
        move(r["item_id"], box_id, -r["qty"], "clear", author)
    with db() as c:
        c.execute("UPDATE boxes SET parent_id=?, place_id=COALESCE(place_id, ?) WHERE parent_id=?",
                  (b["parent_id"], b["place_id"], box_id))


def stock_of_item(c, item_id):
    rows = c.execute("SELECT s.box_id, s.qty, b.name FROM stock s JOIN boxes b ON b.id=s.box_id "
                     "WHERE s.item_id=? ORDER BY s.box_id", (item_id,)).fetchall()
    return [dict(r, where=box_where(c, r["box_id"])) for r in rows]


def search(q):
    """Substring search over name + profile fields marked `search`, ranked by field weight.

    Every word of the query must hit somewhere. Returns (items, boxes).
    """
    # ponytail: full scan in Python (proper Cyrillic lowercasing, ё=е); fine to ~10k items, then FTS5.
    words = norm(q).split()
    if not words:
        return [], []
    weights = {k: {f["key"]: int(f["search"]) for f in fields_for(k) if f.get("search")} for k in [*TYPES, ""]}
    items, boxes = [], []
    with db() as c:
        for row in c.execute("SELECT * FROM items"):
            f = item_fields(row)
            texts = [(3, norm(row["name"])), (2, norm(type_label(row["type"])))]
            texts += [(w, norm(f.get(k))) for k, w in weights.get(row["type"], weights[""]).items()]
            score = 0
            for word in words:
                hit = max((w for w, t in texts if word in t), default=0)
                if not hit:
                    break
                score += hit
            else:
                score += norm(row["name"]).startswith(words[0])
                items.append(dict(id=row["id"], name=row["name"], type=row["type"], fields=f, score=score,
                                  stock=stock_of_item(c, row["id"])))
        for b in c.execute("SELECT b.*, p.name AS place FROM boxes b LEFT JOIN places p ON p.id=b.place_id"):
            text = norm(f"{b['id']} {b['name']} {b['place'] or ''}")
            if all(w in text for w in words):
                boxes.append(dict(id=b["id"], name=b["name"], where=box_where(c, b["id"])))
    items.sort(key=lambda i: (-i["score"], norm(i["name"])))
    return items, boxes
