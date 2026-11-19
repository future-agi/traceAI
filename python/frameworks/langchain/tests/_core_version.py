"""Core-version handling for the echo and raw-output tests.

The package decides what goes into ``output.value`` on every supported core, so those assertions run on every
core. Only the assertions that the *input* side is masked (``input.value`` and the input message attributes under
the message and text settings) need fi-instrumentation-otel >= 1.3.0: older cores do not mask it."""
import json
from importlib.metadata import version

_parts = version("fi-instrumentation-otel").split(".")
CORE_MASKS_INPUT_SETTINGS = (int(_parts[0]), int(_parts[1])) >= (1, 3)


def dump(spans) -> str:
    """Every span attribute and every event of the spans, as text."""
    return json.dumps([dict(s.attributes) for s in spans], default=str) + json.dumps(
        [[(e.name, dict(e.attributes or {})) for e in s.events] for s in spans], default=str)


def output_side(spans) -> str:
    """The spans' attributes whose name does not mention the input side, plus their events."""
    return json.dumps([{k: v for k, v in dict(s.attributes).items() if "input" not in k.lower()} for s in spans], default=str) + json.dumps(
        [[(e.name, dict(e.attributes or {})) for e in s.events] for s in spans], default=str)


def assert_hidden(spans, token, label=None, setting=None) -> None:
    """The token reaches no output-side attribute on any core, and no attribute at all on a core that masks the input
    side. The images setting hides images, not text, so for it only the output side is checked."""
    assert token not in output_side(spans), label
    if CORE_MASKS_INPUT_SETTINGS and setting != "hide_input_images":
        assert token not in dump(spans), label
