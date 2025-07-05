from collections.abc import Sequence
from typing import Any, Dict, List, Literal, TypedDict, Union

from typing_extensions import Required, TypeAlias

from fi_instrumentation.fi_types import (
    FiLLMProviderValues,
    FiLLMSystemValues,
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
FiLLMProvider: TypeAlias = Union[str, FiLLMProviderValues]
FiLLMSystem: TypeAlias = Union[str, FiLLMSystemValues]


class Image(TypedDict, total=False):
    url: str


class TextMessageContent(TypedDict):
    type: Literal["text"]
    text: str


class ImageMessageContent(TypedDict):
    type: Literal["image"]
    image: Image


MessageContent: TypeAlias = Union[TextMessageContent, ImageMessageContent]


class ToolCallFunction(TypedDict, total=False):
    name: str
    arguments: Union[str, Dict[str, Any]]


class ToolCall(TypedDict, total=False):
    id: str
    function: ToolCallFunction


class Message(TypedDict, total=False):
    role: str
    content: str
    contents: "Sequence[MessageContent]"
    tool_call_id: str
    tool_calls: "Sequence[ToolCall]"


class PromptDetails(TypedDict, total=False):
    audio: int
    cache_read: int
    cache_write: int


class TokenCount(TypedDict, total=False):
    prompt: int
    completion: int
    total: int
    prompt_details: PromptDetails


class Tool(TypedDict, total=False):
    json_schema: Required[Union[str, Dict[str, Any]]]


class Embedding(TypedDict, total=False):
    text: str
    vector: List[float]


class Document(TypedDict, total=False):
    content: str
    id: Union[str, int]
    metadata: Union[str, Dict[str, Any]]
    score: float
