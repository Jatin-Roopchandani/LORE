# Graph explorer: implementation plan

**Goal.** The graph becomes a practical way to explore and understand a story, not a decoration. A reader can:
- return to the library view at any time;
- click any entity and get a verified profile, a timeline and its relationships, each with its evidence;
- click any edge and see what connects the two ends;
- find an entity and see how two entities are connected;
- ask a well-formed follow-up question with one click.

All of this reads stored data (graph, codex, beats, chunks). Browsing makes **no Gemini requests**; only the one-click questions do, and they go through the normal ask path.

The reading-position (spoiler-safe) slider is out of scope here. It gets its own spec: `docs/superpowers/specs/2026-11-03-reading-position-design.md`.

**Constraints for every task.**
- Work in `/home/jatin/LORE-prose` (branch `prose-answers`) with `.venv/bin/python`; the frontend lives in `web/`.
- Keep everything general. Nothing may be specific to the current library, and tests use invented names.
- Use TDD. No Gemini or network calls (`tests/conftest.py` blocks them).
- Every endpoint takes `world` and is scoped to it (`lore/query/scope.py`).
- Commits: git email jatinroopchandani3007@gmail.com, NO trailers, stage files by path, do not push.
- Never use `pkill -f`/`pgrep -f`. Do not stop `lore ui` on :8766. Do not touch `~/LORE-r6` or `/home/jatin/LORE/data`.
- All strings go in `messages.ts`. Accessibility: real buttons and links, labelled controls, keyboard access to every panel action.

## Task 1: backend read APIs

All under `lore/web/app.py`, with shared helpers in a new `lore/query/explore.py`.

- **`GET /api/entity/{id}/profile?world=`** returns `{id, name, kind, books, summary, aspects: [{aspect, statements: [{text, quote, chunk_id, book_id, chapter}]}], counts: {beats, relations, chapters}}`.
  - Data comes from the codex: `codex_summaries` and `codex_entries`.
  - If the entity has no codex entry, `summary` is null and `aspects` is empty; the UI then shows the raw facts below instead.
- **`GET /api/entity/{id}/timeline?world=`** returns `{beats: [{kind, text, quote, chunk_id, book_id, book_title, chapter, position_label, others: [{id, name}]}]}`.
  - Beats are in story order (`position.order_passages` semantics).
  - When the books' relative order is unknown, it also returns `order_note`.
- **`GET /api/entity/{id}/relations?world=`** returns `{relations: [{other: {id, name, kind}, predicates: [{predicate, count}], evidence: [{quote, chunk_id, book_id, chapter}] (up to 3), first_chapter, last_chapter}]}`.
  - Ordered by how often each pair is mentioned.
  - The data is the REL edges, which already carry evidence.
- **`GET /api/edge?a=&b=&world=`** returns every relationship between two entities, in both directions, with the predicate, the evidence quotes and the chapters, in story order.
- **`GET /api/search?q=&world=&limit=`** searches entity names and aliases, prefix and fuzzy, ranked by significance, and returns `{id, name, kind, books}`.
- **`GET /api/path?a=&b=&world=&max=4`** finds the shortest path between two entities through REL edges, returning `{nodes, hops: [{from, to, predicates, evidence}]}`.
  - The search is capped at `max` hops, with a time cap.
  - If no path is found, it returns an empty list.
- **`GET /api/passage/{chunk_id}`** returns the passage text for a quote chip, if no such endpoint exists yet.

Tests run on real Kuzu, SQLite and Lance in tmp, using the synthetic world fixtures. They cover:
- world scoping: an entity from another world returns not found;
- story order;
- profile without a codex entry;
- a path that exists and one that doesn't;
- search ranking.

## Task 2: graph pane modes and the entity panel (needs 1)

- **Mode toggle.** The graph pane header gets a segmented toggle, **This answer | Library**.
  - Library shows the overview.
  - The answer view is kept, so toggling back restores it; the answer text stays untouched.
  - Before any question, only Library is shown.
  - Switching world selects Library.
- **Entity panel.** It replaces the thin EntityCard and opens on node click. It has tabs (or stacked sections, whichever fits the pane width):
  - **Profile:** the summary plus aspects, each statement with a quote chip that opens the passage.
  - **Timeline:** beats grouped by book, then chapter, each with its kind icon or label and a quote chip. Clicking another entity named in a beat opens that entity.
  - **Relationships:** the relations list. Clicking one opens the edge view (Task 3) and highlights that edge.
  - **Ask:** one-click questions, built general and scoped to the current world:
    - "Deep dive on {name}" (depth deep);
    - "How does {name} change across the story?";
    - "What role does {name} play?";
    - for a relationship, "How does the relationship between {a} and {b} develop?".

    Each button fills the ask box and submits. Its tooltip notes that it uses the daily allowance.
- **Selection.** Selecting an entity in the panel centres and highlights it in the graph.

## Task 3: edge view, search, path and filters (needs 1; can run in parallel with 2 once its panel shell exists)

- **Edge view.** Clicking an edge, or a relationship in the panel, opens a panel showing both ends, every predicate, and the evidence quotes with chips and chapters, in story order. It includes the "relationship develop" one-click question.
- **Search.** A box at the top of the graph pane with autocomplete from `/api/search`. Choosing a result opens its entity panel and centres it, adding it to the graph if it isn't there.
- **Connect.** In the entity panel, "Connect to…" plus a search picks a second entity. `/api/path` then highlights the path in the graph and lists each hop with its evidence. A "Clear path" button removes it.
- **Filters.** Kind chips (Character, Place, Faction, Object, Concept, Event) and book chips hide the nodes and edges they exclude. Filters persist per browser, extending the existing hidden-books logic.

## Task 4: review and finish

- One Sonnet review of the whole feature.
- Run the full suite and the web build.
- Restart the UI.
