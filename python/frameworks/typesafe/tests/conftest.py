import os
import sys

import pytest

# Make ``tests._support`` importable when pytest runs from the package root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_HIDE_FLAGS = (
    "FI_HIDE_INPUTS",
    "FI_HIDE_OUTPUTS",
    "FI_HIDE_INPUT_MESSAGES",
    "FI_HIDE_OUTPUT_MESSAGES",
    "FI_HIDE_INPUT_TEXT",
    "FI_HIDE_OUTPUT_TEXT",
    "FI_PII_REDACTION",
)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    """No test sees the caller's TraceConfig flags or TypeSafe settings."""
    for name in _HIDE_FLAGS + ("TYPESAFE_API_KEY", "TYPESAFE_BASE_URL", "TYPESAFE_DEFAULT_MODEL"):
        monkeypatch.delenv(name, raising=False)
