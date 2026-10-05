"""Test fixture: the recipe's spans without ``check_environment()``.

The contract test runs this to record what MLflow 3.16.1 itself does with
the settings ``src/app.py`` refuses (a URL without ``/v1/traces``, no
protocol, ``OTEL_EXPORTER_OTLP_ENDPOINT``). Not part of the recipe.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from app import answer  # noqa: E402

print(answer(sys.argv[1] if len(sys.argv) > 1 else "What is the refund window?"))
