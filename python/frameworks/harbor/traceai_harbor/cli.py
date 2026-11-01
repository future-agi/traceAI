"""Maintained CLI for finished-job import (no credentials in command arguments)."""

import argparse
import json
from dataclasses import asdict

from ._importer import import_job
from ._transport import Settings


def main() -> int:
    parser = argparse.ArgumentParser(prog="traceai-harbor")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser(
        "import-job", help="Import a finished local Harbor job"
    )
    command.add_argument("job_dir")
    command.add_argument("--capture-content", action="store_true")
    args = parser.parse_args()
    try:
        settings = Settings.from_env(capture_content=args.capture_content)
    except ValueError:
        parser.exit(
            2, "Invalid Future AGI configuration; check host FI environment variables\n"
        )
    result = import_job(args.job_dir, settings=settings)
    print(json.dumps(asdict(result), sort_keys=True))
    return 0 if result.ok else 1
