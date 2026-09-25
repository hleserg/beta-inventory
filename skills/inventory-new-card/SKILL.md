---
name: inventory-new-card
description: Use when asked to add a part, module, tool or other thing to the home inventory (beta-inventory, the `inventory` MCP server), to write or improve an item card, or to fill an empty box.
---

# New inventory card

The `inventory` MCP server holds the cards. Its tools describe themselves. This skill covers the order to use them in and what a good card needs.

1. **`search`**: look up the name, then what the thing is ("датчик тока"). If it is found, don't create a new card: use `change_stock` to add the pieces and `update_item` for what the card lacks.
2. **`card_template()`**: pick the type. Then call **`card_template(type)`** to get the fields. Every field has a `hint` that says what goes in it. For the description, the hint lists the sections in order. Write each of those sections.
3. **Web**: look up the datasheet, pinout and a guide, and one clear photo of the board. For the photo, pass a *direct* image link: it ends in `.jpg`/`.png` and opens as a picture, not as a shop page. For a household thing (category «Дом») look up the manual and care instead: descaling, filters, consumables — the field hints say what.
4. **`create_item`**: pass `agent` = your name as the user knows you (Claude, Codex). With `box_id` and `qty` it also puts the pieces into the box.
5. **Reply**: give the card link and say what you are unsure of.

## Common mistakes

| Mistake | Fix |
|---|---|
| Error "not a picture" | The link points to a page. Open the image itself and copy its address. |
| Words like "high-side", "шунт", "pull-up" left unexplained | Explain them in plain words, or leave them out. |
| The pinout comes from another board revision | Add «сверьте с шелкографией». |
| «Другие названия» is empty | Fill in how people call it in Russian and English, plus common typos. Search finds the card by these names. |
