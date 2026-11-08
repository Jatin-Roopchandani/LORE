# Release step 7: the homepage, plus privacy and terms

The design is finished and approved: `docs/design/homepage.html`, a self-contained mockup. Build it into the app as the public front door.
- **What to build:** it replaces the placeholder `/` in multi mode (`lore/web/app.py`, around "the public homepage (a placeholder until step 7)") and the placeholder `/privacy` and `/terms` (`web/src/pages/Legal.tsx`).
- **Spec:** `docs/superpowers/specs/2026-11-03-release-design.md` §8 and §8a.
- **What exists already:** the waitlist API (`POST /api/waitlist`, step 2), the sign-in pages (`web/src/pages/`, `Router.tsx`) and the request id.

**Constraints**
- Work in `/home/jatin/LORE-home` on branch `homepage` (a worktree off `release`), using `/home/jatin/LORE-release/.venv` for Python. Run `npm install` in `web/` if `node_modules` is missing. TDD where it applies.
- **Run targeted tests only.** A Gemini eval runs elsewhere on this machine. Run vitest on the touched files, `tsc --noEmit`, one `npm run build`, and the targeted Python tests for the routes you touch.
- Git email jatinroopchandani3007@gmail.com. NO trailers: no Co-Authored-By, no Claude-Session. Stage files by path. Do not push; do not merge. The controller merges `homepage` into `release`.
- **Another agent is hardening `release` right now** (rate limits, CSP, admin controls) in `/home/jatin/LORE-release`. Don't touch that worktree. Keep your changes to:
  - the homepage, privacy and terms components;
  - their CSS and strings;
  - the three routes;
  - the waitlist call.

  That keeps the merge small. Note: **a CSP is coming** (`default-src 'self'`, no inline scripts, no third-party origins), so the build must not rely on inline `<script>`, inline event handlers or external fonts. Use the app's bundled `@fontsource` fonts (Newsreader, Public Sans, JetBrains Mono), not Google Fonts.
- Never use `pkill -f` or `pgrep -f`. Don't start or stop any `lore ui`. Never print API keys.

## Task 1: the homepage as a React route

- **A faithful port.** Port `docs/design/homepage.html` into React components (for example `web/src/pages/home/`), matching it closely:
  - the sticky nav: transparent over the hero, solid once scrolled;
  - the full-bleed dark hero with the animated chapters 1–2 graph and its answer card;
  - the pinned five-step "How LORE answers a question" scene, with the card building up and the step-5 written answer;
  - the pinned feature glide with its 5 SVG icon cards;
  - the pinned beta scene with steps lighting up beside the form;
  - the footer.
- **Copy and colours:** copy every string into `messages.ts` (a `HOME` block) word for word. Keep the mockup's tokens: the night sections are always dark, and the lower sections follow `prefers-color-scheme`. There is no theme switcher.
- **Scroll scenes:** a small hook (`usePinProgress(ref)`) returns 0–1 progress through a scene, using one passive scroll listener and `requestAnimationFrame` throttling.
  - Respect `prefers-reduced-motion`: no animation; steps change without transitions.
  - On narrow screens the beta scene falls back to normal flow, as in the mockup.
- **The waitlist form** posts to `POST /api/waitlist` with `{email, reading, consent}` and the honeypot field.
  - It shows the mockup's success state ("You're on the list ✓"); `202` always means success.
  - It shows friendly errors for 429, network failure and an invalid email.
  - The nav's "Request an invite" scrolls to the form and focuses the email field.
- **Sign in:** every "Sign in" button links to `/signin`. "Request an invite" goes to the form.
- **Accessibility:**
  - semantic landmarks and headings in order;
  - the pinned scenes' changing content in an `aria-live="polite"` region;
  - the step dots as buttons with labels;
  - visible focus rings;
  - keyboard: Tab reaches every control; the dots jump between steps.
- **Speed:**
  - the homepage is its own lazy-loaded chunk, so it doesn't pull in the app (Sigma and so on);
  - its SVGs are inline;
  - no images are needed;
  - aim for under 60 KB gzipped of JS for the homepage chunk, and report the size.

## Task 2: routes

- **In multi mode:**
  - `/` serves the homepage to everyone. A signed-in user sees "Open LORE" (→ `/app`) in place of Sign in and Request an invite.
  - `/privacy` and `/terms` serve the real pages from Task 3.
  - `<title>` is "LORE · Ask your books", with a meta description and Open Graph tags: title, description, and `og:image` as a static SVG or PNG you generate from the hero (no external services).
- **In local mode** `/` stays the app, exactly as today.

## Task 3: privacy and terms

Short, plain-English pages in the homepage's visual style, sharing the nav and footer. They must be accurate to how LORE actually works. Read the step reports to confirm each point.

**Privacy:**
- **What we store:**
  - account email and name;
  - sign-in identities;
  - uploaded books and everything derived from them, in the user's own library;
  - chats and answers;
  - question traces for 30 days;
  - usage counts;
  - the AI key, encrypted;
  - waitlist entries.
- **Where it goes:** book passages and questions are sent to the user's AI provider (Gemini today) under the user's own key. Embeddings are made on our server. Nothing is sold or shared, and no ads or third-party analytics are used.
- **Cookies:** a session cookie and a CSRF cookie only.
- **Retention and deletion:** "Delete my account" removes everything (step 6). Traces expire after 30 days.
- **Contact:** a placeholder `privacy@jrvn.site`. Mark it clearly with a TODO for the user to confirm.

**Terms:**
- **The beta:** it is invite-only, provided as is, and may change or stop.
- **Your books:** upload only books you own, for personal use. You're responsible for your AI key and its usage.
- **Fair use:** no abuse or scraping.
- **Accounts:** we can disable accounts that misuse the service.
- **Governing details:** a TODO for the user.

Add a "Last updated" date. Keep both pages short and readable; they're not legal boilerplate. Put every TODO the user must fill in one list at the top of your report.

## Tests

- **Vitest:**
  - the homepage renders all sections and nav links;
  - the waitlist submit sends the right body, and shows the success state and the error states;
  - the honeypot is sent;
  - the signed-in view shows "Open LORE";
  - the pin hook maps scroll to progress (mock the geometry);
  - reduced motion;
  - privacy and terms render.
- **Python:** in multi mode, `/`, `/privacy` and `/terms` return the app shell with the right `<title>` and meta tags; local mode's `/` is unchanged.
- **Visual check:** with the built app served by `lore ui` from this worktree, you would need to start a UI, so instead render the built `index.html` and route in a headless browser against a temporary static server on a free port. Check:
  - the three pinned scenes hold still while their content advances (assert element positions, as in the mockup's checks);
  - no horizontal overflow at 390 px;
  - no console errors.

  Playwright is available at `/home/jatin/LORE-prose/web/node_modules/playwright`.

## Finish

Write `.superpowers/sdd/release-7-report.md` in this worktree (gitignored), covering:
- the SHAs;
- the TODOs for the user;
- the bundle size;
- test counts;
- what you checked visually;
- concerns.
