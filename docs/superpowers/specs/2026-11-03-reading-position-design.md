# Reading position: spoiler-safe exploring and answers

**Status:** draft, 2026-11-03. The decisions below were taken in chat (the recommended options); the user has approved them.

**Goal.** A reader tells LORE where they are, for example "chapter 9 of book 2". From then on, everything LORE shows stays within what they have read: the graph, the entity panels, the timelines, the relationships and **answers**. Exploring a long series then never spoils what lies ahead.

## Decisions

1. **Scope B.** The reading position limits the graph, panels, timeline and edge views, and it limits answers too.
2. **Stored on the server, per world.** The position also applies to MCP. The current app has a single user; once accounts exist, the key gains a user id.
3. **Off by default.** Everything stays visible until the reader turns the reading position on.

## The position

- **Shape.** A position is `(world_id, book_id, chapter)`: the reader is in `book_id` and has finished every chapter before `chapter`. The current chapter counts as read, so set the position to the chapter you have finished.
- **Which books count as read:**
  - Every book earlier in the same series, by `series_index`, counts as fully read.
  - Books whose order against the current book is unknown (different series in one world, or no index) have their own "read" toggle. LORE never guesses their order (story-order rule).
- **Storage.** A new table, `reading_positions(world_id PK, book_id, chapter, read_books JSON, enabled, updated_at)`, added by a migration in the existing style.

## What gets filtered

One predicate decides visibility everywhere: **a chunk is visible when its book is fully read, or when it is the current book and its chapter is at or before `chapter`.** Every other rule below derives from it.

- **Retrieval for answers.** This covers vector, keyword, graph evidence, timeline beats and codex statements. Only visible chunks enter the evidence. Seeds are limited to entities with at least one visible mention.
- **Graph.** REL edges are shown only if their evidence chunk is visible. Nodes are shown only if they have a visible mention or a visible edge. The overview, subgraph, expand, path and search all apply this.
- **Entity panel.**
  - Profile statements, timeline beats and relationships are filtered by their chunk.
  - The codex one-line **summary** was written over the whole story, so it is **hidden** while a position is on. The panel shows the filtered statements instead.
  - Counts reflect only what is visible.
- **Answers.**
  - The writer, the check and everything above see only visible evidence.
  - Deep plans draw candidates only from visible material.
  - The answer carries `position_note`, for example "Answered from what you've read: up to chapter 9 of <book>".
- **Story order.** Unchanged within the visible set.

## API and UI

- **API:**
  - `GET` and `PUT /api/reading-position?world=` read and set the position.
  - Every read endpoint and `/api/ask` apply the position by default.
  - `?spoilers=1` bypasses it for one request; the UI never sends this unless the user explicitly reveals.
- **MCP.** `lore_search` and the graph tools apply the stored position by default. An optional `ignore_reading_position` argument bypasses it.
- **UI header.** Next to the world selector there is a **Reading position** control:
  - an on/off switch;
  - a book picker (series order, with "read" toggles for books of unknown order);
  - a chapter slider for the current book.

  While the position is on, a small persistent badge says "Up to <book> ch. N".
- **Graph.** When the slider moves, the graph refetches, and relationships appear or disappear in story order. Consecutive moves are debounced, with no animation spam.
- **Answer.** The answer shows the position note under the short answer.

## Testing

All tests use synthetic worlds with invented names.

- **Predicate.** Same book before and after the position; an earlier series book; unknown order with the read toggle; disabled.
- **Each endpoint** hides beyond-position chunks, edges, entities, beats and codex statements, and hides the codex summary while the position is on.
- **Answers.**
  - Evidence only from visible chunks, with a golden check that a beyond-position fact cannot appear.
  - The position note is present.
  - With the position off, the result is byte-identical to before (golden).
- **MCP.** The default applies, and the bypass argument works.
- **UI (Vitest).** The control persists through the API; the slider refetches; the badge shows; the note renders.

## Risks

- **Entity names as spoilers.** A name visible early can later turn out to be an alias, for example one character secretly being another. Merged entities show their merged name, which can spoil. Mitigation: while a position is on, display an entity under its **earliest visible alias**, not its canonical name.
- **Codex summary.** It is hidden while a position is on (above). A position-aware summary would need a Gemini request per entity and position, so it is out of scope.
- **Cost.** Filtering is local, so answers cost no extra requests.
