"""Offline database maintenance and explicit operator reconciliation."""

import argparse
import json
from pathlib import Path

from app.core.config import Settings
from app.db.database import initialize
from app.db.dispatch import inspect_unknown, resolve_unknown
from app.db.models import Action, db
from app.db.repository import cleanup


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    sub.add_parser("cleanup-traces")
    sub.add_parser("list-unknown")
    inspect = sub.add_parser("inspect-action")
    inspect.add_argument("action_id")
    resolve = sub.add_parser("resolve-action")
    resolve.add_argument("action_id")
    resolve.add_argument("--digest", required=True)
    resolve.add_argument("--outcome", choices=["succeeded", "failed"], required=True)
    resolve.add_argument("--operator", required=True)
    resolve.add_argument("--note", required=True)
    resolve.add_argument("--observation-file", type=Path, required=True)
    args = parser.parse_args(argv)
    initialize(Settings().database_path)
    with db.connection_context():
        if args.command == "cleanup-traces":
            print(f"Removed {cleanup()} expired trace records")
        elif args.command == "list-unknown":
            print(
                json.dumps(
                    [
                        inspect_unknown(a.id)
                        for a in Action.select().where(Action.state == "unknown")
                    ],
                    indent=2,
                )
            )
        elif args.command == "inspect-action":
            print(json.dumps(inspect_unknown(args.action_id), indent=2))
        elif args.command == "resolve-action":
            try:
                result = resolve_unknown(
                    args.action_id,
                    args.digest,
                    args.outcome,
                    args.operator,
                    args.note,
                    json.loads(args.observation_file.read_text()),
                )
            except ValueError as exc:
                parser.error(str(exc))
            print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
