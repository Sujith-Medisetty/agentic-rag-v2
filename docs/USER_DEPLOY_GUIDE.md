# Deploying your first app from the chat

Ojas builds fullstack apps into a session workspace, then you publish one
to a public URL by clicking **🚀 Deploy**. This is the user flow.

## 1. The agent builds the app

You describe an app in the chat. The agent scaffolds a project folder
with the pinned stack (Vite + React / FastAPI / SQLite), writes the
code, and runs `npm run build`. You see the agent's tool calls stream
by. When the agent's turn ends with a "Build complete" line, you're
ready to deploy.

## 2. The build-ready banner

Right under the agent's "Build complete" line, a small banner appears:

> **✓ Build complete · built 3m ago** &nbsp;&nbsp; **\[Deploy\]**

The banner knows the build is fresh because the server compares the
build's mtime against the most recent deploy from this session. If
you re-build and don't re-deploy, the banner stays. If you already
deployed, the banner hides (no point asking you to deploy what's
already live).

## 3. Click Deploy — the dialog

Clicking the banner (or the **🚀 Deploy** button in the strip above
the chat) opens the same dialog:

```
┌─ Deploy this build ────────────────────────────────────┐
│  The build's dist/ will be copied to a permanent        │
│  location and served at the URL below.                  │
│                                                        │
│  Slug                                                   │
│  [ weather-app____________________ ]                   │
│  URL: https://weather-app.<host>/                       │
│                                                        │
│  Project (auto-detected from the agent's build)         │
│  🔒 my-app                                             │
│                                                        │
│                            [ Cancel ]  [ Deploy ]       │
└────────────────────────────────────────────────────────┘
```

You only need to type the **Slug** — kebab-case, 1–40 chars, the
leftmost part of the URL. The **Project** field is auto-detected
from the agent's build and shown for context. With one project in
the session, it's locked (🔒).

## 4. Multi-app sessions: pick from a dropdown

If the session has more than one project folder (the agent built two
apps for you, or you're iterating on both), the **Project** field
becomes a dropdown:

```
┌ Project (pick which app to publish) ─────────────┐
│ ▼ produce-price-tracker                         │
│   weather-widget                                 │
│   calorie-tracker                                │
│ Pick the one to publish.                          │
└──────────────────────────────────────────────────┘
```

The newest project (by mtime) is pre-selected. The dialog also tells
you there are multiple projects so the locked-label surprise is gone.

## 5. Slug collision

If the slug you typed is already taken by another deployed app, the
dialog shows a red field-level error under the slug input:

> **Slug 'weather' is already taken — pick a different one**

The Deploy button is disabled until you change the slug. We don't
auto-suffix with `-2`/`-3` because that produces URLs the user
didn't choose (silent surprise). Pick a different name and click
Deploy.

## 6. After deploy

The dialog closes, the strip gets a new pill with your slug, and
the build-ready banner goes away (because the dist is no longer
newer than the latest deploy). Your app is live at
`https://<slug>.<host>/`. The first request takes a few seconds —
Caddy is fetching a Let's Encrypt cert for the new hostname.

## 7. Pause, resume, delete

Visit **Settings** in the sidebar. Your deployed apps are listed
under their source session, each with a toggle (Running / Paused)
and a delete button. Pausing frees the backend's RAM without
losing the build or the URL. The toggle restarts the systemd unit
and re-creates the Caddy route.

## 8. What's where on disk

| Path | What |
|------|------|
| `/opt/ojas-apps/<slug>/static/` | The built `dist/` (what the browser loads) |
| `/opt/ojas-apps/<slug>/backend/` | The FastAPI source (fullstack only) |
| `/opt/ojas-apps/<slug>/backend/.venv/` | The pip-installed deps |
| `/opt/ojas-apps/<slug>/data/app.db` | The SQLite database (fullstack only) |
| `/etc/systemd/system/ojas-app-<slug>.service` | The per-app systemd unit |
| `/etc/caddy/routes.d/<slug>.caddy` | The per-app reverse-proxy snippet |

Toggle off moves `/opt/ojas-apps/<slug>/` to
`/opt/ojas-apps/.stopped/<slug>/`; the data survives.

## Troubleshooting

- **Banner doesn't appear after a build.** Refresh the page. The
  banner only re-fetches detected-dist on mount and right after
  each turn ends.
- **Deploy button stays disabled.** The Project field is empty or
  the build is in `none` state (no dist found). Ask the agent to
  run `npm run build` and try again.
- **`502 Bad Gateway` after deploy.** The backend systemd unit
  didn't start (e.g. FastAPI import error, missing dependency).
  Check `sudo journalctl -u ojas-app-<slug> -n 30`. Most often:
  a typo in `backend/main.py`, a missing `import`, or the agent
  forgot to add a `GET /health` route.
- **`/health` returns 404.** The deploy pipeline's health check
  polls `/health` for 5 seconds. If it's missing, the deploy
  marks the app as `error`. Add `@app.get("/health") def health():
  return {"ok": True}` to `backend/main.py` and re-deploy.
- **Slug error says "already taken" but I want to reuse it.**
  Delete the existing app from Settings first, then deploy again.
  We don't auto-suffix — the slug is yours, the URL is yours.
- **`https://<slug>.<host>/` returns TLS error for 10+ seconds.**
  Caddy is fetching a Let's Encrypt cert for the new hostname.
  This is normal on first deploy; it never happens again for that
  hostname.
