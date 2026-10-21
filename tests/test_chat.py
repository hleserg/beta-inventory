"""The chat with agents: the owner writes on the site, an agent woken by cron takes it over MCP and replies."""
from fastapi.testclient import TestClient

from inventory import core
from inventory.app import app
from test_flow import photo, q
from test_mcp import call

c = TestClient(app)


def clear():
    with core.db() as d:
        d.execute("DELETE FROM messages")


def test_owner_writes_agent_answers():
    clear()
    assert c.get("/chat/new").json() == {"new": 0}  # the cron's runner: nothing waits, no agent woken
    r = c.post("/chat", data={"text": "Пришло 10 резисторов 1к"}, files=photo(), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/chat#end"
    assert c.post("/chat", data={"text": "  "}).status_code == 400  # nothing to send
    assert c.get("/chat/new").json() == {"new": 1}
    page = c.get("/chat").text
    assert "Пришло 10 резисторов 1к" in page and "ждёт агента" in page and "/u/" in page and "/delete" in page
    assert "1 ждут агента" in c.get("/more").text

    box = call("chat_inbox", agent="Мара").structured_content
    (m,) = box["new"]
    assert m["text"] == "Пришло 10 резисторов 1к" and m["photos"][0].endswith(".jpg") and "inventory-chat" in box["skill"]
    assert call("chat_inbox", agent="Кодекс").structured_content["new"] == []  # taken: one delivery isn't booked twice
    assert call("chat_inbox", agent="Мара").structured_content["new"][0]["id"] == m["id"]  # its own round broke: again
    page = c.get("/chat").text
    assert "агент взял" in page and f'/chat/{m["id"]}/delete' not in page  # being handled: no ×

    pic = call("view_photo", photo=m["photos"][0])
    assert not pic.is_error and pic.content[0].type == "image" and pic.content[0].mime_type == "image/jpeg"
    assert call("view_photo", photo="../inventory.db").is_error

    assert call("chat_reply", text="", agent="Мара").is_error
    rep = call("chat_reply", text="Записал 10 шт в **A7KQ2**", agent="Мара", done=[m["id"]]).structured_content
    assert rep["url"].endswith("/chat")
    assert c.get("/chat/new").json() == {"new": 0}
    assert call("chat_inbox", agent="Мара").structured_content["new"] == []
    assert any(a["kind"] == "chat" and a["n"] == 1 for a in core.alerts())  # the bell: the agent answered
    assert "Агент ответил" in c.get("/alerts").text
    page = c.get("/chat").text
    assert "<strong>A7KQ2</strong>" in page and "Мара" in page and "разобрано" in page and "msg ag unread" in page
    assert not any(a["kind"] == "chat" for a in core.alerts())  # opened: seen
    assert "msg ag unread" not in c.get("/chat").text


def test_context_and_delete():
    clear()
    core.chat_post("Где паяльник?", [], "человек")
    core.chat_post("Какой из двух — TS101 или TS80?", [], "Мара", by_agent=True)
    core.chat_post("TS101", [], "человек")
    with core.db() as d:  # the first one dealt with already
        d.execute("UPDATE messages SET done_at=at WHERE text='Где паяльник?'")
    box = call("chat_inbox", agent="Мара").structured_content
    assert [m["text"] for m in box["new"]] == ["TS101"]
    assert [m["text"] for m in box["recent"]] == ["Где паяльник?", "Какой из двух — TS101 или TS80?"]  # what «TS101» answers

    mid = core.chat_post("Ошибся, удали", [], "человек")
    c.post(f"/chat/{mid}/delete")
    assert not q("SELECT count(*) FROM messages WHERE id=?", mid)
    taken = box["new"][0]["id"]
    c.post(f"/chat/{taken}/delete")  # an agent works on it: it stays
    assert q("SELECT count(*) FROM messages WHERE id=?", taken)


def test_lease_runs_out():
    clear()
    mid = core.chat_post("Ушло 2 Nano на робота", [], "человек")
    assert call("chat_inbox", agent="Мара").structured_content["new"]
    with core.db() as d:  # Мара's round died long ago
        d.execute("UPDATE messages SET taken_at=datetime('now','localtime','-31 minutes') WHERE id=?", (mid,))
    assert [m["id"] for m in call("chat_inbox", agent="Кодекс").structured_content["new"]] == [mid]
