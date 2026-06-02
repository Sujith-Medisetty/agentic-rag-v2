"""CLI entry point. Mirrors claw-rag-service `serve` (default) and `ingest`.

Usage:
  python -m rag serve [--host H] [--port P] [--db PATH]
  python -m rag ingest --workspace PATH [--workspace PATH ...] [--db PATH]
"""

from __future__ import annotations

import argparse
import os
import sys

from rag.embed import EmbedConfig
from rag.ingest import run_ingest
from rag.server import serve, DEFAULT_DB


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rag", description="claw-rag-service (python)")
    sub = parser.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="start the HTTP server (default)")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--db", default=None)

    p_ingest = sub.add_parser("ingest", help="index one or more workspaces")
    p_ingest.add_argument("--workspace", action="append", required=True)
    p_ingest.add_argument("--db", default=None)

    args = parser.parse_args(argv)
    cmd = args.cmd or "serve"

    if getattr(args, "db", None):
        os.environ["CLAW_RAG_DB"] = args.db
    db_path = os.getenv("CLAW_RAG_DB", DEFAULT_DB)

    if cmd == "ingest":
        cfg = EmbedConfig.from_env()
        stats = run_ingest(args.workspace, db_path, cfg)
        print(
            f"ingest: files={stats.files_indexed} chunks={stats.chunks_total} "
            f"embeddings={stats.embeddings_written}",
            file=sys.stderr,
        )
        return 0

    serve(host=getattr(args, "host", None), port=getattr(args, "port", None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
