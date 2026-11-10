"""Content guards for semantic attributes not masked by the oldest core.

Keep these explicit even when FITracer masks the same keys in newer releases:
fi-instrumentation-otel 1.2.2 does not cover embedding text, tool schemas or
reranker documents under every applicable privacy flag.
"""

from typing import Any, Iterator, Tuple

from fi_instrumentation import TraceConfig, safe_json_dumps
from fi_instrumentation.fi_types import DocumentAttributes
from opentelemetry.util.types import AttributeValue

from traceai_cohere._utils import _to_dict

# Bound document telemetry, not the request or response seen by the caller.
MAX_DOCUMENTS = 10
MAX_DOCUMENT_CHARS = 4096


def _hide_input_content(config: TraceConfig) -> bool:
    return bool(
        config.hide_inputs or config.hide_input_messages or config.hide_input_text
    )


def _hide_output_content(config: TraceConfig) -> bool:
    return bool(
        config.hide_outputs or config.hide_output_messages or config.hide_output_text
    )


def _document_attributes(document: Any, prefix: str) -> Iterator[Tuple[str, AttributeValue]]:
    # Strings are the v2 format. Preserve all fields of structured v1 documents
    # as JSON so multi-field/rank_fields requests are replayable within the cap.
    content = (
        document if isinstance(document, str) else safe_json_dumps(_to_dict(document))
    )
    yield f"{prefix}.{DocumentAttributes.DOCUMENT_CONTENT}", content[:MAX_DOCUMENT_CHARS]
    if len(content) > MAX_DOCUMENT_CHARS:
        yield f"{prefix}.cohere.truncated", True
