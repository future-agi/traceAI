"""Test fixture: a failing span, with and without the recipe's ``--drop-content``.

The contract test runs this to show that MLflow exports the exception text
as the span's status message, and that ``drop_content`` in ``src/app.py``
removes the text and keeps status ERROR. Not part of the recipe.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import mlflow  # noqa: E402

import app  # noqa: E402

app.check_environment()
args = sys.argv[1:]
processors = {"--drop-content": [app.drop_exception_events, app.drop_content], "--drop-events": [app.drop_exception_events]}
if args[:1] and args[0] in processors:
    mlflow.tracing.configure(span_processors=processors[args[0]])
    args = args[1:]
marker = args[0]


@mlflow.trace(name="failing_step")
def failing_step(text: str) -> str:
    raise ValueError("provider rejected the request: " + text)


try:
    failing_step(marker)
except ValueError:
    print("failed as expected")
