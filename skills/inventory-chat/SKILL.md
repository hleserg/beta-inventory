---
name: inventory-chat
description: Use when the inventory MCP chat_inbox has messages from the owner — deliveries to book ("пришло", "приход"), things to find, questions about the stock, tasks for an agent — or when woken by cron to go through the site's chat.
---

# Answer the owner's chat

The owner drops whatever comes up into the site's chat (/chat): what arrived, what went, where something is, a photo of a delivery note. Nobody waits on the line: an agent comes by on a schedule, does what each message asks and replies in the same chat.

The inventory is reached through its MCP tools only. No scripts of your own against it, no reading its code or database, no files left in its repo.

## The round

1. **`chat_inbox(agent)`** → `new` (yours now, for 30 minutes) and `recent` (the talk before them). Empty `new` → stop, reply nothing.
2. **Read `new` together with `recent`.** A short «да» or «в A7KQ2» is the answer to your earlier question in `recent`: finish that task now. Several messages may be one delivery: handle them as one.
3. **Photos:** open each link; can't reach it (an outside connector) → `view_photo(link)`. A delivery note or a shop's order page is a list of lines: book each line.
4. **Do the work** (below), then **one `chat_reply(text, agent, done=[ids])`** for the round: what you did per message, with links. Every id from `new` goes into `done`, the ones you asked back about too — the owner's answer comes as a new message.

## Per message

**Приход** («пришло», «купил», «привезли», a delivery photo):
1. `search` each thing. Found → `get_item`: look at `stock` and the last `history` lines — already booked today by you or another agent (a round that broke halfway) → don't book it twice, say «уже записано».
2. Where to: the box the owner named → there. Not named and the item lies in one box → that box. Lies in several or nowhere → HANDS («на руках»), and say so: the owner puts it away.
3. In TRANS («в пути», ordered earlier) → `change_stock(TRANS, item, "take", qty)` first, then `put` into the box; not in TRANS → `buy` into the box (or HANDS).
4. Not found → `card_template()` for the type, `create_item(type, name, fields, agent, box_id, qty)` with what the owner said and their photo (`create_item` takes the chat's photo links); fill the fields as `inventory-enrich-card` says, or leave them and say in the reply that the card is bare.
5. No count given («пришли резисторы») → qty null, «не считал».

**Расход** («взял», «ушло на …», «кончились»): `change_stock(..., "take", qty)` from the box it lies in, `project_id` when a project is named (`list_projects`); «кончились» → `count 0`.

**Вопрос** («где …», «сколько …», «что есть для …»): `search`, `get_box`, `get_item`, `stats`, `project_needs`. Answer with the box id and place, the count, a link. Nothing found → say what you searched for.

**Карточку дополнить / найти даташит** → the `inventory-enrich-card` skill (from `agent_queue`), then a line in the reply.

**Unclear** (which box, which variant, how many, the photo unreadable) → don't guess with the stock: book what is certain, ask the rest in the reply, one short question per thing.

## The reply

- Short, plain, in the owner's language (the profile's). No greetings, no exclamations.
- Per message one line: what was done, the count after, where: «Arduino Nano ×2 → [A7KQ2](link), теперь 5».
- Links from the tools' `url`. Numbers only from the tools' answers.
- Nothing done for a message → say why.

## Common mistakes

| Mistake | Instead |
|---|---|
| Booked a delivery a second time after a broken round | Check `get_item` history first |
| Guessed the box | The named box, the only box, else HANDS — and say it |
| Left an asked-back message out of `done` | It is done; the answer comes as a new message |
| Replied once per message | One `chat_reply` per round, `done` lists them all |
| Created a card for a thing that exists | `search` first, by the words and by the model |
| Made up a count | qty null, «не считал» |
| Wrote a script to reach the inventory | The MCP tools only |
