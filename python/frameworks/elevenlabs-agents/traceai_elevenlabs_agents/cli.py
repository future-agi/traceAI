"""``traceai-elevenlabs-agents`` command line.

    traceai-elevenlabs-agents [serve] [--host 127.0.0.1] [--port 8787] [--path /elevenlabs/webhook]
    traceai-elevenlabs-agents backfill --conversation-id conv_... [--force]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import List, Optional

from .backfill import BackfillError, backfill
from .config import ConfigError, ForwarderConfig
from .forwarder import Forwarder
from .server import DEFAULT_PATH, serve


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="traceai-elevenlabs-agents",
        description="Forward ElevenLabs Agents post-call OpenTelemetry traces to Future AGI.",
    )
    sub = parser.add_subparsers(dest="command")
    serve_p = sub.add_parser("serve", help="run the webhook receiver (default)")
    for p, top in ((parser, True), (serve_p, False)):
        # The subcommand copy only overrides what was actually passed after "serve".
        p.add_argument("--host", default="127.0.0.1" if top else argparse.SUPPRESS, help="bind address (default 127.0.0.1)")
        p.add_argument("--port", type=int, default=8787 if top else argparse.SUPPRESS, help="bind port (default 8787)")
        p.add_argument(
            "--path", default=DEFAULT_PATH if top else argparse.SUPPRESS, help="webhook path (default %s)" % DEFAULT_PATH
        )
    back = sub.add_parser("backfill", help="pull one conversation with GET ?format=opentelemetry")
    back.add_argument("--conversation-id", required=True)
    back.add_argument(
        "--force",
        action="store_true",
        help="export even if this conversation was already forwarded (duplicates spans in that trace)",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    command = args.command or "serve"
    try:
        config = ForwarderConfig.from_env(require_webhook_secret=command == "serve")
    except ConfigError as error:
        print("traceai-elevenlabs-agents: {0}".format(error), file=sys.stderr)
        return 2
    forwarder = Forwarder(config)
    if command == "backfill":
        try:
            status, payload = backfill(forwarder, args.conversation_id, force=args.force)
        except BackfillError as error:
            print("traceai-elevenlabs-agents: {0}".format(error), file=sys.stderr)
            return 1
        finally:
            forwarder.store.close()
        print(json.dumps(payload))
        return 0 if status == 200 else 1
    serve(forwarder, host=args.host, port=args.port, path=args.path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
