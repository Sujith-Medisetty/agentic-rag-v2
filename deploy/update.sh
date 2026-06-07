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

OJAS_DIR="${OJAS_DIR:-/opt/ojas}"
OJAS_USER="${OJAS_USER:-ojas}"
OJAS_BRANCH="${OJAS_BRANCH:-master}"

BLUE="\033[34m"; GREEN="\033[32m"; RST="\033[0m"
log()  { printf "${BLUE}▸${RST} %s\n" "$*"; }
ok()   { printf "${GREEN}✓${RST} %s\n" "$*"; }

[[ $EUID -eq 0 ]] || { echo "Run with sudo." ; exit 1; }

log "Pulling latest source"
sudo -u "${OJAS_USER}" git -C "${OJAS_DIR}" fetch origin "${OJAS_BRANCH}"
sudo -u "${OJAS_USER}" git -C "${OJAS_DIR}" reset --hard "origin/${OJAS_BRANCH}"
ok "Source updated"

log "Installing any new Python deps"
sudo -u "${OJAS_USER}" bash <<EOF
set -e
cd ${OJAS_DIR}
. .venv/bin/activate
pip install --quiet -r requirements.txt
EOF
ok "Python deps current"

log "Rebuilding frontend"
# Earlier update.sh ran the build as root and left root-owned files in
# web/dist (notably workbox's sw.js.map). The build now runs as
# ${OJAS_USER}, which can't overwrite those leftovers → EACCES. Wipe
# dist/ and normalise ownership of the whole web/ tree before building
# so every run starts from a clean, user-owned slate.
rm -rf "${OJAS_DIR}/web/dist"
chown -R "${OJAS_USER}:${OJAS_USER}" "${OJAS_DIR}/web"
sudo -u "${OJAS_USER}" bash <<EOF
set -e
cd ${OJAS_DIR}/web
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
