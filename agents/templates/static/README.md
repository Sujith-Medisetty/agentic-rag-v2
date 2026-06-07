# Ojas static app scaffold

A minimal but complete client-only app Ojas's agent can copy as the
starting point for any new static app. No backend, no database,
no port, no systemd unit. State lives in the browser
(`localStorage` / `IndexedDB`) and external data is fetched from
public APIs the browser calls directly.

## Structure (what the agent produces)

```
<project>/                       ← the agent picks this name
└── frontend/                    ← Vite + React (ALWAYS named frontend/)
    ├── index.html
    ├── package.json
    ├── tsconfig.json
    ├── vite.config.ts           # `base: './'` is REQUIRED — see file
    ├── public/
    │   ├── manifest.webmanifest # PWA install metadata
    │   └── sw.js                # minimal service worker
    └── src/
        ├── main.tsx
        ├── App.tsx              # example: todo list with localStorage
        └── components/
            └── InstallButton.tsx # beforeinstallprompt capture
```

**Folder names are part of the contract.** `frontend/` must be
named exactly that — the deploy pipeline greps for it. `client/`,
`web/`, `app/`, `ui/` will not be found.

## Stack (pinned — don't substitute)

- **Frontend:** Vite + React + TypeScript.
- **Storage:** Browser `localStorage` for small UI state. IndexedDB
  via `idb-keyval` (or any wrapper) for larger blobs.
- **External data:** Public APIs called from the browser (CORS
  permitting). If the API requires a secret key, the app needs
  fullstack (use a backend to hold the key).

No backend runtime, no database, no server. If the user needs
multi-user data or secrets-on-the-server, escalate to the
fullstack scaffold — Ojas can't deploy a static app with those
needs.

## Local dev (one terminal)

```bash
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

No backend to build. No `pip install`. No `.venv`. The deploy
pipeline just copies `frontend/dist/` to a permanent location and
Caddy serves it.

## How Ojas deploys it

The user clicks **🚀 Deploy** (or the build-ready banner under
the agent's last turn). Ojas copies `frontend/dist/` to
`/opt/ojas-apps/<slug>/static/`, the per-slug Caddy block serves
it at `https://<slug>.<host>/`, and HTTPS is provisioned
automatically on first request (~3s for the Let's Encrypt cert).

Toggle off in **Settings** → dir is renamed to
`/opt/ojas-apps/.stopped/<slug>/`; the build survives, the URL
comes back when you toggle on.

## PWA bits the template already has

- `<link rel="manifest">` in `index.html` pointing at
  `public/manifest.webmanifest`.
- `meta name="theme-color"`, apple-mobile-web-app-* meta tags,
  apple-touch-icon link.
- A `public/sw.js` that caches the app shell on install and
  serves cache-then-network.
- `<InstallButton />` rendered in the App header — captures
  `beforeinstallprompt` so the user gets a real install button
  instead of having to dig through the browser menu. Renders
  nothing once the app is installed (no nagging).

## PWA bits you need to add

- **Real icon files.** The template references `icon-192.png` and
  `icon-512.png` in the manifest. They're not in the repo (binary
  files don't belong in source control) — generate or download
  192×192 and 512×512 PNGs and drop them into `public/`. Without
  them, the install button still works but the home-screen icon
  is a generic browser icon.
- **App-specific name and theme color** in
  `public/manifest.webmanifest` and `index.html`. The template
  ships a generic `App` / `#4f46e5`; change both to match your
  product.

## What to keep

- `base: './'` in `vite.config.ts` — without it, deployed assets
  404 because the browser requests `/assets/...` instead of
  `./assets/...`.
- `import.meta.env.BASE_URL` for any URLs that need to be relative
  to the current path.
- `<InstallButton />` rendered in the layout, somewhere visible.
  Once the user installs, the button disappears on its own.

## What NOT to do

- Don't add a `backend/` folder to escape to a real database —
  the Ojas deploy pipeline only knows how to deploy static apps OR
  fullstack apps (with the `backend/` at the project root, name
  exactly). Adding `backend/` to a static app is what the
  fullstack scaffold is for.
- Don't put user data in the repo — there's no DB. `localStorage`
  / `IndexedDB` is the only persistence, and it's per-browser.
- Don't bind to a database. If the user wants their data to
  follow them across devices, escalate to the fullstack scaffold.
