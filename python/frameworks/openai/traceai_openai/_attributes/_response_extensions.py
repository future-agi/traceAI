"""Optional Responses extensions exposed by OpenAI-compatible providers.

Perplexity documents search_results and usage.cost at
https://docs.perplexity.ai/docs/agent-api/openai-compatibility .
Do not create synthetic tool/retrieval spans or infer absent usage/costs.
"""

import math
from collections.abc import Iterator, Mapping
from typing import Any

from fi_instrumentation import TraceConfig, safe_json_dumps
from fi_instrumentation.fi_types import DocumentAttributes, SpanAttributes
from opentelemetry.util.types import AttributeValue

# Literal keys keep imports working with core releases predating cost constants.
_COST_FIELDS = {
    "total_cost": "gen_ai.cost.total",
    "input_cost": "gen_ai.cost.input",
    "output_cost": "gen_ai.cost.output",
    "cache_creation_cost": "gen_ai.cost.cache_write",
    "cache_read_cost": "gen_ai.cost.cache_read",
    "tool_calls_cost": "gen_ai.cost.tool_calls",
}
_SEARCH_METADATA_FIELDS = ("url", "title", "date", "last_updated", "source")


def _field(obj: Any, name: str) -> Any:
    # Unknown union members are SDK models at the supported floor; extension
    # children can remain dictionaries. Avoid model_dump on the unknown union.
    return obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)


def response_extension_attributes(
    response: Any,
    config: TraceConfig,
) -> Iterator[tuple[str, AttributeValue]]:
    cost = _field(_field(response, "usage"), "cost")
    for field, attribute in _COST_FIELDS.items():
        value = _field(cost, field)
        if type(value) not in (int, float) or value < 0:
            continue
        try:
            amount = float(value)
        except OverflowError:
            continue
        if math.isfinite(amount):
            yield attribute, amount
    currency = _field(cost, "currency")
    if isinstance(currency, str) and currency:
        yield "gen_ai.cost.currency", currency

    # Apply this before extraction as older fi-instrumentation releases do not
    # recognize retrieval.documents as output. Numeric costs remain available.
    if config.hide_outputs:
        return
    output = _field(response, "output")
    if not isinstance(output, (list, tuple)):
        return
    document_index = 0
    for item in output:
        if _field(item, "type") != "search_results":
            continue
        results = _field(item, "results")
        if not isinstance(results, (list, tuple)):
            continue
        for result in results:
            document = {}
            identifier = _field(result, "id")
            if type(identifier) in (str, int):
                document[DocumentAttributes.DOCUMENT_ID] = str(identifier)
            snippet = _field(result, "snippet")
            if isinstance(snippet, str):
                document[DocumentAttributes.DOCUMENT_CONTENT] = snippet
            metadata = {
                field: value
                for field in _SEARCH_METADATA_FIELDS
                if isinstance((value := _field(result, field)), str)
            }
            if metadata:
                document[DocumentAttributes.DOCUMENT_METADATA] = safe_json_dumps(
                    metadata
                )
            if document:
                prefix = f"{SpanAttributes.RETRIEVAL_DOCUMENTS}.{document_index}."
                for attribute, value in document.items():
                    yield f"{prefix}{attribute}", value
                document_index += 1
