#!/usr/bin/env bash
# Ojas — cron-driven auto-update.
#
# Drop this in /usr/local/bin and have cron run it every 5 minutes:
#
#   sudo install -m 755 -o root -g root \
#       /opt/ojas/deploy/cron-auto-update.sh /usr/local/bin/ojas-auto-update
#
#   sudo crontab -e
#   # Add:
#   */5 * * * * /usr/local/bin/ojas-auto-update >> /var/log/ojas-auto-update.log 2>&1
#
# What it does:
#   1. `git fetch` on /opt/ojas
#   2. If origin/master has new commits → run update.sh
#   3. Else → silently exit
#
# So pushes land within ~5 minutes without any GitHub setup. Lightweight; the
# only "work" between deploys is a `git fetch` which is a quick HTTPS HEAD.

set -euo pipefail

FORGE_DIR="${FORGE_DIR:-/opt/ojas}"
FORGE_USER="${FORGE_USER:-ojas}"
BRANCH="${FORGE_BRANCH:-master}"

cd "${FORGE_DIR}"

# Fetch quietly. Nothing to do if the remote has no new commits.
sudo -u "${FORGE_USER}" git fetch origin "${BRANCH}" --quiet

LOCAL_SHA=$(git rev-parse HEAD)
REMOTE_SHA=$(git rev-parse "origin/${BRANCH}")

if [[ "${LOCAL_SHA}" == "${REMOTE_SHA}" ]]; then
    # No change. Silent exit so the cron log stays clean.
    exit 0
fi

echo "[$(date -Iseconds)] Update detected: ${LOCAL_SHA:0:8} → ${REMOTE_SHA:0:8}"
bash "${FORGE_DIR}/deploy/update.sh"
echo "[$(date -Iseconds)] Deploy complete."
