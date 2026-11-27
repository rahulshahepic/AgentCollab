"""Run AgentCollab locally or on Railway staging."""

import argparse
import os
from pathlib import Path

import uvicorn

from .app import LOCAL_WORKSPACE, create_app
from .staging import StagingConfig
from .store import Store, StoreError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("agentcollab.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8000)
    commands.add_parser("serve-staging", help="Run the authenticated Railway pilot")
    token = commands.add_parser("token", help="Issue or rotate a local agent token")
    token.add_argument("name")
    args = parser.parse_args()
    if args.command == "serve":
        uvicorn.run(create_app(args.db, args.port), host="127.0.0.1", port=args.port, proxy_headers=False)
    elif args.command == "serve-staging":
        try:
            config = StagingConfig.from_environment(os.environ)
        except ValueError as error:
            parser.exit(1, f"{error}\n")
        uvicorn.run(create_app(config.database, config.port, staging=config), host="0.0.0.0",
                    port=config.port, proxy_headers=False, access_log=False)
    else:
        try:
            print(Store(args.db).issue_token(LOCAL_WORKSPACE, args.name))
        except StoreError as error:
            parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
