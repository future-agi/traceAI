"""A test-only fixture: the recipe's model call against a server error.

It runs ``init_tracing()`` and ``ask()`` from ``src/app.py`` as written
(content off). The one change: the OpenAI client is built with
``max_retries=0``, so the fake's error is raised at once instead of being
retried twice with back-off. The error is caught and its type printed, so
the process exits 0. Not part of the recipe.
"""

from __future__ import annotations

import functools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openai import APIStatusError  # noqa: E402

import app  # noqa: E402

app.OpenAI = functools.partial(app.OpenAI, max_retries=0)
app.init_tracing()
try:
    app.ask(sys.argv[1])
except APIStatusError as error:
    print("raised", type(error).__name__, error.status_code)
