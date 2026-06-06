#!/usr/bin/env bash
# Ojas — one-shot installer for Ubuntu 22.04+ / Debian 12+ VMs.
#
# Usage (as root or with sudo):
#   curl -fsSL https://raw.githubusercontent.com/<you>/<repo>/main/deploy/install.sh | bash
# or, if you've already cloned the repo:
#   sudo bash deploy/install.sh
#
# What it does:
#   1. Installs system packages: python3, nodejs, caddy, git
#   2. Creates an `ojas` Linux user (no shell login, only systemd)
#   3. Clones (or updates) the repo at /opt/ojas
#   4. Sets up Python venv + installs requirements.txt
#   5. Builds the frontend (web/dist)
#   6. Drops the systemd unit + Caddyfile in place
#   7. Prompts you to edit /opt/ojas/.env, then starts services
#
# Re-running this script is safe — every step is idempotent.

set -euo pipefail

# ---- Config (override via env) --------------------------------------------

OJAS_REPO="${OJAS_REPO:-https://github.com/Sujith-Medisetty/agentic-rag-v2.git}"
OJAS_BRANCH="${OJAS_BRANCH:-master}"
OJAS_DIR="${OJAS_DIR:-/opt/ojas}"
OJAS_USER="${OJAS_USER:-ojas}"
OJAS_DOMAIN="${OJAS_DOMAIN:-ojas.example.com}"
# Root domain under which deployed apps are served at <slug>.<apps_root>/.
# Defaults to the apex (most common setup) but can be different — e.g.
# apex at ojas.example.com but apps at apps.example.com. If you set this
# explicitly, also set the matching `*.apps_root` DNS A record.
OJAS_APPS_ROOT_DOMAIN="${OJAS_APPS_ROOT_DOMAIN:-$OJAS_DOMAIN}"

# ---- Style ----------------------------------------------------------------

BLUE="\033[34m"; GREEN="\033[32m"; YELLOW="\033[33m"; RED="\033[31m"; DIM="\033[2m"; RST="\033[0m"
log()   { printf "${BLUE}▸${RST} %s\n" "$*"; }
ok()    { printf "${GREEN}✓${RST} %s\n" "$*"; }
warn()  { printf "${YELLOW}⚠${RST}  %s\n" "$*"; }
die()   { printf "${RED}✗${RST} %s\n" "$*" >&2; exit 1; }
banner() { printf "\n${BLUE}━━━ %s ━━━${RST}\n" "$*"; }

# ---- Preflight ------------------------------------------------------------

[[ $EUID -eq 0 ]] || die "Run as root (or with sudo). Got UID $EUID."

. /etc/os-release 2>/dev/null || die "Can't read /etc/os-release. Are you on Linux?"
case "${ID:-}" in
    ubuntu|debian) ok "Detected ${PRETTY_NAME}" ;;
    *) warn "This script is tuned for Ubuntu / Debian. Detected: ${ID}. Continuing." ;;
esac

# ---- 1. System packages ---------------------------------------------------

banner "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3 python3-venv python3-pip \
    git curl ca-certificates gnupg \
    debian-keyring debian-archive-keyring apt-transport-https
ok "Base packages installed"

# Caddy via official repo (Debian/Ubuntu).
if ! command -v caddy >/dev/null; then
    log "Installing Caddy"
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    apt-get update -qq
    apt-get install -y -qq caddy
fi
ok "Caddy installed: $(caddy version | head -1)"

# Node 20 via NodeSource for the frontend build.
if ! command -v node >/dev/null || [[ "$(node -v | tr -d 'v' | cut -d. -f1)" -lt 18 ]]; then
    log "Installing Node 20"
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y -qq nodejs
fi
ok "Node installed: $(node -v)"

# ---- 2. ojas user --------------------------------------------------------

banner "Setting up ojas user"
if ! id "${OJAS_USER}" >/dev/null 2>&1; then
    useradd --system --create-home --shell /usr/sbin/nologin "${OJAS_USER}"
    ok "Created system user '${OJAS_USER}'"
else
    ok "User '${OJAS_USER}' already exists"
fi

# ---- 3. Clone repo --------------------------------------------------------

banner "Fetching Ojas source"
if [[ -d "${OJAS_DIR}/.git" ]]; then
    log "Updating existing checkout at ${OJAS_DIR}"
    # Run git as the owning user. Running it as root on an ojas-owned repo
    # trips git's safe.directory protection ("dubious ownership"). Doing it
    # as the service user avoids that AND keeps file modes consistent.
    sudo -u "${OJAS_USER}" git -C "${OJAS_DIR}" fetch origin "${OJAS_BRANCH}"
    sudo -u "${OJAS_USER}" git -C "${OJAS_DIR}" reset --hard "origin/${OJAS_BRANCH}"
else
    log "Cloning ${OJAS_REPO} into ${OJAS_DIR}"
    git clone --branch "${OJAS_BRANCH}" "${OJAS_REPO}" "${OJAS_DIR}"
    chown -R "${OJAS_USER}:${OJAS_USER}" "${OJAS_DIR}"
fi
ok "Source ready at ${OJAS_DIR}"

# ---- 4. Python venv + deps ------------------------------------------------

banner "Installing Python deps"
sudo -u "${OJAS_USER}" bash <<EOF
set -e
cd ${OJAS_DIR}
if [[ ! -d .venv ]]; then
    python3 -m venv .venv
fi
. .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
EOF
ok "Python deps installed"

# ---- 5. Frontend build ----------------------------------------------------

banner "Building frontend (web/dist)"
sudo -u "${OJAS_USER}" bash <<EOF
set -e
cd ${OJAS_DIR}/web
if [[ ! -d node_modules ]]; then
    npm ci --silent
fi
npm run build --silent
EOF
ok "Frontend built → ${OJAS_DIR}/web/dist"

# ---- 6. Systemd unit + Caddyfile -----------------------------------------

banner "Installing systemd unit"
install -o root -g root -m 644 "${OJAS_DIR}/deploy/ojas-backend.service" \
    /etc/systemd/system/ojas-backend.service
ok "Unit file installed"

banner "Installing Caddyfile"
# Two substitutions: the apex domain + the apps-root domain (may be
# the same). The Caddyfile template has `ojas.example.com` for the apex
# AND `*.ojas.example.com` for the wildcard subdomain block; we sed
# both to the configured values.
sed -e "s/ojas\\.example\\.com/${OJAS_DOMAIN}/g" \
    -e "s|\\*\\.ojas\\.example\\.com|\\*.${OJAS_APPS_ROOT_DOMAIN}|g" \
    "${OJAS_DIR}/deploy/Caddyfile" > /etc/caddy/Caddyfile
ok "Caddyfile installed — apex: ${OJAS_DOMAIN}, apps: *.${OJAS_APPS_ROOT_DOMAIN}"

# ---- 7. .env reminder -----------------------------------------------------

ENV_PATH="${OJAS_DIR}/.env"
if [[ ! -f "${ENV_PATH}" ]]; then
    cp "${OJAS_DIR}/.env.example" "${ENV_PATH}"
    chown "${OJAS_USER}:${OJAS_USER}" "${ENV_PATH}"
    chmod 600 "${ENV_PATH}"
    warn "Created ${ENV_PATH} from .env.example. EDIT IT before starting services:"
    warn "  sudo -u ${OJAS_USER} \$EDITOR ${ENV_PATH}"
    warn ""
    warn "At minimum set:"
    warn "  ANTHROPIC_API_KEY (or your provider's API key)"
    warn "  OJAS_ROOT_EMAIL"
    warn "  OJAS_ROOT_PASSWORD"
    warn "  OJAS_DEFAULT_WORKSPACE=/home/${OJAS_USER}/ojas"
    warn ""
    warn "Then run: sudo systemctl daemon-reload && sudo systemctl restart ojas-backend caddy"
    exit 0
fi
ok ".env already present (not overwriting)"

# ---- 8. Start everything --------------------------------------------------

banner "Starting services"
systemctl daemon-reload
systemctl enable --now ojas-backend
ok "ojas-backend started"
systemctl reload caddy || systemctl restart caddy
ok "Caddy reloaded"

# ---- 9. Verify ------------------------------------------------------------

banner "Verifying"
sleep 1
if curl -fsS http://127.0.0.1:8765/api/health >/dev/null 2>&1; then
    ok "Backend responds on 127.0.0.1:8765"
else
    warn "Backend didn't respond — check: journalctl -u ojas-backend -f"
fi

printf "\n${GREEN}━━━ Done.${RST}\n"
printf "Open: ${GREEN}https://${OJAS_DOMAIN}${RST}\n"
printf "\nUseful commands:\n"
printf "  ${DIM}sudo systemctl status ojas-backend${RST}\n"
printf "  ${DIM}journalctl -u ojas-backend -f${RST}\n"
printf "  ${DIM}sudo systemctl reload caddy${RST}\n"
printf "  ${DIM}journalctl -u caddy -f${RST}\n"
