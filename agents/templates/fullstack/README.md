# Ojas fullstack app scaffold

A minimal but complete fullstack app Ojas's agent can copy as the
starting point for any new fullstack project.

## Structure

```
my-app/
├── backend/
│   ├── main.py            # FastAPI app (see comments for Ojas conventions)
│   └── requirements.txt   # fastapi, uvicorn, sqlalchemy, pydantic
├── frontend/
│   ├── index.html
│   ├── package.json
│   ├── tsconfig.json
│   ├── vite.config.ts     # `base: './'` is REQUIRED — see file
│   └── src/
│       ├── main.tsx
│       └── App.tsx        # Example: list + add + toggle
└── (no top-level files)
```

## Local dev

Two terminals:

```bash
# terminal 1 — backend on :8000
cd backend
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn main:app --reload --port 8000

# terminal 2 — frontend on :5180 (proxies /api/* to :8000)
cd frontend
npm install
npm run dev -- --port 5180
```

Open http://localhost:5180.

## Production build (what the agent does)

```bash
cd frontend && npm install && npm run build   # → frontend/dist/
# Backend has no build step; Python source is shipped as-is.
```

## How Ojas deploys it

The `🚀 Deploy` button in the chat detects this is a fullstack app
(presence of `backend/requirements.txt`), then:

1. Allocates a free port (9100-9899)
2. Writes a systemd unit `ojas-app-<slug>.service` with:
   - `User=ojas`, `WorkingDirectory=/opt/ojas-apps/<slug>/backend`
   - `Environment=DATABASE_URL=sqlite:////opt/ojas-apps/<slug>/data/app.db`
   - `ExecStart=/opt/ojas-apps/<slug>/backend/.venv/bin/uvicorn main:app --host 127.0.0.1 --port <port>`
   - `MemoryMax=512M`, `CPUQuota=200%`, `ProtectSystem=strict`
3. `pip install -r requirements.txt` into a venv at `backend/.venv`
4. `systemctl daemon-reload && systemctl enable --now ojas-app-<slug>`
5. Polls `http://127.0.0.1:<port>/health` for up to 5s
6. Caddy's per-slug block gets a `@api path /api/*` handle that
   reverse-proxies to `127.0.0.1:<port>`
7. Frontend `dist/` is served from `/opt/ojas-apps/<slug>/static/`

Toggle off → `systemctl stop` + dir move. Memory goes to ~0.

## Customising

- Replace the `Item` model in `backend/main.py` with your domain
- Add Alembic for real migrations (drop the `create_all` line)
- Replace `frontend/src/App.tsx` with your UI
- Both backend and frontend can have multiple files — keep them
  under `backend/` and `frontend/` and Ojas will find them

## Don't

- Don't use `subprocess.run` from the backend — there's no shell
  isolation and it'd be a security hole
- Don't bind the backend to 0.0.0.0 — it must be 127.0.0.1 (Caddy
  proxies to localhost)
- Don't put data files in the repo — `data/` is gitignored
