# Ojas — VM Setup, Deploy & Operations Guide

This is the single reference for everything operational: first-time VM setup,
redeploys after code changes, multi-user behaviour, debugging.

If you've never deployed before, read **Part 1** in order. If you're already
running and just want to update, jump to **Part 3**.

---

## Part 1 — Initial VM setup (one-time)

### 1.1 Provision the VM
- Linux VM with public IP (Ubuntu 22.04+ or Debian 12+)
- Open inbound: `22/tcp` (SSH), `80/tcp` (HTTP), `443/tcp` (HTTPS)
- Block everything else

### 1.2 DNS
At your DNS provider, create an **A record**:

| Type | Name | Value |
|------|------|-------|
| A    | ojas (or whatever subdomain you want) | the VM's public IP |

Verify from the VM with:
```bash
dig ojas.yourdomain.com +short
# Must return the VM's IP. If empty, wait for propagation (5-60 min).
```

### 1.3 Run the installer

SSH into the VM as root (or use sudo), then:

```bash
curl -fsSL https://raw.githubusercontent.com/Sujith-Medisetty/agentic-rag-v2/master/deploy/install.sh \
    | FORGE_DOMAIN=ojas.yourdomain.com sudo -E bash
```

The script:
1. Installs Caddy, Node 20, Python build deps
2. Creates a system `forge` user (no shell login)
3. Clones the repo to `/opt/forge`
4. Sets up Python venv + installs requirements.txt
5. Builds the frontend (`web/dist/`)
6. Drops the systemd unit + Caddyfile
7. Stops at a prompt asking you to fill in `.env`

### 1.4 Fill in `.env`

```bash
sudo -u forge nano /opt/forge/.env
```

Minimum required:

```env
# --- LLM provider ---
# Default is Anthropic; change to "minimax" or "openai-compatible" if you
# prefer those. Each takes its own API key + base URL below.
AGENT_PROVIDER=minimax
AGENT_MODEL=MiniMax-M1            # or whatever your model dashboard shows
MINIMAX_API_KEY=<your-key>
# MINIMAX_BASE_URL is optional; defaults to https://api.minimax.io/v1

# --- Or, for Anthropic ---
# AGENT_PROVIDER=anthropic    (or omit; default)
# ANTHROPIC_API_KEY=sk-ant-...

# --- Root user (you, the VM owner) ---
FORGE_ROOT_EMAIL=you@example.com
FORGE_ROOT_PASSWORD=a-strong-password

# --- Where projects live on disk ---
FORGE_DEFAULT_WORKSPACE=/home/forge/ojas
```

Optional but useful:

```env
FORGE_ALLOW_SIGNUP=false        # lock signup so only root has an account
AGENT_LLM_TIMEOUT_SECS=300      # hard per-LLM-call timeout
```

Save (Ctrl-O, Enter, Ctrl-X).

### 1.5 Start services

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now forge-backend
sudo systemctl reload caddy   # or restart if reload fails
```

### 1.6 Verify

```bash
# Local backend
curl http://127.0.0.1:8765/api/health
# Expect: {"ok":true,...}

# Public HTTPS (wait ~30s after first start for Let's Encrypt cert)
curl https://ojas.yourdomain.com/api/health
# Expect: same JSON

# Tail logs if anything fails
sudo journalctl -u forge-backend -f
sudo journalctl -u caddy -f
```

Open `https://ojas.yourdomain.com` in a browser → log in with the root
email + password you set.

---

## Part 2 — Multi-user model

### Roles
- **root** — the VM owner. Materialised on first login with the
  `FORGE_ROOT_EMAIL` + `FORGE_ROOT_PASSWORD` credentials. Sees every user's
  projects + sessions, has the **Admin** tab, bypasses the workspace jail
  (full filesystem + shell access).
- **user** — anyone who signs up via the Login screen (when
  `FORGE_ALLOW_SIGNUP=true`). Sees only their own data. Writes are jailed
  to `<FORGE_DEFAULT_WORKSPACE>/<their-email-slug>/`.

### What each user gets on first login
- An auto-created default project pointing at their workspace folder
- An empty session list (sidebar)
- Their own LangGraph checkpoint thread per session

### Important: identity switches require a hard reload

If you log in as root, then **on the SAME tab** log out and sign up as a
new user — the frontend NOW does a `window.location.assign("/")` after
auth to fully reset React state. Without this, the previous identity's
cached `me` / project / session list leak into the new user's UI and you
see "project not found", the wrong Admin tab visibility, etc. This is
fixed in the latest deploy.

If you're upgrading from an older deploy and still see the bug:
- Log out
- Clear localStorage manually in DevTools (or close the PWA + clear app data)
- Log in fresh

---

## Part 3 — Redeploying after code changes

### Manual (always works)

```bash
sudo bash /opt/forge/deploy/update.sh
```

That script: `git pull` → reinstall any new Python deps → rebuild frontend
→ restart backend → reload Caddy. ~20-30 seconds.

### Automatic via GitHub Actions (recommended)

Already wired in `.github/workflows/deploy.yml`. One-time setup:

1. **On the VM**, generate a deploy SSH key:
   ```bash
   sudo ssh-keygen -t ed25519 -f /root/.ssh/ojas-deploy -N ''
   sudo bash -c 'cat /root/.ssh/ojas-deploy.pub >> /root/.ssh/authorized_keys'
   sudo cat /root/.ssh/ojas-deploy   # COPY this output
   ```

2. **In GitHub** → repo → Settings → Secrets and variables → Actions →
   New repository secret. Add three:

   | Secret | Value |
   |--------|-------|
   | `VM_HOST` | Your VM's public IP or domain |
   | `VM_USER` | `root` |
   | `VM_SSH_PRIVATE_KEY` | The private key you copied |

3. Every `git push origin master` now triggers a deploy. Check the
   **Actions** tab on GitHub to watch it run (~30s).

### Automatic via cron (lazier alternative)

If you'd rather skip the GitHub Secrets dance, install the cron-based
poller on the VM:

```bash
sudo install -m 755 -o root -g root \
    /opt/forge/deploy/cron-auto-update.sh /usr/local/bin/forge-auto-update
sudo crontab -e
# Add this line:
*/5 * * * * /usr/local/bin/forge-auto-update >> /var/log/forge-auto-update.log 2>&1
```

Polls origin every 5 minutes; deploys only if there are new commits.

---

## Part 4 — Day-to-day operations

### Useful commands

```bash
# Backend
sudo systemctl status forge-backend
sudo systemctl restart forge-backend
sudo journalctl -u forge-backend -f

# Caddy
sudo systemctl reload caddy
sudo journalctl -u caddy -f

# Database queries (read-only is fine as the forge user)
sudo -u forge sqlite3 /home/forge/.agentic-rag/server.db \
    "SELECT email, role FROM users;"
sudo -u forge sqlite3 /home/forge/.agentic-rag/server.db \
    "SELECT pid, port, command FROM session_processes;"
```

### The Admin panel (root only)

While logged in as root, the sidebar shows an **Admin** button:
- **Processes table** — every long-running PID spawned by the agent
  (npm run dev, vite preview, etc.) with PID + port + session + command.
  Click "Kill" to SIGTERM. Auto-refreshes every 3 seconds.
- **Users table** — every account, with their role.

### Where files live on the VM

| Path | What |
|------|------|
| `/opt/forge/` | The Ojas source code (cloned from this repo) |
| `/opt/forge/.env` | Secrets — chmod 600, owned by forge |
| `/opt/forge/web/dist/` | Built frontend (served by Caddy) |
| `/home/forge/.agentic-rag/server.db` | Sessions, messages, events, users |
| `/home/forge/.agent/checkpoints.db` | LangGraph conversation checkpoints |
| `/home/forge/.agent/sessions/<id>/` | Per-session sub-agent + todo state |
| `<FORGE_DEFAULT_WORKSPACE>/<user-slug>/<session-slug>/` | What the agent built for that session |

### What gets deleted when you delete a session
- DB rows (session + messages + events + processes)
- LangGraph checkpoint thread
- `~/.agent/sessions/<id>/` directory
- The session's `<workspace>/<user>/<session-slug>/` workspace (the
  generated app files)
- Any running spawned processes for that session (SIGTERM)

### What gets deleted when you delete a project
- All the above for every session in the project
- Plus the project row itself

### What is NEVER deleted automatically
- Your `.env`, your DB, your source repo at `/opt/forge`
- The shared LangGraph checkpoints DB file (just the rows for those threads)

---

## Part 5 — Troubleshooting

| Symptom | Where to look |
|---------|---------------|
| Can't reach the domain at all | DNS not propagated. `dig ojas.yourdomain.com +short` |
| Browser says "your connection isn't private" | Caddy hasn't fetched the Let's Encrypt cert yet. `journalctl -u caddy -f` and wait |
| Login shows wrong identity / Admin tab leaks | You're on an old build. Run `sudo bash /opt/forge/deploy/update.sh`, hard-refresh the browser, log out + log in. |
| "Project not found" right after signup | Same as above — pre-fix builds had a state-leak bug. After update.sh, this is gone. |
| 502 Bad Gateway from Caddy | Backend isn't running. `systemctl status forge-backend` and `journalctl -u forge-backend -n 50` |
| Chat history doesn't load on refresh | Already-fixed bug (was a `LIMIT 1000` in `list_events`). Pull latest + redeploy. |
| Agent spawns a dev server but admin doesn't show it | The agent must use `bash` with `run_in_background=true` for tracking to kick in. One-shot `npm run dev` in foreground exits with the tool call, so there's no PID to register. |
| MiniMax 400 errors | Usually wrong `AGENT_MODEL` string. Check the API docs page for the exact model name. |

### Reset a session entirely from CLI
If a session is misbehaving:
```bash
sudo -u forge sqlite3 /home/forge/.agentic-rag/server.db \
    "DELETE FROM sessions WHERE id = '<session-id>';"
# Or just use the UI's delete button.
```

### Full purge of a single user
```bash
sudo -u forge sqlite3 /home/forge/.agentic-rag/server.db \
    "DELETE FROM users WHERE email = 'alice@example.com';"
# CASCADE deletes their projects, sessions, messages, events, processes
```

---

## Part 6 — Updating the LLM provider

Live without restarting Forge:

1. Edit `/opt/forge/.env`
2. `sudo systemctl restart forge-backend`

That's it. Existing sessions keep working but the next turn uses the new
provider. (LangGraph checkpoints survive — only the model client gets
re-instantiated.)

Switching providers will NOT migrate prior message history's
provider-specific metadata (e.g. Anthropic `tool_use` blocks vs OpenAI
`tool_calls`). If you swap mid-session and see weird errors, start a fresh
session.

---

## Part 7 — Backup recommendation

Single file to back up: `/home/forge/.agentic-rag/server.db`

This holds users, projects, sessions, messages, events, processes — i.e.
everything you'd care about. The LangGraph checkpoint DB
(`/home/forge/.agent/checkpoints.db`) is optional — losing it means
sessions can't resume mid-conversation, but the chat history in `server.db`
is still readable.

Quick cron-based backup:

```bash
sudo crontab -e
# Add:
0 3 * * * sqlite3 /home/forge/.agentic-rag/server.db ".backup '/var/backups/ojas-$(date +\%F).db'"
```

Restores by copying the backup back over `server.db` while
`forge-backend` is stopped.
