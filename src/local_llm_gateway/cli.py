"""Foreground server and offline backup commands."""
import argparse
import logging
import os
import sys

from aiohttp import web
from .app import create_app
from .config import Config
from .storage import Store


def main():
    parser = argparse.ArgumentParser(description="Local-only LLM gateway")
    parser.add_argument("--config", default="gateway.local.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    backup = sub.add_parser("backup", help="Stop the gateway first; never overwrites a backup")
    backup.add_argument("destination")
    args = parser.parse_args()
    os.umask(0o077)
    # Access/error logs can contain attacker-controlled headers and request targets.
    for name in ("aiohttp.access", "aiohttp.server"):
        logging.getLogger(name).disabled = True
    try:
        config = Config.load(args.config)
        if args.command == "backup":
            store = Store(config.database)
            try:
                store.backup(args.destination)
            finally:
                store.close()
            print("Backup created")
        else:
            web.run_app(create_app(config), host=config.host, port=config.port,
                        access_log=None, handler_cancellation=True, shutdown_timeout=10)
    except (OSError, ValueError, RuntimeError):
        print("Gateway could not start: check configuration, database ownership and listening port.", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
