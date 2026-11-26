"""Run the loopback prototype or issue a token for a synthetic agent."""

import argparse
from pathlib import Path

import uvicorn

from .app import LOCAL_WORKSPACE, create_app
from .store import Store, StoreError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("agentcollab.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8000)
    token = commands.add_parser("token", help="Issue or rotate a local agent token")
    token.add_argument("name")
    args = parser.parse_args()
    if args.command == "serve":
        uvicorn.run(create_app(args.db, args.port), host="127.0.0.1", port=args.port, proxy_headers=False)
    else:
        try:
            print(Store(args.db).issue_token(LOCAL_WORKSPACE, args.name))
        except StoreError as error:
            parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
