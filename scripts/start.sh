#!/usr/bin/env bash
# One-shot start script for Mac + Linux.
#
# Boots the FastAPI backend (uvicorn) on :8765 in the background, then the
# Vite dev server on :5173 in the foreground. Ctrl-C kills both.
#
# Usage:
#   ./scripts/start.sh
#
# Requirements (checked at runtime):
#   python3 ≥ 3.10
#   node    ≥ 18
#   npm     (ships with node)
#   ANTHROPIC_API_KEY (the agent needs it)
#
# First-run side effects (all self-healing — re-running is safe):
#   - Creates a venv at ./.venv if absent and installs requirements.txt
#   - Runs `npm install` in web/ if node_modules is absent
#   - Generates PWA icons via scripts/generate-pwa-icons.py if they're missing.
#     Auto-installs Pillow + cairosvg (pip) AND the native libcairo library
#     (brew on macOS; clear apt-get/dnf hint on Linux). If libcairo can't be
#     installed automatically, the PWA still works with SVG icons only.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BLUE="\033[34m"; GREEN="\033[32m"; YELLOW="\033[33m"; RED="\033[31m"; DIM="\033[2m"; RST="\033[0m"

log()  { printf "${BLUE}▸${RST} %s\n" "$*"; }
ok()   { printf "${GREEN}✓${RST} %s\n" "$*"; }
warn() { printf "${YELLOW}⚠${RST}  %s\n" "$*"; }
die()  { printf "${RED}✗${RST} %s\n" "$*" >&2; exit 1; }

# ---- Preflight ------------------------------------------------------------

command -v python3 >/dev/null || die "python3 not found. Install Python 3.10+."
command -v node    >/dev/null || die "node not found. Install Node 18+."
command -v npm     >/dev/null || die "npm not found (should come with node)."

py_major=$(python3 -c 'import sys; print(sys.version_info.major)')
py_minor=$(python3 -c 'import sys; print(sys.version_info.minor)')
if (( py_major < 3 || (py_major == 3 && py_minor < 10) )); then
  die "Python 3.10+ required (found ${py_major}.${py_minor})."
fi

node_major=$(node -p 'process.versions.node.split(".")[0]')
if (( node_major < 18 )); then
  die "Node 18+ required (found v${node_major})."
fi

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  warn "ANTHROPIC_API_KEY is not set. The agent will fail until you export it."
  warn "  export ANTHROPIC_API_KEY=sk-ant-..."
fi

# ---- Backend: venv + deps -------------------------------------------------

if [[ ! -d .venv ]]; then
  log "creating Python venv at .venv"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

if [[ ! -f .venv/.deps_installed ]] || [[ requirements.txt -nt .venv/.deps_installed ]]; then
  log "installing Python deps (one-time, ~30s)"
  pip install --quiet --upgrade pip
  pip install --quiet -r requirements.txt
  touch .venv/.deps_installed
  ok "Python deps installed"
fi

# ---- Frontend: npm install ------------------------------------------------

if [[ ! -d web/node_modules ]]; then
  log "installing web deps (one-time, ~1min)"
  (cd web && npm install --silent)
  ok "web deps installed"
fi

# ---- PWA icons (optional) -------------------------------------------------
#
# cairosvg is a thin Python wrapper around the native libcairo C library.
# pip can install the wrapper, but libcairo itself needs an OS package
# manager (brew on macOS, apt/dnf on Linux). We try to bootstrap both.

ensure_icon_deps() {
  pip install --quiet pillow cairosvg 2>/dev/null || return 1

  # cairosvg's import triggers libcairo dlopen — this is the real check.
  if python3 -c 'import cairosvg' 2>/dev/null; then
    return 0
  fi

  # Native libcairo is missing — try to install it for the user.
  case "$(uname -s)" in
    Darwin)
      if command -v brew >/dev/null; then
        log "installing libcairo via Homebrew (one-time, ~20s)"
        if brew install cairo >/dev/null 2>&1; then
          ok "libcairo installed"
          python3 -c 'import cairosvg' 2>/dev/null && return 0
        fi
        warn "brew install cairo failed"
      else
        warn "Homebrew not found. Install from https://brew.sh, then re-run."
      fi
      ;;
    Linux)
      if command -v apt-get >/dev/null; then
        warn "libcairo missing. Run:  sudo apt-get install -y libcairo2"
      elif command -v dnf >/dev/null; then
        warn "libcairo missing. Run:  sudo dnf install -y cairo"
      elif command -v pacman >/dev/null; then
        warn "libcairo missing. Run:  sudo pacman -S --noconfirm cairo"
      else
        warn "libcairo missing — install your distro's cairo package and re-run."
      fi
      ;;
  esac
  return 1
}

if [[ ! -f web/public/icons/icon-512.png ]]; then
  log "generating PWA icons (one-time)"
  if ensure_icon_deps; then
    python3 scripts/generate-pwa-icons.py || warn "icon generation failed — using SVG only"
  else
    warn "skipping PNG icons — PWA will use SVG icons only"
  fi
fi

# ---- Start both servers ---------------------------------------------------

BACKEND_LOG="$ROOT/.venv/uvicorn.log"
: > "$BACKEND_LOG"

log "starting backend (uvicorn) on http://127.0.0.1:8765 — auto-reloads on .py changes"
# --reload makes uvicorn watch the project tree and restart the worker on any
# Python file change. Without this, edits to agents/*.py or server/*.py just sit
# on disk until the user restarts start.sh — which is exactly the foot-gun we
# kept hitting. --reload-dir is scoped to the project root so we don't watch
# .venv (huge tree, would thrash the file watcher).
uvicorn server.app:app --host 127.0.0.1 --port 8765 \
  --log-level warning \
  --reload --reload-dir . \
  --reload-exclude '.venv/*' \
  --reload-exclude 'web/*' \
  --reload-exclude '**/__pycache__/*' \
  > "$BACKEND_LOG" 2>&1 &
BACKEND_PID=$!

# Wait briefly for backend to be ready (or fail fast)
for _ in $(seq 1 20); do
  if curl -fsS http://127.0.0.1:8765/api/health >/dev/null 2>&1; then
    ok "backend is up"
    break
  fi
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    printf "${DIM}--- backend log ---${RST}\n"
    cat "$BACKEND_LOG"
    die "backend exited before responding"
  fi
  sleep 0.25
done

cleanup() {
  log "stopping backend (pid $BACKEND_PID)"
  kill "$BACKEND_PID" 2>/dev/null || true
  wait "$BACKEND_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

log "starting frontend (vite) on http://127.0.0.1:5173"
log "open in your browser: ${GREEN}http://127.0.0.1:5173${RST}"
echo

(cd web && npm run dev)
