#!/usr/bin/env bash
# backend/scripts/smoke.sh — one-shot backend liveness check.
#
# Why this script exists:
#   The Ojas system prompt forbids `kill` / `pkill` / freeing ports,
#   so the usual "start uvicorn with run_in_background + curl in a
#   separate bash call" pattern doesn't work — the spawned process
#   dies between bash calls and the curl times out. This script
#   does the bounded start, curl, and clean exit in ONE bash call
#   so the process lifetime is owned by this script (and dies with
#   it, no orphan).
#
# Usage:
#   cd <project-root>
#   bash backend/scripts/smoke.sh
#
# Exit codes:
#   0 — /health returned 200 within the timeout
#   1 — anything else (uvicorn failed to start, curl failed,
#       /health didn't return 200 in time, port already in use)
#
# Side effects:
#   - Kills any process listening on SMOKE_PORT before starting
#     (using lsof + scoped xargs, NOT pkill — pkill is forbidden).
#   - Removes the SMOKE_PORT-scoped uvicorn when the script exits,
#     even on success.

set -euo pipefail

# --- Config ------------------------------------------------------------------

# Pick a port that's almost certainly free. Avoid 8000 (default uvicorn)
# and 8765 (Ojas backend) — this script is meant to be run from a
# project root whose backend defaults to 8000, so we use 8765 only as
# a "definitely not the real one" sentinel.
SMOKE_PORT="${SMOKE_PORT:-8765}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-12}"  # seconds total, including uvicorn boot

# Resolve project root from this script's location so the agent can
# run it from anywhere: `bash backend/scripts/smoke.sh` works whether
# the cwd is the project root or not.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${PROJECT_ROOT}/backend"

# --- Pre-flight: clear the port if something is on it (scoped, no pkill) ---

if command -v lsof >/dev/null 2>&1; then
  STALE_PIDS="$(lsof -ti tcp:"${SMOKE_PORT}" 2>/dev/null || true)"
  if [ -n "${STALE_PIDS}" ]; then
    echo "[smoke] killing stale listeners on :${SMOKE_PORT} (pids: ${STALE_PIDS})"
    # shellcheck disable=SC2086  # STALE_PIDS is intentionally word-split
    kill ${STALE_PIDS} 2>/dev/null || true
    sleep 0.5
  fi
fi

# --- Pick the right uvicorn entry point --------------------------------------
#
# Prefer the project venv if it exists; fall back to whatever uvicorn
# is on PATH.

UVICORN=""
if [ -x ".venv/bin/uvicorn" ]; then
  UVICORN=".venv/bin/uvicorn"
elif command -v uvicorn >/dev/null 2>&1; then
  UVICORN="uvicorn"
else
  echo "[smoke] uvicorn not found. Install it: pip install -r requirements.txt"
  exit 1
fi

# --- Start uvicorn in the background, bound to this script's lifetime --------

LOG="$(mktemp -t ojas-smoke.XXXXXX.log)"
echo "[smoke] starting uvicorn on :${SMOKE_PORT} (logs: ${LOG})"

# `exec` is NOT used here — we need to capture the PID so we can kill
# it on exit regardless of the curl result.
"${UVICORN}" main:app --host 127.0.0.1 --port "${SMOKE_PORT}" --log-level warning \
  >"${LOG}" 2>&1 &
UVICORN_PID=$!

# Make sure uvicorn dies when this script exits, even on a Ctrl-C
# (EXIT catches the normal path; INT/TERM catch the interrupt paths).
cleanup() {
  if kill -0 "${UVICORN_PID}" 2>/dev/null; then
    kill "${UVICORN_PID}" 2>/dev/null || true
    # Give it a moment, then SIGKILL if it didn't go down cleanly.
    sleep 0.3
    kill -9 "${UVICORN_PID}" 2>/dev/null || true
  fi
  rm -f "${LOG}"
}
trap cleanup EXIT INT TERM

# --- Poll /health until 200 or timeout ---------------------------------------

DEADLINE=$(( $(date +%s) + HEALTH_TIMEOUT ))
RC=1
LAST_STATUS=""

while [ "$(date +%s)" -lt "${DEADLINE}" ]; do
  # -s: silent, -o: write body to /dev/null, -w: print just the status code
  LAST_STATUS="$(curl -s -o /dev/null -w '%{http_code}' \
    "http://127.0.0.1:${SMOKE_PORT}/health" 2>/dev/null || echo '000')"
  if [ "${LAST_STATUS}" = "200" ]; then
    RC=0
    break
  fi
  # If uvicorn already died, no point in continuing to poll.
  if ! kill -0 "${UVICORN_PID}" 2>/dev/null; then
    echo "[smoke] uvicorn exited unexpectedly. Last 20 log lines:"
    tail -n 20 "${LOG}" || true
    exit 1
  fi
  sleep 0.2
done

if [ "${RC}" -ne 0 ]; then
  echo "[smoke] FAIL: /health did not return 200 within ${HEALTH_TIMEOUT}s (last status: ${LAST_STATUS})"
  echo "[smoke] uvicorn log (last 30 lines):"
  tail -n 30 "${LOG}" || true
  exit 1
fi

echo "[smoke] PASS: /health returned 200 on http://127.0.0.1:${SMOKE_PORT}/"
exit 0
