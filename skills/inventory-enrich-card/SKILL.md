---
name: inventory-enrich-card
description: Use when asked to fill in, complete, check or tidy up existing cards of the home inventory ("дополни", "приложи даташит", "наведи порядок"), or when the inventory MCP agent_queue lists cards a person handed over.
---

# Enrich an inventory card

A person made the card in a hurry: phone photos, a name, maybe a line of text. Turn it into a card they can trust: exact model, official documents, the right type, every field filled. Their photos and words stay.

## Which cards

- `agent_queue` → cards a person ticked «Передать агенту». Do those first.
- Asked about something specific → `search`, then `get_item`.

## Per card

1. **`get_item`, then look at the owner's photos.** Chip markings, silkscreen, labels, connectors. The name may be a guess; the photo is the fact. Read markings letter by letter and search them («TTP223 BA6», «AMS1117-3.3»).
2. **Identify maker and exact model.** Can't be sure → write what it most likely is and «сверьте с шелкографией» in the description. Never present a guess as fact.
3. **Right type?** `card_template()` lists types with hints; take the type whose hint names this thing, the narrowest one if two fit. Wrong type → `update_item(type=...)`: the new type's required fields go in the same call; the old type's values stay stored, hidden, nothing is lost.
4. **Every field of the type, as its hint says** (`card_template(type)`). Values from the datasheet, not from how the thing is wired: a touch sensor with a high/low output is digital, not analog.
5. **The owner's text stays word for word.** Add your part under it, never rewrite or shorten theirs.
6. **Documents.** Datasheet or manual PDF from the maker's own site first. Check the link before attaching: `curl -sIL <url>` → final `200` and `application/pdf`. Only a mirror has it → attach it and name the source in the file name («TTP223 datasheet (lcsc).pdf»). Pages behind a login (Mouser, Digi-Key, LCSC product pages) are not files. No PDF anywhere → the best page goes in the datasheet link field, and say so in the reply.
7. **Photos.** A clean product photo may be added after the owner's, never instead: send get_item's photo list back as it is, plus new direct image URLs.
8. **One `update_item` per card at the end.** Only the keys you give change. It takes the card off `agent_queue`.

Stock is not yours here: no `change_stock`. Two cards for the same thing → tell the person; don't merge or delete.

## Reply

One line per card: what it turned out to be, what was added, what is still missing (no official PDF, model unsure).

## Common mistakes

| Mistake | Instead |
|---|---|
| Rewrote the owner's description | Their text first, verbatim; yours below |
| Kept a wrong type because changing felt risky | `type=` hides the old fields, keeps their values |
| Took the chip from the card's name | Read the markings on the photo |
| Linked a search page or a login-walled page as the datasheet | A direct PDF, checked with `curl -sIL` |
| Replaced the owner's photo with a stock one | Keep theirs first, add after |
