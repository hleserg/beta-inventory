"""Storage, profile, search and stock movements. Shared by the web pages and (later) MCP."""
import io
import json
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import urllib.request
import uuid
import zipfile
from pathlib import Path

import numpy as np
import yaml
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("DATA_DIR", ROOT / "data"))
UPLOADS = DATA / "uploads"
ID_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # no 0/O/1/I
# №28: things taken and not put back, or brought home and not put away yet, lie «на руках» — a box of kind
# 'hands' that no box list shows; stock and history work on it as on any box
HANDS = "HANDS"
# manifesto 3: ordered and not come yet lies «в пути» — a box of kind 'transit'; a thing's total leaves it out,
# its «докупить» counts it. Five letters of ID_ALPHABET, so its page and label work like any box's
TRANSIT = "TRANS"

# qty NULL: «есть, не считал» (manifesto 4) — loose parts nobody counts; found and taken, left out of sums
STOCK = """CREATE TABLE IF NOT EXISTS stock(
  box_id TEXT NOT NULL REFERENCES boxes(id), item_id INTEGER NOT NULL REFERENCES items(id),
  qty INTEGER CHECK (qty IS NULL OR qty > 0),
  updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  PRIMARY KEY (box_id, item_id));"""
SCHEMA = """
CREATE TABLE IF NOT EXISTS places(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, note TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS boxes(
  id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'box',
  place_id INTEGER REFERENCES places(id) ON DELETE SET NULL,
  parent_id TEXT REFERENCES boxes(id) ON DELETE SET NULL,
  photos TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL DEFAULT '', fields TEXT NOT NULL DEFAULT '{}',
  for_agent INTEGER NOT NULL DEFAULT 0, single INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
""" + STOCK + """
CREATE TABLE IF NOT EXISTS projects(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  git_url TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active');
CREATE TABLE IF NOT EXISTS movements(
  id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id), box_id TEXT NOT NULL,
  delta INTEGER NOT NULL, kind TEXT NOT NULL, project_id INTEGER REFERENCES projects(id),
  author TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
  at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS needs(
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE, item_id INTEGER NOT NULL REFERENCES items(id),
  qty INTEGER NOT NULL CHECK (qty > 0), PRIMARY KEY (project_id, item_id));
CREATE TABLE IF NOT EXISTS readers(
  id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
  place_id INTEGER REFERENCES places(id) ON DELETE SET NULL,
  portable INTEGER NOT NULL DEFAULT 0, accepted INTEGER NOT NULL DEFAULT 0,
  box_id TEXT REFERENCES boxes(id) ON DELETE SET NULL, tapped_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS trash(
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL, data TEXT NOT NULL,
  at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
"""

MOVES = ("SELECT m.*, i.name AS item, p.name AS project, b.name AS box FROM movements m JOIN items i ON i.id=m.item_id "
         "LEFT JOIN projects p ON p.id=m.project_id LEFT JOIN boxes b ON b.id=m.box_id "
         # the hand's leg of a take/put/return mirrors the box's: history shows the box's only (№28)
         "WHERE NOT (m.box_id='HANDS' AND (m.kind='take' AND m.delta>0 OR m.kind IN ('put', 'return') AND m.delta<0))")
KINDS = {"put": "положил", "take": "забрал", "return": "вернул", "buy": "докупил",
         "count": "инвентаризация", "clear": "освободил", "move": "переложил", "spend": "списал"}


def load_profile():
    p = os.environ.get("PROFILE")
    path = Path(p) if p else DATA / "profile.yaml"
    if not path.exists():
        path = ROOT / "profiles" / "default.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


PROFILE = load_profile()
# type key -> type with its category; keys are what items.type stores
TYPES = {t["key"]: dict({"single": c.get("single", False)}, **t, category=c)  # a category's `single` goes to its types
         for c in PROFILE["categories"] for t in c.get("types", [])}
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


def unit_of(type_key, fields):
    """Manifesto 4: what a thing's qty counts — its field marked `unit`, else that field's `default` for the type
    (wire: м), else the profile's terms.unit."""
    fd = next((f for f in fields_for(type_key) if f.get("unit")), {})
    return fields.get(fd.get("key")) or fd.get("default") or PROFILE["terms"]["unit"]


def type_label(type_key):
    t = TYPES.get(type_key)
    return f"{t['category']['label']} › {t['label']}" if t else ""


# field key -> search weight, per type ("" = untyped item)
WEIGHTS = {k: {f["key"]: int(f["search"]) for f in fields_for(k) if f.get("search")} for k in [*TYPES, ""]}

# Meaning search: a small local model, so "понижайка" finds a buck converter with no alias typed in.
# Off with SEMANTIC_MODEL= ; keyword search works alone while the model loads or if it can't.
SEM_MODEL = os.environ.get("SEMANTIC_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
SEM_MIN = float(os.environ.get("SEMANTIC_MIN", "0.35"))  # cosine floor; tune on real data
SEM_MARGIN = float(os.environ.get("SEMANTIC_MARGIN", "0.08"))  # and no further than this below the best match: cut noise 4x on 22 test queries
SEM_TOP = int(os.environ.get("SEMANTIC_TOP", "5"))
_sem = {"model": None, "vecs": {}}  # vecs: item_id -> (embedded text, unit vector)

# Photos: phones shoot 5-10 MB. Shrunk to this long side and JPEG quality: screw sizes on a box photo stay readable.
PHOTO_MAX_PX = int(os.environ.get("PHOTO_MAX_PX", "2560"))
PHOTO_QUALITY = int(os.environ.get("PHOTO_QUALITY", "90"))
TRASH_DAYS = int(os.environ.get("TRASH_DAYS", "30"))  # deleted things wait this long for «Вернуть», then go for good
READER_FORGET_MIN = int(os.environ.get("READER_FORGET_MIN", "10"))  # a portable NFC reader forgets its box after this long without a tap


def db():
    c = sqlite3.connect(DATA / "inventory.db")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init():
    UPLOADS.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.executescript(SCHEMA)
        if "IS NULL" not in c.execute("SELECT sql FROM sqlite_master WHERE name='stock'").fetchone()["sql"]:
            # stock from before «не считал»: SQLite changes a CHECK only by rebuilding the table
            c.executescript("BEGIN; ALTER TABLE stock RENAME TO stock_v0;" + STOCK +
                            "INSERT INTO stock SELECT * FROM stock_v0; DROP TABLE stock_v0; COMMIT;")
        if "kind" not in [r["name"] for r in c.execute("PRAGMA table_info(boxes)")]:  # boxes from before shelves
            c.execute("ALTER TABLE boxes ADD COLUMN kind TEXT NOT NULL DEFAULT 'box'")
        if "photos" not in [r["name"] for r in c.execute("PRAGMA table_info(boxes)")]:  # boxes from before №45
            c.execute("ALTER TABLE boxes ADD COLUMN photos TEXT NOT NULL DEFAULT '[]'")
        if "for_agent" not in [r["name"] for r in c.execute("PRAGMA table_info(items)")]:  # cards from before №41
            c.execute("ALTER TABLE items ADD COLUMN for_agent INTEGER NOT NULL DEFAULT 0")
        if "single" not in [r["name"] for r in c.execute("PRAGMA table_info(items)")]:  # cards from before №43
            c.execute("ALTER TABLE items ADD COLUMN single INTEGER")
            one = [k for k, t in TYPES.items() if t["single"]]  # a tool put in «не считал» is one
            c.execute(f"UPDATE stock SET qty=1 WHERE qty IS NULL AND item_id IN (SELECT id FROM items WHERE type IN ({','.join('?' * len(one))}))", one)
        for bid, kind, name in ((HANDS, "hands", "На руках"), (TRANSIT, "transit", "В пути")):
            c.execute("INSERT INTO boxes(id, name, kind) VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                      (bid, PROFILE["terms"].get(kind, name), kind))
        pics = {fd["key"] for t in [*TYPES, ""] for fd in fields_for(t) if fd["type"] == "photo"}
        for r in c.execute("SELECT id, fields FROM items").fetchall():  # one photo per card before: a name, not a list
            f = item_fields(r)
            old = {k: f[k] for k in pics if isinstance(f.get(k), str)}
            if old:
                f.update({k: [v] if v else [] for k, v in old.items()})
                c.execute("UPDATE items SET fields=? WHERE id=?", (json.dumps(f, ensure_ascii=False), r["id"]))
    if SEM_MODEL:
        threading.Thread(target=_load_model, daemon=True).start()


def _load_model():
    try:
        from fastembed import TextEmbedding
        _sem["model"] = TextEmbedding(SEM_MODEL, cache_dir=str(DATA / "models"))  # ~240 MB, first start only
        with db() as c:
            _item_vecs(c.execute("SELECT * FROM items").fetchall())  # warm up before the first search
    except Exception as e:  # no network, unknown model: keep keyword search
        print(f"meaning search off: {e!r}", flush=True)


def _unit(vectors):
    m = np.array(list(vectors))
    return m / np.linalg.norm(m, axis=1, keepdims=True)


def item_text(row):
    """What the model reads: name, type, then searchable fields, heaviest first (the model truncates the tail)."""
    f = item_fields(row)
    keys = sorted(WEIGHTS.get(row["type"], WEIGHTS[""]).items(), key=lambda kw: -kw[1])
    return ". ".join(filter(None, [row["name"], type_label(row["type"])] + [str(f.get(k) or "") for k, _ in keys]))


def _item_vecs(rows):
    """Item vectors, re-embedding only items whose text changed since last time."""
    vecs = _sem["vecs"]
    todo = [(r["id"], t) for r in rows if vecs.get(r["id"], ("",))[0] != (t := item_text(r))]
    if todo:
        for (iid, t), v in zip(todo, _unit(_sem["model"].embed([t for _, t in todo]))):
            vecs[iid] = (t, v)
    return {r["id"]: vecs[r["id"]] for r in rows}  # a deleted item's vector must not raise the cut


def norm(s):
    return str(s or "").lower().replace("ё", "е")


def item_fields(row):
    return json.loads(row["fields"])


def new_id():
    return "".join(secrets.choice(ID_ALPHABET) for _ in range(5))


def is_box_id(s):
    return len(s) == 5 and set(s) <= set(ID_ALPHABET)


def new_boxes(n):
    ids = []
    with db() as c:
        while len(ids) < n:
            bid = new_id()
            if c.execute("INSERT OR IGNORE INTO boxes(id) VALUES (?)", (bid,)).rowcount:
                ids.append(bid)
    return ids


def free_box_id():
    """An ID for a box not made yet: «+ Коробка» shows it, the first save makes the box."""
    with db() as c:
        while c.execute("SELECT 1 FROM boxes WHERE id=?", (bid := new_id(),)).fetchone():
            pass
    return bid


def box_crumbs(c, box_id):
    """The way up from a box, as links: [('Шкаф', '/places#p1'), ('K3ABC', '/b/K3ABC')] (№44)."""
    parts, seen = [], set()
    b = c.execute("SELECT * FROM boxes WHERE id=?", (box_id,)).fetchone()
    while b and b["parent_id"] and b["parent_id"] not in seen:
        seen.add(b["parent_id"])
        b = c.execute("SELECT * FROM boxes WHERE id=?", (b["parent_id"],)).fetchone()
        if b:
            parts.insert(0, (b["name"] or b["id"], f"/b/{b['id']}"))
    if b and b["place_id"]:
        p = c.execute("SELECT name FROM places WHERE id=?", (b["place_id"],)).fetchone()
        parts.insert(0, (p["name"], f"/places#p{b['place_id']}"))
    return parts


def box_where(c, box_id):
    """Human path of where a box stands: 'Шкаф › K3ABC'."""
    return " › ".join(label for label, _ in box_crumbs(c, box_id))


def find_box(text):
    """Box ID from what a person typed or picked: the ID in any case, «Name (ID)» from the list, a unique part of a name."""
    t = text.strip()
    if not t:  # no box: in hand (№28)
        return HANDS
    with db() as c:
        boxes = c.execute("SELECT id, name, kind FROM boxes ORDER BY name").fetchall()
    ids = {b["id"] for b in boxes}
    m = re.search(r"\(([A-Za-z0-9]+)\)$", t)  # ASCII only: «Резисторы (Вт)» is a name, not an ID
    for k in (t.upper(), m and m[1].upper()):
        if k in ids:
            return k
    # casefold here, not SQL: SQLite's lower() and LIKE leave Cyrillic as is
    hits = [b for b in boxes if t and b["kind"] != "hands" and t.casefold() in (b["name"] or "").casefold()]
    exact = [b for b in hits if b["name"].casefold() == t.casefold()]
    if len(hits) == 1 or len(exact) == 1:
        return (exact or hits)[0]["id"]
    if not hits:
        raise ValueError(f"Нет коробки «{t}»")
    raise ValueError("Несколько коробок: " + ", ".join(f"«{b['name']}» ({b['id']})" for b in hits[:5]) + ". Какая?")


def move(item_id, box_id, delta, kind, author, project_id=None, note=""):
    """The only way stock changes: updates the box×item quantity and logs a movement.

    delta None puts some in without counting: the pair's qty becomes None, «есть, не считал». Puts and takes
    keep such a pair uncounted (the movement still logs their delta); count and clear give it a number again,
    and so does a return: «вернул 1» on a thing made without a count means there is one (№34).
    """
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind}")
    with db() as c:
        if not c.execute("SELECT 1 FROM boxes WHERE id=?", (box_id,)).fetchone():
            raise ValueError(f"нет коробки {box_id}")
        it = c.execute("SELECT single, type FROM items WHERE id=?", (item_id,)).fetchone()
        if not it:
            raise ValueError(f"нет позиции {item_id}")
        row = c.execute("SELECT qty FROM stock WHERE box_id=? AND item_id=?", (box_id, item_id)).fetchone()
        old = row["qty"] if row else 0
        if single_of(it):  # №43: one of a kind is counted by being there — never «не считал»
            old, delta = 1 if old is None else old, 1 if delta is None else delta
        if old is None and kind == "return" and delta:
            old = 0
        # count and clear say how many are left, and so does moving all of an uncounted pile out
        if delta is None or old is None and kind not in ("count", "clear") and (kind, delta) != ("move", 0):
            qty, note = None, note or "не считал"
        else:
            qty = (old or 0) + delta
            if qty < 0:
                raise ValueError(f"в коробке только {old}")
        if delta == 0 and qty == old:
            return qty
        if qty == 0:
            c.execute("DELETE FROM stock WHERE box_id=? AND item_id=?", (box_id, item_id))
        else:
            c.execute("INSERT INTO stock(box_id, item_id, qty) VALUES (?,?,?) ON CONFLICT DO UPDATE "
                      "SET qty=excluded.qty, updated_at=datetime('now','localtime')", (box_id, item_id, qty))
        c.execute("INSERT INTO movements(item_id, box_id, delta, kind, project_id, author, note) "
                  "VALUES (?,?,?,?,?,?,?)", (item_id, box_id, delta or 0, kind, project_id, author, note))
    return qty


def change_stock(box_id, item_id, action, qty, author, project_id=None):
    """A stock change as people say it: put/return/buy/take qty, or count: the box holds qty now.

    qty None on put/return/buy: some went in, not counted.
    No box, or HANDS, is «на руках» (№28): a take from a box lands there, a put or return into a box takes from
    there first, and a take from there is written off («списал»).
    """
    box_id = find_box(box_id)
    if action not in ("put", "return", "buy", "take", "count"):
        raise ValueError(f"нет действия {action}: put, return, buy, take, count")
    with db() as c:
        it = c.execute("SELECT single, type FROM items WHERE id=?", (item_id,)).fetchone()
        rows = {r["box_id"]: r["qty"] for r in c.execute("SELECT box_id, qty FROM stock WHERE item_id=?", (item_id,))}
    if it and single_of(it) and action != "count":  # №43: one of a kind — no count asked, and it lies in one place
        qty = 1
        if action in ("put", "return") and box_id != HANDS and HANDS not in rows and rows:
            return (rows[box_id] or 1) if box_id in rows else transfer(item_id, next(iter(rows)), box_id, author)
    if qty is None and action in ("take", "count"):
        raise ValueError("Сколько? Нужно число")
    if action == "count":
        with db() as c:
            row = c.execute("SELECT qty FROM stock WHERE box_id=? AND item_id=?", (box_id, item_id)).fetchone()
        return move(item_id, box_id, max(qty, 0) - ((row["qty"] or 0) if row else 0), "count", author)
    if qty is not None and qty < 1:
        raise ValueError("Количество должно быть больше нуля")
    if box_id == HANDS:
        if qty is None:
            raise ValueError("Сколько на руках? Нужно число")
        return move(item_id, HANDS, -qty if action == "take" else qty, "spend" if action == "take" else action,
                    author, project_id)
    if action == "take":  # the box first: only it can refuse («в коробке только N»)
        left = move(item_id, box_id, -qty, "take", author, project_id)
        move(item_id, HANDS, qty, "take", author, project_id)
        return left
    if action in ("put", "return"):  # put away what is in hand: not a second lot
        with db() as c:
            row = c.execute("SELECT qty FROM stock WHERE box_id=? AND item_id=?", (HANDS, item_id)).fetchone()
        if row:
            qty = qty or row["qty"]  # «не считал» from hand: all of it, and that many
            move(item_id, HANDS, -min(qty, row["qty"]), action, author)
    return move(item_id, box_id, qty, action, author, project_id)


def transfer(item_id, src, dst, author, qty=None):
    """A thing from one box to another (a box scanned on its card, №30): two «переложил» movements. qty None moves
    it all, else at most qty; from «в пути» all of qty lands — 12 came of 10 ordered: 10 leave it, 12 land."""
    if qty is not None and qty < 1:
        raise ValueError("Количество должно быть больше нуля")
    src, dst = find_box(src), find_box(dst)
    if src == dst:
        return
    with db() as c:
        row = c.execute("SELECT qty FROM stock WHERE box_id=? AND item_id=?", (src, item_id)).fetchone()
        it = c.execute("SELECT single, type FROM items WHERE id=?", (item_id,)).fetchone()
    if not row:
        raise ValueError(f"в {src} этого нет")
    # ponytail: two transactions like clear_box; both boxes are checked first, so a half-move needs a crash between
    took = row["qty"] if qty is None or row["qty"] is None else min(qty, row["qty"])
    if took is None and single_of(it):  # №43: an uncounted unique thing is one
        took = 1
    move(item_id, src, -(took or 0), "move", author)
    return move(item_id, dst, qty if qty is not None and src == TRANSIT else took, "move", author)


def projects(c):
    return c.execute("SELECT id, name FROM projects WHERE status='active' ORDER BY name").fetchall()


def set_need(project_id, item_id, qty):
    """How many of a thing a project needs; 0 or None: it needs none."""
    with db() as c:
        if not qty:
            c.execute("DELETE FROM needs WHERE project_id=? AND item_id=?", (project_id, item_id))
        else:
            c.execute("INSERT INTO needs(project_id, item_id, qty) VALUES (?,?,?) ON CONFLICT DO UPDATE SET qty=excluded.qty",
                      (project_id, item_id, qty))


def project_needs(project_id):
    """What a project needs, line by line: need, have (all boxes and hands), transit («в пути»), short = what is left
    to order. A pile «есть, не считал» may be enough: its line is not short.
    ponytail: stock is not reserved — two projects needing the same 5 both see them; add reservations when that bites."""
    with db() as c:
        rows = c.execute(
            "SELECT n.item_id, i.name, i.type, i.fields, n.qty AS need, "
            "coalesce(sum(CASE WHEN s.box_id!=? THEN s.qty END), 0) AS have, "
            "coalesce(sum(CASE WHEN s.box_id=? THEN s.qty END), 0) AS transit, "
            "count(CASE WHEN s.box_id!=? AND s.qty IS NULL THEN 1 END) AS uncounted "
            "FROM needs n JOIN items i ON i.id=n.item_id LEFT JOIN stock s ON s.item_id=n.item_id "
            "WHERE n.project_id=? GROUP BY n.item_id ORDER BY i.name", (TRANSIT, TRANSIT, TRANSIT, project_id)).fetchall()
    return [dict(r, short=0 if r["uncounted"] else max(0, r["need"] - r["have"] - r["transit"])) for r in rows]


# GitHub: the owner's new repos wait in the inbox (status 'inbox') until a person or an agent takes them in their
# own words (accept_project) or skips them ('skipped': not offered again). No owner and no token: sync is off.
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_SYNC_MIN = float(os.environ.get("GITHUB_SYNC_MIN", "60"))


def repo_key(url):
    return url.strip().lower().rstrip("/").removesuffix(".git")


def fetch_repos():
    """The owner's repos; a token sees the private ones too."""
    url = ("https://api.github.com/user/repos?affiliation=owner&per_page=100" if GITHUB_TOKEN
           else f"https://api.github.com/users/{GITHUB_OWNER}/repos?per_page=100")
    headers = {"Accept": "application/vnd.github+json"} | ({"Authorization": f"Bearer {GITHUB_TOKEN}"} if GITHUB_TOKEN else {})
    repos, page = [], 1
    while True:
        with urllib.request.urlopen(urllib.request.Request(f"{url}&page={page}", headers=headers), timeout=30) as r:
            batch = json.load(r)
        repos += batch
        if len(batch) < 100:
            return repos
        page += 1


def sync_projects(repos):
    """Repos not known in any status, forks aside, go to the inbox. → how many."""
    with db() as c:
        known = {repo_key(r[0]) for r in c.execute("SELECT git_url FROM projects")}
        new = [r for r in repos if not r.get("fork") and repo_key(r["html_url"]) not in known]
        c.executemany("INSERT INTO projects(name, description, git_url, status) VALUES (?, ?, ?, 'inbox')",
                      [(r["name"], r.get("description") or "", r["html_url"]) for r in new])
    return len(new)


def project_by_url(c, git_url):
    return next((r for r in c.execute("SELECT id, status, git_url FROM projects WHERE git_url != ''")
                 if repo_key(r["git_url"]) == repo_key(git_url)), None)


def accept_project(git_url, name, description):
    """Inbox repo → active project under the given name; a repo the sync can't see (private) is made. → id."""
    with db() as c:
        p = project_by_url(c, git_url)
        if p is None:
            return c.execute("INSERT INTO projects(name, description, git_url) VALUES (?, ?, ?)",
                             (name.strip(), description.strip(), git_url.strip())).lastrowid
        c.execute("UPDATE projects SET name=?, description=?, status='active' WHERE id=?",
                  (name.strip(), description.strip(), p["id"]))
        return p["id"]


def skip_project(git_url):
    with db() as c:
        p = project_by_url(c, git_url)
        if p is None or p["status"] not in ("inbox", "skipped"):
            raise ValueError(f"{git_url} не во входящих")
        c.execute("UPDATE projects SET status='skipped' WHERE id=?", (p["id"],))


def clean_fields(name, fields, f):
    """Card check shared by the site and MCP: numbers parsed in place, required fields present. → errors."""
    errors = [] if name.strip() else ["Название: обязательно"]
    for fd in fields:
        k, v = fd["key"], f.get(fd["key"])
        if fd["type"] == "number" and isinstance(v, str) and v.strip():
            try:
                v = float(v.strip().replace(",", "."))
                f[k] = int(v) if v.is_integer() else v
            except ValueError:
                errors.append(f"{fd['label']}: нужно число")
        if fd.get("required") and f.get(k) in (None, "", []):
            errors.append(f"{fd['label']}: обязательно")
    return errors



def save_item(item_id, name, type_key, f):
    """New card when item_id is None. → item id"""
    with db() as c:
        if item_id is None:
            return c.execute("INSERT INTO items(name, type, fields) VALUES (?, ?, ?)",
                             (name, type_key, json.dumps(f, ensure_ascii=False))).lastrowid
        c.execute("UPDATE items SET name=?, type=?, fields=?, updated_at=datetime('now','localtime') WHERE id=?",
                  (name, type_key, json.dumps(f, ensure_ascii=False), item_id))
        return item_id


def single_of(it):
    """№43 one of a kind: ticked or unticked on the card, else as its type says (profile `single`, e.g. tools)."""
    return bool(it["single"]) if it["single"] is not None else TYPES.get(it["type"], {}).get("single", False)


def set_single(item_id, on):
    with db() as c:
        c.execute("UPDATE items SET single=? WHERE id=?", (int(on), item_id))
        if on:  # one of a kind is never «не считал»
            c.execute("UPDATE stock SET qty=1 WHERE qty IS NULL AND item_id=?", (item_id,))


def tag_item(item_id):
    """A tag got written or scanned (№43): the thing is one of a kind from now on, unless unticked by hand before."""
    with db() as c:
        if c.execute("UPDATE items SET single=1 WHERE id=? AND single IS NULL", (item_id,)).rowcount:
            c.execute("UPDATE stock SET qty=1 WHERE qty IS NULL AND item_id=?", (item_id,))


def set_for_agent(item_id, on):
    """№41 «Передать агенту»: the card waits in MCP agent_queue until an agent's update_item."""
    with db() as c:
        c.execute("UPDATE items SET for_agent=? WHERE id=?", (int(on), item_id))


def own_upload(v):
    """Name of our own upload, given as a /u/ link or bare, or None: a form or an agent can't point at other files."""
    name = str(v).rsplit("/u/", 1)[-1]
    return name if name and Path(name).name == name and (UPLOADS / name).is_file() else None


def backup(path):
    """A household in one zip: a consistent copy of the database, and the uploads. models/ downloads again by itself.
    Back: stop, unzip into DATA_DIR, start."""
    with zipfile.ZipFile(path, "w") as z, tempfile.TemporaryDirectory() as d:
        src, dst = sqlite3.connect(DATA / "inventory.db"), sqlite3.connect(Path(d) / "inventory.db")
        src.backup(dst)
        src.close(), dst.close()
        z.write(Path(d) / "inventory.db", "inventory.db", zipfile.ZIP_DEFLATED)
        for f in sorted(UPLOADS.rglob("*")):  # photos are compressed already: stored as they are
            if f.is_file():
                z.write(f, f.relative_to(DATA).as_posix())


def save_bytes(data, ext, photo=False):
    """Store an upload, return its name under /u/. Photos are shrunk: see PHOTO_MAX_PX."""
    if photo:
        try:
            im = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
            im.thumbnail((PHOTO_MAX_PX, PHOTO_MAX_PX))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=PHOTO_QUALITY, optimize=True)
            data, ext = buf.getvalue(), ".jpg"
        except OSError:
            pass  # Pillow can't read it (HEIC…) — keep the original
    name = uuid.uuid4().hex + ext
    (UPLOADS / name).write_bytes(data)
    return name

def turn_photo(name, deg):
    """Turn a stored photo by 90° (deg > 0 — clockwise). → the new file's name: the old one may sit in a cache."""
    try:
        im = Image.open(UPLOADS / name).transpose(Image.Transpose.ROTATE_270 if deg > 0 else Image.Transpose.ROTATE_90)
    except OSError:
        raise ValueError("Это фото не повернуть: формат не читается")
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=PHOTO_QUALITY, optimize=True)
    return save_bytes(buf.getvalue(), ".jpg")


def rotate_box_photo(box_id, name, deg):
    with db() as c:
        b = c.execute("SELECT photos FROM boxes WHERE id=?", (box_id,)).fetchone()
    pics = json.loads(b["photos"]) if b else []
    if name not in pics:
        raise ValueError("Нет такого фото у этой коробки")
    new = turn_photo(name, deg)
    with db() as c:
        c.execute("UPDATE boxes SET photos=? WHERE id=?", (json.dumps([new if x == name else x for x in pics]), box_id))
    return new


def rotate_photo(item_id, name, deg):
    """Turn one photo of the card by 90° (deg > 0 — clockwise). → the new file's name: the old one may sit in a cache."""
    with db() as c:
        r = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    f = item_fields(r) if r else {}
    k = next((k for k, v in f.items() if isinstance(v, list) and name in v), None)
    if not k:
        raise ValueError("Нет такого фото у этой вещи")
    new = turn_photo(name, deg)
    f[k] = [new if x == name else x for x in f[k]]
    with db() as c:
        c.execute("UPDATE items SET fields=?, updated_at=datetime('now','localtime') WHERE id=?",
                  (json.dumps(f, ensure_ascii=False), item_id))
    return new


def clear_box(box_id, author):
    """Empty the box for reuse: stock out with 'clear' movements, child boxes move up a level."""
    with db() as c:
        rows = c.execute("SELECT item_id, qty FROM stock WHERE box_id=?", (box_id,)).fetchall()
        b = c.execute("SELECT * FROM boxes WHERE id=?", (box_id,)).fetchone()
    for r in rows:
        move(r["item_id"], box_id, -(r["qty"] or 0), "clear", author)
    with db() as c:
        c.execute("UPDATE boxes SET parent_id=?, place_id=COALESCE(place_id, ?) WHERE parent_id=?",
                  (b["parent_id"], b["place_id"], box_id))


# Trash (№18): the rows leave their tables for one JSON snapshot, so every list, search and agent stops seeing
# them with no filter anywhere; «Вернуть» puts them back. ponytail: uploaded photos stay on disk after the purge.
def _rows(c, sql, *a):
    return [dict(r) for r in c.execute(sql, a)]


def _relink(c, s, b, parent, place):
    """A box that loses its parent or place; «Вернуть» undoes it unless the box was moved since."""
    s["links"].append([b["id"], b["parent_id"], b["place_id"], parent, place])
    c.execute("UPDATE boxes SET parent_id=?, place_id=? WHERE id=?", (parent, place, b["id"]))


def _drop_box(c, s, b):
    """The box and its stock go into the snapshot; boxes inside move up a level, as when it is freed."""
    s["boxes"].append(dict(b))
    s["stock"] += _rows(c, "SELECT * FROM stock WHERE box_id=?", b["id"])
    for r in c.execute("SELECT * FROM boxes WHERE parent_id=?", (b["id"],)).fetchall():
        _relink(c, s, r, b["parent_id"], r["place_id"] or b["place_id"])
    c.execute("DELETE FROM stock WHERE box_id=?", (b["id"],))
    c.execute("DELETE FROM boxes WHERE id=?", (b["id"],))


def trash(kind, key):
    """kind is item, box or place. A place takes its shelves along (a shelf never leaves its cabinet);
    boxes standing on it lose the place (№27)."""
    s = {k: [] for k in ("places", "boxes", "items", "stock", "movements", "links", "needs")}
    with db() as c:
        table = {"item": "items", "box": "boxes", "place": "places"}[kind]
        r = c.execute(f"SELECT * FROM {table} WHERE id=?", (key,)).fetchone()
        if not r:
            raise ValueError("Уже удалено")
        if kind == "box" and r["kind"] in ("hands", "transit"):
            raise ValueError(f"«{r['name']}» не удаляется")
        if kind == "item":
            s["items"].append(dict(r))
            s["stock"] = _rows(c, "SELECT * FROM stock WHERE item_id=?", key)
            s["movements"] = _rows(c, "SELECT * FROM movements WHERE item_id=?", key)
            s["needs"] = _rows(c, "SELECT * FROM needs WHERE item_id=?", key)
            for t in ("movements", "stock", "needs"):
                c.execute(f"DELETE FROM {t} WHERE item_id=?", (key,))
            c.execute("DELETE FROM items WHERE id=?", (key,))
        elif kind == "box":
            _drop_box(c, s, r)
        else:
            for sh in c.execute("SELECT * FROM boxes WHERE place_id=? AND kind='shelf'", (key,)).fetchall():
                _drop_box(c, s, sh)
            for b in c.execute("SELECT * FROM boxes WHERE place_id=?", (key,)).fetchall():
                _relink(c, s, b, b["parent_id"], None)
            s["places"].append(dict(r))
            c.execute("DELETE FROM places WHERE id=?", (key,))
        c.execute("INSERT INTO trash(kind, label, data) VALUES (?, ?, ?)",
                  (kind, r["name"] or r["id"], json.dumps(s, ensure_ascii=False)))
        _purge(c)


def _put(c, table, r):
    return c.execute(f"INSERT INTO {table}({','.join(r)}) VALUES ({','.join('?' * len(r))})", list(r.values())).lastrowid


def restore(trash_id):
    """Everything back where it was; returns the page to show. A number taken meanwhile gives a new one."""
    with db() as c:
        t = c.execute("SELECT * FROM trash WHERE id=?", (trash_id,)).fetchone()
        if not t:
            raise ValueError("В корзине этого уже нет")
        s, pid, iid = json.loads(t["data"]), {}, {}
        has = lambda table, key: c.execute(f"SELECT 1 FROM {table} WHERE id=?", (key,)).fetchone()
        for r in s["places"]:
            if c.execute("SELECT 1 FROM places WHERE name=?", (r["name"],)).fetchone():
                raise ValueError(f"Место «{r['name']}» уже есть — переименуйте его, потом верните это")
            old = r["id"]
            pid[old] = _put(c, "places", {k: v for k, v in r.items() if k != "id" or not has("places", old)})
        for r in s["boxes"]:
            if has("boxes", r["id"]):
                raise ValueError(f"Коробку {r['id']} уже завели заново — удалите её, потом верните эту")
            r["place_id"] = pid.get(r["place_id"], r["place_id"])
            r["parent_id"], r["place_id"] = (r["parent_id"] if has("boxes", r["parent_id"]) else None,
                                             r["place_id"] if has("places", r["place_id"]) else None)
            _put(c, "boxes", r)
        for r in s["items"]:
            old = r["id"]
            iid[old] = _put(c, "items", {k: v for k, v in r.items() if k != "id" or not has("items", old)})
        for r in s["stock"]:
            r["item_id"] = iid.get(r["item_id"], r["item_id"])
            if has("boxes", r["box_id"]) and has("items", r["item_id"]):  # one of them deleted since: that pile is gone
                c.execute("INSERT OR IGNORE INTO stock(box_id, item_id, qty, updated_at) VALUES (?, ?, ?, ?)",
                          (r["box_id"], r["item_id"], r["qty"], r["updated_at"]))
        for r in s["movements"]:
            r["item_id"] = iid.get(r["item_id"], r["item_id"])
            _put(c, "movements", {k: v for k, v in r.items() if k != "id" or not has("movements", r["id"])})
        for r in s.get("needs", []):  # trash from before needs has none
            if has("projects", r["project_id"]):
                c.execute("INSERT OR IGNORE INTO needs(project_id, item_id, qty) VALUES (?, ?, ?)",
                          (r["project_id"], iid.get(r["item_id"], r["item_id"]), r["qty"]))
        for box, parent, place, parent_now, place_now in reversed(s["links"]):
            c.execute("UPDATE boxes SET parent_id=?, place_id=? WHERE id=? AND parent_id IS ? AND place_id IS ?",
                      (parent, pid.get(place, place), box, parent_now, pid.get(place_now, place_now)))
        c.execute("DELETE FROM trash WHERE id=?", (trash_id,))
    if s["items"]:
        return f"/i/{iid[s['items'][0]['id']]}"
    return f"/b/{s['boxes'][0]['id']}" if t["kind"] == "box" else f"/places#p{pid[s['places'][0]['id']]}"


def _purge(c):
    c.execute("DELETE FROM trash WHERE at < datetime('now', 'localtime', ?)", (f"-{TRASH_DAYS} days",))


def trash_list():
    with db() as c:
        _purge(c)
        return c.execute("SELECT id, kind, label, at, julianday(at, ?) - julianday('now', 'localtime') AS left "
                         "FROM trash ORDER BY id DESC", (f"+{TRASH_DAYS} days",)).fetchall()


def stock_of_item(c, item_id):
    """Where it lies: [{box_id, qty, name, where}]; in transit and in hand last, their where «заказано ДД.ММ» and
    «взят из …» when a take put it there."""
    rows = c.execute("SELECT s.box_id, s.qty, s.updated_at, b.name FROM stock s JOIN boxes b ON b.id=s.box_id "
                     "WHERE s.item_id=? ORDER BY s.box_id=?, s.box_id=?, s.box_id", (item_id, HANDS, TRANSIT)).fetchall()
    out = [dict(r, where=f"заказано {r['updated_at'][8:10]}.{r['updated_at'][5:7]}" if r["box_id"] == TRANSIT
                else box_where(c, r["box_id"]), crumbs=[] if r["box_id"] in (HANDS, TRANSIT) else box_crumbs(c, r["box_id"]))
           for r in rows]
    if out and out[-1]["box_id"] == HANDS:
        out[-1]["src"] = taken_from(c, item_id)
        if out[-1]["src"]:
            b = c.execute("SELECT name FROM boxes WHERE id=?", (out[-1]["src"],)).fetchone()
            where = box_where(c, out[-1]["src"])
            out[-1]["where"] = "взят из " + ((b["name"] if b else "") or out[-1]["src"]) + (f" · {where}" if where else "")
        if out[-1]["src"] == TRANSIT:  # it came: «положить на место» there would order it again
            out[-1]["src"] = ""
    return out


def taken_from(c, item_id):
    """The box the last lot in hand came from, if a take brought it (a take logs the box, then the hand)."""
    last = c.execute("SELECT id, kind FROM movements WHERE item_id=? AND box_id=? AND delta>0 ORDER BY id DESC LIMIT 1",
                     (item_id, HANDS)).fetchone()
    if not last or last["kind"] != "take":
        return ""
    r = c.execute("SELECT box_id FROM movements WHERE item_id=? AND id<? AND kind='take' AND box_id!=? "
                  "ORDER BY id DESC LIMIT 1", (item_id, last["id"], HANDS)).fetchone()
    return r["box_id"] if r else ""


def lookalikes(name):
    """Items named with the same words, or with all of this name's words, or with only some of them: likely a double (№32)."""
    new = set(norm(name).split())
    with db() as c:
        return [dict(r, fields=item_fields(r), stock=stock_of_item(c, r["id"])) for r in c.execute("SELECT * FROM items ORDER BY name")
                if new and (old := set(norm(r["name"]).split())) and (old <= new or new <= old)][:5]


def box_contents(c, box_id):
    return [dict(r, fields=item_fields(r), single=single_of(r)) for r in c.execute(
        "SELECT s.qty, i.* FROM stock s JOIN items i ON i.id=s.item_id WHERE s.box_id=? ORDER BY i.name", (box_id,))]


def search(q):
    """Items by keyword (name, type, profile fields marked `search`) and by meaning; boxes by id/name/place.

    Keyword hits need every word of the query and rank first, by field weight. Items found only by
    meaning come after them, marked `similar`. Returns (items, boxes).
    """
    # ponytail: full scan in Python (proper Cyrillic lowercasing, ё=е); fine to ~10k items, then FTS5.
    words = norm(q).split()
    if not words:
        return [], []
    items, boxes = [], []
    with db() as c:
        rows = c.execute("SELECT * FROM items").fetchall()
        sims, cut = {}, SEM_MIN
        if _sem["model"] and rows:
            qv = _unit(_sem["model"].embed([q]))[0]
            sims = {iid: float(v @ qv) for iid, (_, v) in _item_vecs(rows).items()}
            cut = max(SEM_MIN, max(sims.values()) - SEM_MARGIN)
        for row in rows:
            f = item_fields(row)
            texts = [(3, norm(row["name"])), (2, norm(type_label(row["type"])))]
            texts += [(w, norm(f.get(k))) for k, w in WEIGHTS.get(row["type"], WEIGHTS[""]).items()]
            score = 0
            for word in words:
                hit = max((w for w, t in texts if word in t), default=0)
                if not hit:
                    score = 0
                    break
                score += hit
            else:
                score += norm(row["name"]).startswith(words[0])
            sim = sims.get(row["id"], 0.0)
            if score or sim >= cut:
                items.append(dict(id=row["id"], name=row["name"], type=row["type"], fields=f,
                                  score=score + sim, similar=not score))
        items.sort(key=lambda i: (i["similar"], -i["score"], norm(i["name"])))
        items = [i for i in items if not i["similar"]] + [i for i in items if i["similar"]][:SEM_TOP]
        for i in items:
            i["stock"] = stock_of_item(c, i["id"])
        for b in c.execute("SELECT b.*, p.name AS place FROM boxes b LEFT JOIN places p ON p.id=b.place_id WHERE b.kind!='hands'"):
            text = norm(f"{b['id']} {b['name']} {b['place'] or ''}")
            if all(w in text for w in words):
                boxes.append(dict(id=b["id"], name=b["name"], where=box_where(c, b["id"])))
    return items, boxes
