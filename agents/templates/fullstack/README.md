# Ojas fullstack app scaffold

A minimal but complete fullstack app Ojas's agent can copy as the
starting point for any new fullstack project. The agent copy-pastes
the files (not the wrapper dirs) into `<project>/backend/` and
`<project>/frontend/`.

## Structure (what the agent produces)

```
<project>/                       ← the agent picks this name
├── backend/                     ← FastAPI (FULLSTACK ONLY)
│   ├── main.py                  # FastAPI app (see comments for Ojas conventions)
│   ├── requirements.txt         # fastapi, uvicorn[standard], sqlalchemy, pydantic
│   ├── database.py              # SQLAlchemy engine + SessionLocal + Base + get_db
│   ├── models.py                # SQLAlchemy ORM models
│   ├── schemas.py               # Pydantic request/response schemas
│   └── .venv/                   # created by the deploy pipeline (don't commit)
├── frontend/                    ← Vite + React (ALWAYS named frontend/)
│   ├── index.html
│   ├── package.json
│   ├── tsconfig.json
│   ├── vite.config.ts           # `base: './'` is REQUIRED — see file
│   └── src/
│       ├── main.tsx
│       └── App.tsx              # Example: list + add / toggle
└── (no other top-level files for the app itself — README,
   LICENSE, .gitignore are fine)
```

**Folder names are part of the contract.** `frontend/` and `backend/`
must be named exactly that — the deploy pipeline greps for them.
`client/`, `web/`, `app/`, `ui/`, `server/`, `api/` will not be found.

## Stack (pinned — don't substitute)

- **Frontend:** Vite + React + TypeScript.
- **Backend:** Python 3.10+ + FastAPI + SQLAlchemy + SQLite.
- **DB:** SQLite file at `<project>/data/app.db` (path passed via
  `DATABASE_URL` env var; created by the deploy pipeline).

No other framework, runtime, or database is supported. If the user
asks for Django, Express, Postgres, etc., the agent must stop and
tell the user Ojas can't deploy it.

## Local dev (two terminals)

```bash
# terminal 1 — backend on :8000
cd <project>/backend
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn main:app --reload --port 8000

# terminal 2 — frontend on :5180 (proxies /api/* to :8000)
cd <project>/frontend
npm install
npm run dev -- --port 5180
```

Open http://localhost:5180.

## Production build (what the agent does)

```bash
cd <project>/frontend
npm install
npm run build               # → frontend/dist/
ls frontend/dist/index.html   # MUST exist after build
```

The backend has no build step — Python source ships as-is.

## `include_router` ordering — read this if you write your own `main.py`

FastAPI's `APIRouter.include_router` (and `FastAPI.include_router`)
snapshots the router's routes at the moment of the call. Anything
added to the router AFTER `include_router()` returns is **silently
dropped** — the route never registers, and the deploy pipeline's
`/health` check will time out and the app will look like it crashed
even though it didn't.

Rule: **define every `@api_router.*` decorator BEFORE
`app.include_router(api_router)`.** The template's `main.py` already
follows this order — copy it verbatim if you're not sure.

## `/health` route — required

The deploy pipeline polls `GET /health` for up to 5 seconds after
starting the systemd unit. If it doesn't return 200, the deploy
marks the app as `error`. Always include a `/health` route in your
`main.py` — even if your app has no other reason for one. Example:

```python
@app.get("/health")
def health() -> dict:
    """Liveness probe. Ojas's deploy pipeline polls this for up to 5s
    after `systemctl start`. If it doesn't return 200, the deploy
    marks the app as 'error'."""
    return {"ok": True}
```

## Bind to 127.0.0.1, not 0.0.0.0

The deploy pipeline's systemd unit sets `--host 127.0.0.1` for you.
If you test locally, use the same. Caddy is the only thing on the
host that talks to your backend; binding to 0.0.0.0 would expose
the API to anyone on the VM's network.

## How Ojas deploys it (the user clicks 🚀 Deploy)

1. The agent's `npm run build` finishes — `frontend/dist/index.html`
   must exist.
2. The agent's turn ends. The user sees a **🚀 Deploy** button above
   the chat. They click it, type a slug, click Deploy. (If the
   session has more than one project folder, the dialog shows a
   Project dropdown so they can pick.)
3. Ojas allocates a free port (9100–9899), writes the systemd unit
   `ojas-app-<slug>.service` with:
   - `User=ojas`, `WorkingDirectory=/opt/ojas-apps/<slug>/backend`
   - `Environment="DATABASE_URL=sqlite:////opt/ojas-apps/<slug>/data/app.db"`
   - `ExecStart=/opt/ojas-apps/<slug>/backend/.venv/bin/uvicorn main:app --host 127.0.0.1 --port <port>`
   - `MemoryMax=512M`, `CPUQuota=200%`, `ProtectSystem=strict`
4. `pip install -r requirements.txt` into a venv at `backend/.venv`.
5. `systemctl daemon-reload && systemctl enable --now ojas-app-<slug>`
6. Polls `http://127.0.0.1:<port>/health` for up to 5s.
7. Caddy's per-slug block gets a `@api path /api/*` handle that
   reverse-proxies to `127.0.0.1:<port>`. The frontend `dist/` is
   served from `/opt/ojas-apps/<slug>/static/`.

Toggle off → `systemctl stop` + dir move. Memory goes to ~0.

If the slug is already taken, the dialog shows a clear "this slug
is already taken, pick another" error — no auto-suffix.

## Customising the template

- Replace the `Item` model in `backend/main.py` with your domain.
- Add more routes with `@api_router.get/post/patch/delete(...)` —
  they all live on the same router, registered before
  `app.include_router(api_router)`.
- Add Alembic for real migrations (drop the `Base.metadata.create_all`
  line and run `alembic upgrade head` in the lifespan).
- Replace `frontend/src/App.tsx` with your UI. The `API` constant in
  the template is `import.meta.env.BASE_URL + "/api"` — works at
  any subpath because of `base: './'` in `vite.config.ts`.
- Both backend and frontend can have multiple files — keep them
  under `backend/` and `frontend/` and Ojas will find them.

## Don't

- Don't use a non-FastAPI backend (Flask, Express, Django, Node,
  Go, Rust, Java) — the systemd unit literally runs `uvicorn main:app`.
- Don't bind the backend to `0.0.0.0` (Caddy proxies to localhost).
- Don't put data files in the repo — `data/` is gitignored.
- Don't put the frontend in anything other than `frontend/` — the
  deploy pipeline greps for that exact name.
- Don't run `subprocess.run` from the backend — there's no shell
  isolation and it'd be a security hole.
- Don't use a different DB engine — SQLite at `data/app.db` is the
  only thing wired up.
