"""Apply FI's content policy without changing Pydantic's exported field names."""

import wrapt
from fi_instrumentation.fi_types import SpanAttributes

from ._attributes import PydanticAIAttributes as A


# These opaque values must use the same rules as input.value/output.value,
# including the narrower message/text flags. Renaming just the display fields
# or adding masked duplicates would leave the original aliases unprotected.
_CONTENT_ALIASES = {
    A.RUN_PROMPT: SpanAttributes.INPUT_VALUE,
    A.AGENT_INSTRUCTIONS: SpanAttributes.INPUT_VALUE,
    A.GEN_AI_TOOL_DESCRIPTION: SpanAttributes.INPUT_VALUE,
    A.TOOL_ARGS: SpanAttributes.INPUT_VALUE,
    A.RUN_RESULT: SpanAttributes.OUTPUT_VALUE,
    A.TOOL_RESULT: SpanAttributes.OUTPUT_VALUE,
    A.ERROR_MESSAGE: "error.message",
    A.TOOL_ERROR_MESSAGE: "error.message",
    A.RETRY_REASON: "error.message",
    A.VALIDATION_ERROR: "error.message",
}
_MIXED_CONTENT = (A.MESSAGE_CONTENT, A.MESSAGE_TOOL_CALLS)


def _matches(key, prefix):
    return key == prefix or key.startswith(prefix + ".")


class _PydanticTraceConfig(wrapt.ObjectProxy):
    """Delegate to the supplied config, adapting only content-key recognition.

    FITracer and FiSpan share this policy at creation, late updates, events and
    links. All other config behavior, especially derived error details, PII and
    image redaction, remains owned by the original TraceConfig.
    """

    def mask(self, key, value):
        config = self.__wrapped__
        if any(_matches(key, prefix) for prefix in _MIXED_CONTENT):
            # These aliases have no direction: either side's privacy rule wins.
            value = config.mask(SpanAttributes.INPUT_VALUE, value)
            if value is None:
                return None
            return config.mask(SpanAttributes.OUTPUT_VALUE, value)
        for alias, canonical in _CONTENT_ALIASES.items():
            if _matches(key, alias):
                return config.mask(canonical, value)
        return config.mask(key, value)
