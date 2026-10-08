# Phase 5a: deferred follow-ups

Minor findings from the per-task reviews and the final whole-branch review, triaged "can wait" before merge. Grouped by the task that found them.

- **Task 1:** template id test uses subset `want <= got <= {...}`; exact per-template equality would be tighter.
- **Task 1:** no test with two same-name entities guarding id ordering/_FACT_KEY.
- **Task 1:** mid-file `# noqa: E402` import in tests/test_query_templates.py.
- **Task 2:** no test of the 150-edge cap with true total; no test for `call is None` edges event, raising check closing its step, check-dropped-all-claims.
- **Task 2:** hook-neutrality test only runs with check=None; lore_search's on_event param untyped (could be OnEvent).
- **Task 3:** websocket scopes bypass the guard (no websocket routes exist); non-UTF-8 header decode → 500 not 403 (use latin-1); imports private _check_loopback from lore.mcp.server.
- **Task 3:** no tests for a missing Host, the [::1] Host form, Origin: null, websocket scopes.
- **Task 4:** no alias-id test for entity_by_id/subgraph_by_id; subgraph_by_id drops the depth-clamp note; exists/canonicalise duplicated (a _resolve_id helper would fix it); /api/subgraph name/400/422 and not_found paths untested; test_mcp_details assertion nearly vacuous; /api/mcp port probe blocks up to 0.2s in a sync handler.
- **Task 5:** the per-app asyncio.Lock is loop-bound (only matters under a TestClient with no `with`); content-type check case-sensitive; 400 detail is pydantic's verbose message; event.pop mutates the emitter's dict; test comment "what Starlette does on disconnect" inaccurate.
- **Task 5:** the ask.py module docstring says "Starlette closes this generator"; on a real disconnect the task is cancelled.
- **Task 6:** `port or config.web.port` treats 0 as unset; config loads before the host check; the uvicorn stub doesn't assert that dev/log_level reach create_app.
- **Task 7:** postbuild uses require in node -e under "type":"module" (works, fragile); @keyframes pulse animates r (copied from the mock); tsconfig includes e2e before it exists; npm audit dev-dep advisories.
- **Task 8:** multi-line data joined without "\n"; "data:" without a space ignored; one malformed frame throws and drops sibling events; trailing frame without "\n\n" dropped at stream end; reader not cancelled on early exit; error mapping reads only .detail (403 body/500/422-array cases); tests miss abort-silence, no-interrupted-after-answer, CRLF, multi-line, trailing frame, 403 shape; SubgraphLink lacks optional citations.
- **Task 8:** parseSse returns `rest` already normalised, so a CRLF split across chunks ("event: parts\r" | "\ndata: ...") becomes "\n" + "\n" — a false frame boundary that loses the event type. Latent: our backend sends LF only. Fix: hold back a trailing "\r" in rest instead of normalising it.
- **Task 9:** no tests for startedAt across parts, answer settling running→done, subQuestions kept when parts came, the non-interrupted error path; synthetic.ts types name/state as any; loadHistory has no per-item shape check; the seeds step flickers done→running→done between parts (watch in Task 13).
- **Task 10:** a dispatch throw rejects the chain and drops later events; an interrupted sleep never resolves after stop; replay with reduced motion still waits gaps; expansion links have books: [] so never dim; canonical root id may differ from the clicked id (check in Task 13).
- **Task 10:** the repulsion loop doesn't clamp displacement or bound the canvas; spacing with many close-hashed seeds is only tested for 8.
- **Task 11:** toLowerCase can change length (rare Unicode); segment word count by split(" ") vs the backend's \w tokens; the for-await doesn't break on abort; saveHistory runs inside a setState updater; replay doesn't update last.current (retry after replay re-asks the older question); `current` set to an id with no history entry on error.
- **Task 11:** fold's toLowerCase isn't strictly one-for-one for rare Unicode (e.g. İ).
- **Task 12:** tablist lacks aria-controls/tabpanel/arrow keys; document-level Escape not scoped; "All books (0)" when nothing is graphed; Snip's timeout not cleared; re-picking the same file doesn't fire onChange; Select all/Clear ignore the filter; Snip would sit better in its own file; no tests for SettingsMenu gating, Escape/outside-click, clipboard fallback, AddBookDialog.
- **Task 12:** `lore ingest -- <quoted>` would stop a name like `-x.epub` being read as an option; popover containers lack role=dialog/label; no focus trap in AddBookDialog; .drop declared twice in theme.css; no test for books-popover Escape focus return.
- **Task 13:** stale expansions render for one frame on a new question (reset in the render branch, or key by question); a late expand lands in a new question or on another node's card (guard with a live/id token, reset `expanding` on id change); Claims' open index carries to the next answer (key Claims by question); quota not refreshed after an interrupted stream.
- **Task 13:** budget no-retry test passes on empty render; no tests for the partial/refusal lines, the Snip command, replay; canvas mouse-only, card aria-label generic; useRef(new Graph) built every render; dead hasDirectedEdge guard; Snip imported from SettingsMenu; refusal keyed on claims.length not mode; visual state via mergeNodeAttributes rather than reducers (brief-mandated).
- **Task 13:** if the canvas remounts while pinned with fitRequest 0, the first paint pins an empty bbox (guard with graph.order > 0); camera zoom/pan not reset on a new question; the EntityCard rewrite dropped the aliases line.
- **Task 14:** fixed waitForTimeout sleeps; 180s test timeout tight against the 150s answer wait (use 240s); LORE_STATE hook undocumented; non-null `!` on the .sigma-labels locator gives a cryptic failure.
- **Final:** the multi-part check detail reflects only the last part's check reply; a failed /api/entities batch isn't retried (those nodes stay Other for that ask); the seed ring and Character fill are both blue when there's one part; the e2e's 5s first-step wait is tight with a cold Ollama.

Visual polish still open from the screenshot checks: hub expansions can flood the graph (cap them and place new nodes outward); node labels can overlap; on phones the entity card covers most of the small graph; with one part, the seed ring and the Character fill are both blue.

After merging into answer-quality or main, run the answer-quality dev set once: the template ids now take part in RETURN DISTINCT and EdgeRow equality, which can rarely change evidence.
