"""A test-only fixture: the recipe's model call inside OpenLIT's ``@openlit.trace``.

It runs ``init_tracing()`` from ``src/app.py`` as written (content off), so
the contract test can show which content the ``@openlit.trace`` decorator
records anyway. Not part of the recipe.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import openlit  # noqa: E402
from app import ask, init_tracing  # noqa: E402

init_tracing()


@openlit.trace
def support_request(question: str) -> str:
    return ask(question)


print(support_request(sys.argv[1]))
