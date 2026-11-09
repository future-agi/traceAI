"""Print the fi-instrumentation-otel floor that traceAI-llamaindex declares.

The LlamaIndex workflow installs exactly this version from PyPI, so the
declared floor is tested instead of this repository's core.
"""

import re
import sys
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[2] / "python/frameworks/llama_index/pyproject.toml"


def main() -> int:
    with PYPROJECT.open("rb") as f:
        spec = tomllib.load(f)["tool"]["poetry"]["dependencies"]["fi-instrumentation-otel"]
    match = re.fullmatch(r">=\s*([0-9]+(?:\.[0-9]+)*)", spec)
    if not match:
        print(f"expected a plain >= floor for fi-instrumentation-otel, got {spec!r}", file=sys.stderr)
        return 1
    print(match.group(1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
