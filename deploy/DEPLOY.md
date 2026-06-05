# Deploying Forge to a Linux VM

A complete walkthrough from "fresh Ubuntu/Debian VM with SSH" to "Forge live
at `https://forge.yourdomain.com`".

Everything below assumes you're SSH'd into the VM **as root** (or have `sudo`).
If you've cloned the repo locally and have the `deploy/` folder, the
`install.sh` script handles 95% of this — but read through once so you know
what's happening.

---

## 0. Prerequisites

- A Linux VM (Ubuntu 22.04+ or Debian 12+ — script targets these).
- A domain (e.g. `forge.example.com`) with an **A record pointing at the
  VM's public IP**. Set this up at your DNS provider FIRST. Wait for DNS
  to propagate (use `dig forge.yourdomain.com +short` — should return your
  VM's IP).
- An Anthropic API key.
- Ports **80** and **443** open in your firewall (`ufw allow 80,443/tcp`).
- SSH access on port 22 (don't lock yourself out).

---

## 1. One-shot install

The simplest path — works if you have GitHub access to clone the repo:

```bash
# As root on the VM
curl -fsSL https://raw.githubusercontent.com/Sujith-Medisetty/agentic-rag-v2/master/deploy/install.sh \
    | FORGE_DOMAIN=forge.yourdomain.com bash
```

That script does everything in sections 2-8 below in one go. If it
succeeds you'll be told to edit `/opt/forge/.env`, fill in your secrets,
then run `sudo systemctl restart forge-backend caddy`.

If you'd rather do it manually (recommended the first time so you understand
the moving pieces), continue with sections 2-8.

---

## 2. System packages

```bash
sudo apt-get update
sudo apt-get install -y \
    python3 python3-venv python3-pip \
    git curl ca-certificates gnupg \
    debian-keyring debian-archive-keyring apt-transport-https
```

Install **Caddy** (the reverse proxy + auto-HTTPS):

```bash
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt-get update
sudo apt-get install -y caddy
caddy version   # sanity check
```

Install **Node 20** (for the frontend build):

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash -
sudo apt-get install -y nodejs
node -v   # should print v20.x.x
```

---

## 3. Create a dedicated `forge` user

Don't run the backend as root — even though you OWN the VM, a security
boundary inside the OS adds defence in depth.

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin forge
```

`--system` = no UID quota issues, no /home/forge cleanup on uninstall,
`--shell /usr/sbin/nologin` = no interactive login (only systemd uses it).

---

## 4. Clone Forge

```bash
sudo git clone https://github.com/Sujith-Medisetty/agentic-rag-v2.git /opt/forge
sudo chown -R forge:forge /opt/forge
```

---

## 5. Python venv + install backend deps

```bash
sudo -u forge bash -c '
    cd /opt/forge
    python3 -m venv .venv
    . .venv/bin/activate
    pip install --upgrade pip
    pip install -r requirements.txt
'
```

---

## 6. Build the frontend

```bash
sudo -u forge bash -c '
    cd /opt/forge/web
    npm ci
    npm run build
'
```

Produces `/opt/forge/web/dist/` — these are the static files Caddy will
serve.

---

## 7. Configure `.env`

```bash
sudo -u forge cp /opt/forge/.env.example /opt/forge/.env
sudo -u forge chmod 600 /opt/forge/.env
sudo -u forge nano /opt/forge/.env    # or your editor of choice
```

**Minimum** to set:

```env
ANTHROPIC_API_KEY="sk-ant-..."
FORGE_ROOT_EMAIL="you@example.com"
FORGE_ROOT_PASSWORD="a-strong-password"
FORGE_DEFAULT_WORKSPACE="/home/forge/forge"
```

Set `FORGE_ALLOW_SIGNUP=false` if you want to be the only user.

Save + exit.

---

## 8. Install Caddyfile

Edit a copy of the Caddyfile in your repo, **replace `forge.example.com`
with your real domain**, then install it:

```bash
sed 's/forge\.example\.com/forge.yourdomain.com/g' \
    /opt/forge/deploy/Caddyfile \
    | sudo tee /etc/caddy/Caddyfile > /dev/null
sudo caddy validate --config /etc/caddy/Caddyfile   # sanity check
```

---

## 9. Install + start the systemd unit

```bash
sudo install -o root -g root -m 644 \
    /opt/forge/deploy/forge-backend.service \
    /etc/systemd/system/forge-backend.service
sudo systemctl daemon-reload
sudo systemctl enable --now forge-backend
sudo systemctl status forge-backend     # should say "active (running)"
```

If it's not running, tail the logs:

```bash
sudo journalctl -u forge-backend -f
```

---

## 10. Reload Caddy

```bash
sudo systemctl reload caddy
# If reload fails (first time, no cert yet), try:
sudo systemctl restart caddy
sudo journalctl -u caddy -f      # watch the TLS handshake with Let's Encrypt
```

Caddy will auto-fetch a Let's Encrypt cert on first start. This takes
~30 seconds the first time.

---

## 11. Verify

```bash
# Local HTTP check (backend talking)
curl http://127.0.0.1:8765/api/health
# {"ok":true,"needs_setup":...}

# Public HTTPS check
curl https://forge.yourdomain.com/api/health
# Same JSON
```

Open `https://forge.yourdomain.com` in a browser. You should see Forge.

---

## 12. First login

Two flows:

**a. Log in as root** — type the `FORGE_ROOT_EMAIL` + `FORGE_ROOT_PASSWORD`
you set in `.env`. The root row is materialised in the DB on first login.
You can see all sessions, all users, kill any process.

**b. Sign up a regular user** — if `FORGE_ALLOW_SIGNUP=true` (default),
anyone can sign up. They get their own workspace at
`/home/forge/forge/<email-slug>/`, scoped to them, jailed from filesystem
escape.

---

## Updating Forge later

```bash
sudo bash /opt/forge/deploy/update.sh
```

That pulls latest, reinstalls deps, rebuilds frontend, restarts the backend.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| `curl: (6) Could not resolve host` | DNS hasn't propagated. `dig forge.yourdomain.com +short` should return your VM's IP. |
| Caddy fails to get cert | Port 80 might be blocked by firewall — Let's Encrypt needs it. `sudo ufw allow 80/tcp && sudo ufw allow 443/tcp`. |
| Backend won't start | `journalctl -u forge-backend -n 100` — usually a missing env var or bad Python dep. |
| `502 Bad Gateway` from Caddy | Backend isn't running. `systemctl status forge-backend`. |
| Login works but APIs fail | Browser is hitting wrong origin. Check `AGENTIC_RAG_CORS_ORIGINS` in `.env` includes `https://forge.yourdomain.com`. |
| Agent can't write files | Workspace jail might be biting. As root user it shouldn't; for non-root, paths must be under `/home/forge/forge/<their-slug>/`. |

---

## Useful commands

```bash
# Restart backend after editing .env
sudo systemctl restart forge-backend

# Tail backend logs
sudo journalctl -u forge-backend -f

# Tail Caddy logs (TLS issues, request errors)
sudo journalctl -u caddy -f

# Force-reload Caddyfile after edits
sudo systemctl reload caddy

# See running processes the agent has spawned
sudo -u forge sqlite3 /home/forge/.agentic-rag/server.db \
    "SELECT pid, port, command FROM session_processes;"

# Kill a stray dev server
sudo kill <pid>
# (or use the admin panel in the UI)
```
