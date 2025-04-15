from typing import Literal, Union

from fi_instrumentation.fi_types import (
    FiMimeTypeValues,
    FiSpanKindValues,
)

FiSpanKind = Union[
    Literal[
        "agent",
        "chain",
        "embedding",
        "evaluator",
        "guardrail",
        "llm",
        "reranker",
        "retriever",
        "tool",
        "unknown",
    ],
    FiSpanKindValues,
]
FiMimeType = Union[
    Literal["application/json", "text/plain"],
    FiMimeTypeValues,
]
