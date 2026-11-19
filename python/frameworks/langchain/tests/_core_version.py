"""Tests of the echo and raw-output rules need the current core's masking of input settings."""
from importlib.metadata import version

import pytest

_parts = version("fi-instrumentation-otel").split(".")
CORE_MASKS_INPUT_SETTINGS = (int(_parts[0]), int(_parts[1])) >= (1, 3)

requires_current_core = pytest.mark.skipif(
    not CORE_MASKS_INPUT_SETTINGS,
    reason="needs fi-instrumentation-otel >= 1.3.0: older cores do not mask input.value under the message and text settings",
)
