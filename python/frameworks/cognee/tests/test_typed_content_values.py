"""Typed (non-string) values on content keys follow the same hide settings as text."""
import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanContext, TraceFlags

from fi_instrumentation import TraceConfig
from traceai_cognee._exporter import project

INPUT_KEYS = (
    "langfuse.observation.input",
    "memory.query.text",
    "cognee.search.query",
)
OUTPUT_KEYS = ("langfuse.observation.output",)

INPUT_VALUES = [4242, 0.4244, True]
OUTPUT_VALUES = [4243, 0.4245, True]

INPUT_FLAGS = ("hide_inputs", "hide_input_messages", "hide_input_text")
OUTPUT_FLAGS = ("hide_outputs", "hide_output_messages", "hide_output_text")


def _export(attrs, **flags):
    context = SpanContext(1, 1, False, TraceFlags(1))
    span = ReadableSpan("cognee.search", context=context, attributes=attrs)
    return dict(project(span, "typed-values", True, TraceConfig(**flags)).attributes)


def _same(found, sentinel):
    return type(found) is type(sentinel) and found == sentinel


@pytest.mark.parametrize("sentinel", INPUT_VALUES)
def test_typed_input_values_are_exported_without_hide_settings(sentinel):
    exported = _export({key: sentinel for key in INPUT_KEYS})
    assert _same(exported.get("input.value"), sentinel)


@pytest.mark.parametrize("sentinel", OUTPUT_VALUES)
def test_typed_output_values_are_exported_without_hide_settings(sentinel):
    exported = _export({key: sentinel for key in OUTPUT_KEYS})
    assert _same(exported.get("output.value"), sentinel)


@pytest.mark.parametrize("flag", INPUT_FLAGS)
@pytest.mark.parametrize("sentinel", INPUT_VALUES)
def test_typedContentValuesAreMaskedUnderHideInputSettings(flag, sentinel):
    exported = _export({key: sentinel for key in INPUT_KEYS}, **{flag: True})
    assert not _same(exported.get("input.value"), sentinel), (flag, exported)


@pytest.mark.parametrize("flag", OUTPUT_FLAGS)
@pytest.mark.parametrize("sentinel", OUTPUT_VALUES)
def test_typedContentValuesAreMaskedUnderHideOutputSettings(flag, sentinel):
    exported = _export({key: sentinel for key in OUTPUT_KEYS}, **{flag: True})
    assert not _same(exported.get("output.value"), sentinel), (flag, exported)
