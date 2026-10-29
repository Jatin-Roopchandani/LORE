# Worlds: keep unrelated books apart, let shared universes merge

**Status:** draft, 2026-10-29. The decisions below were made in chat; the user still has to review this spec.

**Goal.** A user can upload many series. Two unrelated series may have characters with the same name, and those characters must never merge. Some different series share one universe, like several series set in the same fictional cosmology, and those *should* share characters. Every book belongs to a **world**, chosen by the user when the book is added. If the user picks the wrong world, they can move the book.

**Done when:**
- Entities merge across books only within one world.
- Adding a book (UI or CLI) asks which world it goes in, with a sensible default the user confirms.
- "Move to world…" in the Library panel moves a book with no Gemini requests. Afterwards the book's entities exist only in the new world, and the old world is as if the book had never been there.
- Questions are answered within the selected world. An "All worlds" mode answers across worlds without merging their entities.
- The existing library is migrated to worlds automatically.

**Hard requirement.** Nothing may be specific to the books in today's library. Worlds are user data. The code only knows "world", "series" and "book".

---

## 1. Today

All books share one graph. When the resolver loads a book, it fuses a distinctive name that already exists from another book into that existing entity. That is how one detective became one person across two novels: `loader.py`, step 2 (homonyms) and step 2b (cross-book name variants such as `watson` → `john watson`). Generic names like "the Captain" are already kept apart. Distinctive names are not, so two unrelated "Elena Vasquez" characters would become one entity carrying both books' edges.

## 2. Decisions (made in chat)

1. **Worlds live inside one library (option B), not as separate databases.**
   - A world contains one or more series. A series still orders its books (`books.series` and `series_index`, from the story-order work).
   - A book with no series is a standalone in its world.
2. **The user picks the world when adding a book.** We suggest a default; the user confirms. We never infer a world silently: the same author does not imply the same world, and different series can share one.
3. **Undo is "move to world".** It costs nothing in Gemini requests, because extraction, merge answers, speakers and beats are all cached.

## 3. Data

- **New table:** `worlds(id, name UNIQUE, created_at)`.
- **Books:** `books.world_id` (NOT NULL after migration). Added by a migration in the existing style.
- **Default world.** The migration creates one world per existing series name, plus one world per book that has no series, named after the book. It assigns every book accordingly. For today's library this gives one world for the two-book series and one world for the standalone novel. The user can rename worlds or merge them with Move.
- **Graph.** An entity is already tied to books through `Entity.books`. Its world is the world of those books. An invariant is enforced by the loader and tested: all of an entity's books are in one world.

## 4. Resolution scoped to a world

The resolver's cross-book steps (homonym fusion and cross-book variants) consider only **persisted entities whose books are in the same world** as the book being loaded.
- An entity from another world is invisible to the resolver. A same-named candidate takes the next free id slot (step 3) and becomes a separate node.
- Same-book resolution is unchanged.
- **Adjudication and disambiguation questions are cached by their prompt.** A world-scoped load asks only about candidates in its world. New questions can appear the first time a world boundary changes what gets compared. Each costs one request and is cached.

## 5. Adding a book

**UI.** The "Add book" dialog shows a **World** dropdown: existing worlds plus "New world…", which asks for a name. The default is chosen in this order:
1. if the EPUB's series metadata matches a series that already exists in a world, that world;
2. else if the EPUB has a series, "New world", named after the series;
3. else "New world", named after the book title.

The dialog produces the CLI command, as it does today, now with `--world`. Uploading straight from the browser is still Phase 5b.

**CLI.** `lore ingest <file> --world "<name>"`. `--world` creates the world if it is missing, after asking for confirmation unless `--yes` is given. Without `--world`, the CLI prints the default it would use and asks; `--yes` accepts the default.

## 6. Moving a book (undo)

Run it with `lore book move <book> --world "<name>"`, or with **Move to world…** in the Library panel. It runs as a job through the resumable pipeline, and its stages run in order.

1. **remove.** Take the book out of the graph and indexes:
   - its chunk nodes and MENTIONS edges;
   - its REL edges, identified by the edge's book;
   - its Lance chunk vectors and its beats (rows and vectors).

   Then, for each entity that lists this book:
   - drop the book from `Entity.books`;
   - delete the entity if no books remain;
   - otherwise keep the entity for its other books.

   Alias nodes and ALIAS_OF edges that only this book's load created are removed the same way. Merges this book caused into another book's entity are undone by that same removal, because the other book's entity simply loses this book.
2. **reassign.** Set `books.world_id`.
3. **load.** The normal world-scoped load of the book into the new world. It uses cached extraction and cached answers.
4. **beats.** Rebuild from the cache, then relink.

This is idempotent and resumable: a move that is interrupted continues from its stage. The remove stage is also exposed on its own as `lore book remove-from-graph <book>`, which keeps the chunks and caches. This lays groundwork for later "delete book".

## 7. Questions scoped to a world

- **UI.** The header's books dropdown becomes a **world selector**, and the chosen world is remembered per browser. It offers each world and "All worlds".
- **API and MCP.** `lore_search` and the web ask endpoint accept an optional `world` (id or name). With no world given:
  - if there is one world, it is used;
  - otherwise the default world is the most recently used one in the UI;
  - MCP callers get "all worlds".
- **Retrieval.** Seeds, select candidates, graph evidence, vector and keyword candidates, the timeline and the decomposition's book list are all filtered to the world's books. The existing book scoping applies within the world.
- **All worlds.** Each world is searched as above and the evidence is pooled. Entities are never merged across worlds, so two same-named characters from different worlds appear as different people. The answer names the world (or book) when facts come from more than one world, using the same rule as naming books today.

### 7.1 The graph follows the world

Switching worlds switches everything the UI shows:
- **The graph pane:** the library overview, the per-answer subgraph, node expansion and the entity cards show only the selected world's entities and edges.
- **Example questions and history:** history items record their world. Replaying one switches to that world.
- **"All worlds":** shows every world's graph together. Each world is a separate cluster, never joined by merged nodes, and nodes are marked with their world (in the label or colour, plus a legend). Two same-named characters from different worlds appear as two nodes.

The graph endpoints (`/api/overview`, `/api/subgraph`, `/api/entity`, expand) take the same optional `world` as ask.

## 8. Library panel

- Books are grouped by world, and each world header shows its series.
- Each book has a **Move to world…** action: pick a world or create one, confirm, and the job runs with the existing progress and Continue UI.
- Worlds can be renamed. An empty world can be deleted.

## 9. Testing

All tests use invented worlds, books and names.
- **Resolution.** Same-named characters in books from two worlds stay separate. Within one world they merge, as today. The one-world invariant holds.
- **Migration.** Worlds are created from series and standalone books, and every book gets a world.
- **Remove.** After removing a book:
  - entities that belonged only to that book are gone;
  - shared entities keep their other books;
  - its edges, vectors and beats are gone;
  - running the remove a second time changes nothing.
- **Move round-trip.** Move a book from world A to world B and back. The graph equals the original in entities, edges and books per entity, and the Gemini fake records zero requests.
- **Scoping.** A question in world A never returns world B's passages, entities or beats. "All worlds" keeps same-named entities distinct.
- **CLI.** `--world` creates the world and confirms; without it, the default is offered; `book move` works.
- **UI (Vitest).** The world dropdown and its default in Add book; the world selector; Move to world… starts a job.
- **The pre-registered eval sets keep working.** They run in the world holding their books.

## 10. Risks

- **Remove completeness.** Anything a load writes that remove misses would leak a book into a world it left. Mitigation: the move round-trip test compares the full graph, not samples.
- **Cost on the first boundary change.** Resolution can ask new merge questions once world scoping changes which candidates are compared. Each is one cached request, and the job's quota handling covers it.
- **Users wanting two worlds merged later.** Moving books one by one is enough for now. A "merge worlds" action can come later.
