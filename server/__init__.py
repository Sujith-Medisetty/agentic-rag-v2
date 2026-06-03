"""
Web backend for agentic-rag-v2 — the sole user-facing entry point.

Modules:
 - app.py — FastAPI HTTP + WebSocket surface
 - db.py — SQLite project / session / message / event store
 - reporter.py — WebReporter bridges ProgressReporter → WebSocket
 - session_runner.py — runs a LangGraph turn in a background task
 - git_autocommit.py — turn-end auto-commit + push helpers
 - auth.py — single-user passcode + signed device tokens
 - schemas.py — Pydantic request/response models

Web-backend state lives at ~/.agentic-rag/ (server.db, auth.json). LangGraph
checkpoints live at ~/.agent/checkpoints.db.
"""
