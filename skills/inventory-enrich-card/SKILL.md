---
name: inventory-enrich-card
description: Use when asked to fill in, complete, check or tidy up existing cards of the home inventory ("дополни", "приложи даташит", "наведи порядок"), or when the inventory MCP agent_queue lists cards a person handed over.
---

# Enrich an inventory card

A person made the card in a hurry: phone photos, a name, maybe a line of text. Turn it into a card they can trust: exact model, official documents, the right type, every field filled. Their photos and words stay.

The inventory is reached through its MCP tools only. No scripts of your own against it, no reading its code or database, no files left in its repo.

## Which cards

- `agent_queue` → cards a person ticked «Передать агенту». Do those first.
- Asked about something specific → `search`, then `get_item`.

## Per card

1. **`get_item`, then look at the owner's photos**: download each photo URL and open the file. Chip markings, silkscreen, labels, connectors. The name may be a guess; the photo is the fact. Read markings letter by letter and search them («TTP223 BA6», «AMS1117-3.3»). No chip or label (tools, cases, cables) → read what is stamped on the metal: brand, scales, sizes. The shape says what it does: a row of holes with a mm scale is a wire stripper.
2. **Identify maker and exact model.** Every value you write is either on the photo, in a document, or marked as a guess: «вероятно PH0 — сверьте по винту». Can't tell even that → leave the field empty. Describe this thing, not what similar boards usually have (no trimmer on the photo → no trimmer in the text).
3. **Right type?** `card_template()` lists types with hints; take the type whose hint names this thing, the narrowest one if two fit. Wrong type → `update_item(type=...)`: **all** the new type's required fields go in the same call (one missing and the whole call is refused, the error lists which); the old type's values stay stored, hidden, nothing is lost. The right type needs a photo and the owner gave none → keep the current type, fill what it has, and ask for a photo in the reply. No type fits (an assembled device: a power bank, a charging station) → the nearest one, and say so in the reply.
4. **Every field of the type, as its hint says** (`card_template(type)`). Values from the datasheet, not from how the thing is wired: a touch sensor with a high/low output is digital, not analog.
5. **The owner's text stays word for word.** Add your part under it, never rewrite or shorten theirs. A note addressed to the agent («агент, найди по фото…») is a task, not content: do it, then it may go. Asked to fix the name → `name=`, keeping their words where they fit. Write in the language of the field labels (`card_template`), plainly, no jargon.
6. **Documents.** Datasheet or manual PDF from the maker's own site first. Check the link before attaching: `curl -sIL <url>` → final `200` and `application/pdf`. Open the PDF and check it covers your exact variant (TTP223-BA6 ≠ TTP223-HA6). Only a mirror has it → attach it and name the source in the file name («TTP223 datasheet (lcsc).pdf»). Pages behind a login (Mouser, Digi-Key, LCSC product pages) are not files. No PDF anywhere → the best page goes in the datasheet link field, and say so in the reply.
7. **Photos.** A clean product photo may be added after the owner's, never instead: send get_item's photo list back as it is, plus new direct image URLs (the maker's product page, or the page your PDF came from).
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
| Wrote a size or spec nobody printed («PH0», «аналоговый») | «вероятно …, сверьте» or an empty field |
| Took a stripper for side cutters | Look at what is stamped on the jaws |
| Linked a search page or a login-walled page as the datasheet | A direct PDF, checked with `curl -sIL` |
| Attached the datasheet of a sibling variant | Check the variant inside the PDF |
| Replaced the owner's photo with a stock one | Keep theirs first, add after |
| Wrote a script to reach the inventory | The MCP tools only |
