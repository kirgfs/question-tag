"""Entry point: ``python -m fbot`` starts the site, ``python -m fbot update`` refreshes data once."""
from __future__ import annotations

import argparse
import sys
import threading
import webbrowser


def main() -> None:
    parser = argparse.ArgumentParser(prog="fbot", description="Футбольный value-бот")
    sub = parser.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve", help="запустить сайт (по умолчанию)")
    for p in (parser, serve):
        p.add_argument("--host", default="127.0.0.1", help="адрес (0.0.0.0 — доступ из локальной сети)")
        p.add_argument("--port", type=int, default=8000)
        p.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    sub.add_parser("update", help="скачать данные и пересчитать прогнозы без сайта")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.cmd == "update":
        from . import db, engine

        db.init()
        engine.ECHO = True
        ok = engine.run_update()
        sys.exit(0 if ok else 1)

    import uvicorn

    from .server import app

    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}"
    print(f"\n  Футбольный value-бот запущен: {url}\n  Остановить — Ctrl+C\n", flush=True)
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
