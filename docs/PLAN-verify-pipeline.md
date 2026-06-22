# Verify-pipeline improvement plan

Owner: Sujith · Author: Claude · Status: drafting → ready to ship slices

## Why this exists

The Ojas verify pipeline (`npm run verify` in the fullstack template) is
already substantial — 5 guards, ~2,500 lines of Node, OpenAPI-driven API
smoke, Playwright BFS walk, blank-screen detection, dynamic-route
discovery, refresh-survival checks, API-data-must-appear-in-DOM checks.
But it has three real gaps that cause the user to lose trust:

1. **No visibility into what was actually verified.** When the user
   sees a green "verify:browser" exit, they have to take it on faith
   that every endpoint was hit, every route was walked, every screen
   rendered. The output is pass/fail; the evidence isn't surfaced.
2. **No endpoint↔UI mapping check.** `verify-api` exercises endpoints
   in isolation. `verify-browser` walks UI routes in isolation.
   Nothing proves that every (method, path) in `/openapi.json` is
   reachable from a UI screen that displays the result. A documented
   endpoint with no UI caller is an orphan — half-built feature, dead
   endpoint, or forgotten screen.
3. **Verify suite uses a temp DB, not the live one.** Both
   `verify-api` and `verify-browser` set
   `DATABASE_URL=sqlite:///.../verify.db` and run against it. The
   file is in `node_modules/.ojas-verify/` and gets thrown away.
   The live DB at `backend/data/app.db` is a separate file, only
   populated by the backend's auto-seed (`_seed_if_empty()` runs in
   `lifespan`). Auto-seed works in practice for the current
   `insta` and `gallery` builds (confirmed: 4 stores / 15 categories
   / 42 products in insta, 4 albums / 28 album_photos / 31 photos
   in gallery), but the verify suite never proves the live DB
   actually has data — it only proves the temp DB has it.

## Current state (what's there)

```
verify:deps   check-deps.mjs       241L  compile-time sanity, catches two-React hoisting
verify:radix  verify-radix.mjs     164L  Dialog/Sheet/Popover trigger-in-provider check
build         vite                 -     TS + bundle
verify:render verify-render.mjs    199L  esbuild + react-dom/server, catches two-React at runtime
verify:api    verify-api.mjs       691L  OpenAPI walk, body synthesis, POST→GET→DELETE round-trip
verify:browser verify-browser.mjs  959L  Playwright BFS, blank-screen, API-data-in-DOM, refresh, dynamic routes
```

`npm run verify` chains: `verify:deps && verify:radix && build && verify:render && verify:api && verify:browser`

The `verify:integration` check (Change A below) is the only new script
proposed; everything else is wiring + visibility.

## Proposed changes

### Change A — `verify:integration` script (the headline feature)

A new walker, after `verify:api` and before `verify:browser`. For every
(method, path) in `/openapi.json`:

1. **Find the UI caller.** Grep `frontend/src/**/*.{ts,tsx}` for
   `fetch("<path>"` and `api.{verb}("<path>"` patterns. Use simple
   regex; full AST parsing is overkill for a smoke gate.
2. **Verify the response is rendered.** For the call site, confirm
   the response shape's string fields are referenced in JSX (one
   field from a sample response must appear in the component's
   text). If no caller, FAIL with
   `orphan endpoint: POST /api/cart/items — no UI caller found`.
3. **Inverse check.** Also list every `fetch("/api/...")` and
   `api.{verb}(...)` call in `src/`. If a UI makes an API call
   that's NOT in the OpenAPI spec, FAIL with
   `undocumented endpoint: GET /api/foo called from src/pages/X.tsx
   but not in /openapi.json`. Catches the agent writing ad-hoc
   fetch calls that bypass the documented surface.
4. **Sample-call execution.** For each (method, path), fetch a
   single sample response from the live backend, then confirm a
   representative string field appears in the rendered HTML of the
   page that should display it (look up the page via the
   `<Route path=...>` declarations in `App.tsx` and pick the first
   route that imports the call site).

Output: a per-endpoint table — `PASS / FAIL / SKIP, caller, sample
field, page it appears on`. Persist to
`node_modules/.ojas-verify/integration-report.json` so the deploy
reporter can read it and surface "X of Y endpoints proven integrated"
on the deploy success card.

### Change B — Verify report surfaced in the deploy UI

After every `npm run verify` run, the templates already print a
human-readable summary to stdout. The new requirement: also write
`verify-report.json` to `<project>/.ojas-verify/verify-report.json`
with structured evidence:

```json
{
  "passed_at": "2026-06-22T...",
  "guards": {
    "deps":    {"status": "pass", "ms": 1200},
    "radix":   {"status": "pass", "violations": 0},
    "render":  {"status": "pass", "ms": 4500, "html_len": 12345},
    "api":     {"status": "pass", "exercised": 31, "passed": 28, "skipped": 3, "failed": 0, "examples": [...]},
    "browser": {"status": "pass", "routes_walked": 14, "dynamic_routes_walked": 6, "blank_routes": 0, "crud_evidence": [...]},
    "integration": {"status": "pass", "endpoints": 31, "all_have_ui_caller": true, "undocumented_calls": 0}
  },
  "data": {
    "live_db_path": "backend/data/app.db",
    "seeded_tables": {"stores": 4, "categories": 15, "products": 42},
    "size_bytes": 102400
  }
}
```

The server's deploy step reads this and emits a
`deploy_progress` event with the report, which the UI renders as a
collapsible "What was verified" section. The user no longer has to
take pass/fail on faith.

### Change C — `verify:api` runs against the live DB with txn rollback

Current: `verify-api` boots the backend with
`DATABASE_URL=sqlite:///.../verify.db` (a temp file).

Proposed: `verify-api` boots the backend with the SAME
`DATABASE_URL` the systemd unit uses
(`/opt/ojas-apps/<slug>/data/app.db` or the project's
`backend/data/app.db`). To prevent test runs from corrupting user
data, wrap each test in `BEGIN ... ROLLBACK`. The DB is provably
unchanged when the test finishes.

Implementation: add a `OJAS_VERIFY_TXN_MODE=1` env var the backend
honors — when set, every request handler runs inside a
`with engine.begin() as conn: ...` that rolls back at the end of
the request. The verify suite sets this env var, the systemd unit
doesn't, so production is unaffected.

Why: the test then exercises the same data the user sees. FK
relationships, unique constraints, and the auto-seeded state are
all real. False-positives from "schema is incomplete" drop
significantly.

Risk: SQLite + multi-thread + concurrent transactions can deadlock.
Mitigation: use a single `BEGIN IMMEDIATE` per test, single
threaded (`uvicorn --workers 1 --loop asyncio`).

### Change D — FK-aware pre-pass in `verify:api`

Current: `verify-api` synthesizes a body from the schema and sends
it. If the body has a FK field (`product_id`), the synthesis
generates a random integer, which may not exist in the parent
table → 500 from the FK constraint, reported as "endpoint broken".

Proposed: before exercising a (method, path), identify FK-shaped
fields in the request body schema (suffixed `_id`, type=integer,
not in the path params), walk the OpenAPI for a matching list
endpoint, `GET` the list, take the first id, substitute it into
the synthesized body. This makes the test produce realistic
requests and eliminates the largest class of false-positives.

### Change E — `verify:browser` discovers routes from `App.tsx` declarations

Current: BFS from `/`, cap at 50 routes. Routes only reachable by
direct URL or only linked from JS-conditional branches get missed.

Proposed: also parse `src/App.tsx` (and any other router file
matching `<Route path=` or `createBrowserRouter([...])`) and add
each declared path to the BFS seed list. Raise `MAX_ROUTES` from
50 to 200. The result: declared routes get tested even if no
anchor links to them.

### Change F — Seed-copy safety net (Change 1b from earlier discussion)

After `verify:api` + `verify:browser` pass, if the live
`backend/data/app.db` is empty (size < 1KB or zero rows in every
table), copy `node_modules/.ojas-verify/verify.db` →
`backend/data/app.db`. Belt-and-suspenders for the case where
auto-seed code is buggy or missing. Skip if the live DB already
has data (don't clobber user edits).

## Shipping order

Each change is independently shippable. Recommended order, biggest
impact first:

1. **Change A** (`verify:integration`) — 2 days, the headline
   "every endpoint must have a UI screen" check
2. **Change B** (verify report surfaced in UI) — 1 day, the
   visibility fix that builds trust
3. **Change D** (FK-aware pre-pass) — 1 day, kills the largest
   class of false-positives
4. **Change C** (live-DB verify-api) — 1 day, makes the API
   test real
5. **Change E** (App.tsx route discovery) — 0.5 day, last-mile
   coverage
6. **Change F** (seed-copy safety net) — 0.5 day, belt-and-suspenders

Total: ~1 week. The first two slices alone (A + B) give the user
most of what they want: provable endpoint↔UI coverage and visible
evidence on every deploy.

## What this plan does NOT change

- **The two-React guard** — still in `check-deps.mjs` and
  `verify-render.mjs`, no change
- **The Radix invariant guard** — still in `verify-radix.mjs`,
  no change
- **The IncompleteBuild self-heal** — server-side, already
  shipped in commit `bda331d`
- **The static template's verify chain** — `verify:api` and
  `verify:integration` are no-ops there (no backend), but the
  rest of the chain runs unchanged

## Open questions

- **Change C, SQLite + multi-thread risk:** is the existing
  backend single-threaded under uvicorn in production? (Need to
  check the systemd unit. If yes, the txn-isolation is easy. If
  no, we need a different strategy — maybe a shadow DB copied
  from the live one at test start.)
- **Change B, deploy UI integration:** the server reads the
  report via the existing `WebReporter._pub` channel, but the
  exact UI rendering is owned by the React frontend (web/).
  Coordination needed.
- **Change A regex precision:** simple regex on `fetch("/api/...")`
  will miss `fetch(`/api/${id}/items`)` (template-string
  concatenation). Heuristic: any `fetch(` whose first quoted
  argument contains `/api/` as a substring. Not perfect but
  catches >90% of cases. Full TS AST parsing is overkill.
