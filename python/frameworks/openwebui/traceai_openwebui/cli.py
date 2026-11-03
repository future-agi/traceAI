"""Generate Open WebUI's supported Function import file from the installed package."""

import argparse
import json
from pathlib import Path

from . import __version__


def function_document() -> dict:
    source = f'''"""
title: Traced model
version: {__version__}
license: Apache-2.0
required_open_webui_version: 0.11.4
requirements: traceAI-openwebui=={__version__}
"""
from traceai_openwebui import Pipe as _Pipe

class Pipe(_Pipe):
    pass
'''
    return {
        "id": "traceai_openwebui",
        "name": "Traced model",
        "type": "pipe",
        "content": source,
        "meta": {"description": "Trace individual native model calls in Future AGI"},
        "is_active": False,
        "is_global": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["export-function"])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Never overwrite a user's existing Function export or configuration.
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump([function_document()], handle, indent=2)
        handle.write("\n")
