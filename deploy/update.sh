#!/usr/bin/env bash
# Ojas — update + restart.
#
# Run this AFTER `install.sh` whenever you want to pull the latest code:
#   sudo bash /opt/ojas/deploy/update.sh
#
# Pulls from origin, reinstalls any new Python deps, rebuilds the frontend,
# restarts the systemd unit. No need to re-touch Caddy unless the Caddyfile
# changed.

set -euo pipefail

FORGE_DIR="${FORGE_DIR:-/opt/ojas}"
FORGE_USER="${FORGE_USER:-ojas}"
FORGE_BRANCH="${FORGE_BRANCH:-master}"

BLUE="\033[34m"; GREEN="\033[32m"; RST="\033[0m"
log()  { printf "${BLUE}▸${RST} %s\n" "$*"; }
ok()   { printf "${GREEN}✓${RST} %s\n" "$*"; }

[[ $EUID -eq 0 ]] || { echo "Run with sudo." ; exit 1; }

log "Pulling latest source"
sudo -u "${FORGE_USER}" git -C "${FORGE_DIR}" fetch origin "${FORGE_BRANCH}"
sudo -u "${FORGE_USER}" git -C "${FORGE_DIR}" reset --hard "origin/${FORGE_BRANCH}"
ok "Source updated"

log "Installing any new Python deps"
sudo -u "${FORGE_USER}" bash <<EOF
set -e
cd ${FORGE_DIR}
. .venv/bin/activate
pip install --quiet -r requirements.txt
EOF
ok "Python deps current"

log "Rebuilding frontend"
sudo -u "${FORGE_USER}" bash <<EOF
set -e
cd ${FORGE_DIR}/web
npm ci --silent
npm run build --silent
EOF
ok "Frontend built"

log "Restarting backend"
systemctl restart ojas-backend
ok "ojas-backend restarted"

log "Reloading Caddy"
systemctl reload caddy
ok "Caddy reloaded"

printf "\n${GREEN}Done.${RST} Tail logs with:  journalctl -u ojas-backend -f\n"
