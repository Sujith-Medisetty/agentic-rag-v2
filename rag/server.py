"""HTTP server. Faithful port of claw-rag-service/src/main.rs routes.

Uses the stdlib http.server (no extra deps). Routes:
  GET  /health    -> "ok"
  GET  /v1/stats  -> {"chunks": N, "phase": ...}
  POST /v1/query  -> {"query","top_k"?} => {"hits":[...], "phase":...}
  GET  /          -> minimal HTML inspector
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from rag import db
from rag.embed import EmbedConfig
from rag.search import query_index, QueryRequest, default_top_k

DEFAULT_DB = ".claw-rag/index.sqlite"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

_INDEX_HTML = (
    "<!doctype html><meta charset=utf-8><title>claw-rag</title>"
    "<h1>claw-rag-service (python)</h1>"
    "<p>POST /v1/query with {\"query\":\"...\",\"top_k\":8}</p>"
)


def _db_path() -> Path:
    return Path(os.getenv("CLAW_RAG_DB", DEFAULT_DB))


def _stats(db_path: Path) -> dict:
    if not db_path.is_file():
        return {"chunks": 0, "phase": "1-sqlite-no-db"}
    con = db.connect(db_path)
    try:
        return {"chunks": db.chunk_count(con), "phase": "1-sqlite"}
    finally:
        con.close()


class _Handler(BaseHTTPRequestHandler):
    cfg: EmbedConfig  # set on the server class

    def _send_json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, code: int, text: str, ctype: str = "text/plain") -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self._send_text(200, "ok")
        elif self.path == "/v1/stats":
            self._send_json(200, _stats(_db_path()))
        elif self.path == "/":
            self._send_text(200, _INDEX_HTML, "text/html")
        else:
            self._send_text(404, "not found")

    def do_POST(self):  # noqa: N802
        if self.path != "/v1/query":
            self._send_text(404, "not found")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8"))
            req = QueryRequest(
                query=str(payload.get("query", "")),
                top_k=int(payload.get("top_k", default_top_k())),
            )
        except (ValueError, TypeError):
            self._send_json(400, {"error": "invalid request body"})
            return

        resp = query_index(_db_path(), type(self).cfg, req)
        self._send_json(200, resp.to_dict())

    def log_message(self, *args):  # silence default stderr noise
        pass


def serve(host: str | None = None, port: int | None = None) -> None:
    host = host or os.getenv("CLAW_RAG_HOST", DEFAULT_HOST)
    port = port or int(os.getenv("CLAW_RAG_PORT", str(DEFAULT_PORT)))
    handler = _Handler
    handler.cfg = EmbedConfig.from_env()
    server = ThreadingHTTPServer((host, port), handler)
    print(f"claw-rag-service listening on http://{host}:{port}  (db={_db_path()})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
