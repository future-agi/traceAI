from enum import Enum, auto
from inspect import BoundArguments, Parameter, signature
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
    cast,
    get_type_hints,
)

from fi_instrumentation import safe_json_dumps
from fi_instrumentation.fi_types import (
    DocumentAttributes,
    EmbeddingAttributes,
    FiMimeTypeValues,
    FiSpanKindValues,
    MessageAttributes,
    RerankerAttributes,
    SpanAttributes,
    ToolCallAttributes,
)
from typing_extensions import TypeGuard

if TYPE_CHECKING:
    from haystack import Document
    from haystack.core.component import Component


class ComponentType(Enum):
    GENERATOR = auto()
    EMBEDDER = auto()
    RANKER = auto()
    RETRIEVER = auto()
    PROMPT_BUILDER = auto()
    UNKNOWN = auto()


def _get_component_class_name(component: "Component") -> str:
    """
    Gets the name of the component.
    """
    return str(component.__class__.__name__)


def _get_component_span_name(component_class_name: str) -> str:
    """
    Gets the name of the span for a component.
    """
    return f"{component_class_name}.run"


def _get_component_type(component: "Component") -> ComponentType:
    """
    Haystack has a single `Component` interface that produces unstructured
    outputs. In the absence of typing information, we make a best-effort attempt
    to infer the component type.
    """

    from haystack.components.builders import PromptBuilder

    component_name = _get_component_class_name(component)
    if (run_method := _get_component_run_method(component)) is None:
        return ComponentType.UNKNOWN
    if "Generator" in component_name or _has_generator_output_type(run_method):
        return ComponentType.GENERATOR
    elif "Embedder" in component_name:
        return ComponentType.EMBEDDER
    elif "Ranker" in component_name and _has_ranker_io_types(run_method):
        return ComponentType.RANKER
    elif (
        "Retriever" in component_name or "WebSearch" in component_name
    ) and _has_retriever_io_types(run_method):
        return ComponentType.RETRIEVER
    elif isinstance(component, PromptBuilder):
        return ComponentType.PROMPT_BUILDER
    return ComponentType.UNKNOWN


def _get_component_run_method(component: "Component") -> Optional[Callable[..., Any]]:
    """
    Gets the `run` method for a component (if one exists).
    """
    if callable(run_method := getattr(component, "run", None)):
        return cast(Callable[..., Any], run_method)
    return None


def _get_run_method_output_types(
    run_method: Callable[..., Any],
) -> Optional[Dict[str, type]]:
    """
    Haystack components are decorated with an `output_type` decorator that is
    useful for inferring the component type.

    https://github.com/deepset-ai/haystack/blob/21c507331c98c76aed88cd8046373dfa2a3590e7/haystack/core/component/component.py#L398
    """

    if isinstance(
        (output_types_cache := getattr(run_method, "_output_types_cache", None)), dict
    ):
        return {key: value.type for key, value in output_types_cache.items()}
    return None


def _get_run_method_input_types(
    run_method: Callable[..., Any],
) -> Optional[Dict[str, type]]:
    """
    Gets input types of parameters to the `run` method.
    """
    return get_type_hints(run_method)


def _has_generator_output_type(run_method: Callable[..., Any]) -> bool:
    """
    Uses heuristics to infer if a component has a generator-like `run` method.
    """
    from haystack.dataclasses import ChatMessage

    if (output_types := _get_run_method_output_types(run_method)) is None or (
        replies := output_types.get("replies")
    ) is None:
        return False
    return replies in (List[ChatMessage], List[str], list[ChatMessage], list[str])


def _has_ranker_io_types(run_method: Callable[..., Any]) -> bool:
    """
    Uses heuristics to infer if a component has a ranker-like `run` method.
    """
    from haystack import Document

    if (input_types := _get_run_method_input_types(run_method)) is None or (
        output_types := _get_run_method_output_types(run_method)
    ) is None:
        return False
    has_documents_parameter = input_types.get("documents") in (
        List[Document],
        list[Document],
    )
    outputs_list_of_documents = output_types.get("documents") in (
        List[Document],
        list[Document],
    )
    return has_documents_parameter and outputs_list_of_documents


def _has_retriever_io_types(run_method: Callable[..., Any]) -> bool:
    """
    Uses heuristics to infer if a component has a retriever-like `run` method.

    This is used to find unusual retrievers such as `SerperDevWebSearch`. See:
    https://github.com/deepset-ai/haystack/blob/21c507331c98c76aed88cd8046373dfa2a3590e7/haystack/components/websearch/serper_dev.py#L93
    """
    from haystack import Document

    if (input_types := _get_run_method_input_types(run_method)) is None or (
        output_types := _get_run_method_output_types(run_method)
    ) is None:
        return False
    has_documents_parameter = "documents" in input_types
    outputs_list_of_documents = output_types.get("documents") in (
        List[Document],
        list[Document],
    )
    return not has_documents_parameter and outputs_list_of_documents


def _get_span_kind_attributes(span_kind: str) -> Iterator[Tuple[str, Any]]:
    """
    Yields span kind attributes.
    """
    yield GEN_AI_SPAN_KIND, span_kind


def _get_input_attributes(arguments: Mapping[str, Any]) -> Iterator[Tuple[str, Any]]:
    """
    Yields input attributes.
    """
    masked_arguments = dict(
        _mask_embedding_vectors(key, value) for key, value in arguments.items()
    )
    yield INPUT_MIME_TYPE, JSON
    yield INPUT_VALUE, safe_json_dumps(masked_arguments)


def _get_component_output_attributes(
    response: Mapping[str, Any], component_type: ComponentType
) -> Iterator[Tuple[str, Any]]:
    """
    Yields output attributes.
    """
    if component_type is ComponentType.PROMPT_BUILDER:
        yield from _get_output_attributes_for_prompt_builder(response)
    else:
        yield from _get_output_attributes(response)


def _get_output_attributes(response: Mapping[str, Any]) -> Iterator[Tuple[str, Any]]:
    """
    Yields output attributes.
    """
    masked_response = dict(
        _mask_embedding_vectors(key, value) for key, value in response.items()
    )
    yield OUTPUT_MIME_TYPE, JSON
    yield OUTPUT_VALUE, safe_json_dumps(masked_response)


def _get_llm_input_message_attributes(
    arguments: Mapping[str, Any],
) -> Iterator[Tuple[str, Any]]:
    """
    Extracts input messages.
    """
    from haystack.dataclasses import ChatMessage

    if isinstance(messages := arguments.get("messages"), Sequence) and all(
        map(lambda x: isinstance(x, ChatMessage), messages)
    ):
        for message_index, message in enumerate(messages):
            if (content := message.text) is not None:
                yield (
                    f"{GEN_AI_INPUT_MESSAGES}.{message_index}.{MESSAGE_CONTENT}",
                    content,
                )
            if (role := message.role) is not None:
                yield (
                    f"{GEN_AI_INPUT_MESSAGES}.{message_index}.{MESSAGE_ROLE}",
                    role.value,
                )
            if (name := message.name) is not None:
                yield f"{GEN_AI_INPUT_MESSAGES}.{message_index}.{MESSAGE_NAME}", name
    elif isinstance(prompt := arguments.get("prompt"), str):
        yield f"{GEN_AI_INPUT_MESSAGES}.0.{MESSAGE_CONTENT}", prompt
        yield f"{GEN_AI_INPUT_MESSAGES}.0.{MESSAGE_ROLE}", USER


def _get_llm_output_message_attributes(
    response: Mapping[str, Any],
) -> Iterator[Tuple[str, Any]]:
    """
    Extracts output messages.
    """
    from haystack.dataclasses import ChatMessage

    if not isinstance(replies := response.get("replies"), Sequence):
        return
    for reply_index, reply in enumerate(replies):
        if isinstance(reply, ChatMessage):
            if reply.tool_calls:
                tool_calls = reply.tool_calls
                for tool_call_index, tool_call in enumerate(tool_calls):
                    if (tool_call_arguments := tool_call.arguments) is not None:
                        yield (
                            f"{GEN_AI_OUTPUT_MESSAGES}.{reply_index}.{MESSAGE_TOOL_CALLS}.{tool_call_index}.{TOOL_CALL_FUNCTION_ARGUMENTS_JSON}",
                            safe_json_dumps(tool_call_arguments),
                        )
                    if (tool_name := tool_call.tool_name) is not None:
                        yield (
                            f"{GEN_AI_OUTPUT_MESSAGES}.{reply_index}.{MESSAGE_TOOL_CALLS}.{tool_call_index}.{TOOL_CALL_FUNCTION_NAME}",
                            tool_name,
                        )
            else:
                yield (
                    f"{GEN_AI_OUTPUT_MESSAGES}.{reply_index}.{MESSAGE_CONTENT}",
                    reply.text,
                )
            yield (
                f"{GEN_AI_OUTPUT_MESSAGES}.{reply_index}.{MESSAGE_ROLE}",
                reply.role.value,
            )
        elif isinstance(reply, str):
            yield f"{GEN_AI_OUTPUT_MESSAGES}.{reply_index}.{MESSAGE_CONTENT}", reply
            yield f"{GEN_AI_OUTPUT_MESSAGES}.{reply_index}.{MESSAGE_ROLE}", ASSISTANT


def _get_llm_model_attributes(response: Mapping[str, Any]) -> Iterator[Tuple[str, Any]]:
    """
    Extracts LLM model attributes from response.
    """
    from haystack.dataclasses import ChatMessage

    if (
        isinstance(response_meta := response.get("meta"), Sequence)
        and response_meta
        and (model := response_meta[0].get("model")) is not None
    ):
        yield GEN_AI_REQUEST_MODEL, model
    elif (
        isinstance(replies := response.get("replies"), Sequence)
        and replies
        and isinstance(reply := replies[0], ChatMessage)
        and (model := reply.meta.get("model")) is not None
    ):
        yield GEN_AI_REQUEST_MODEL, model


def _get_llm_token_count_attributes(
    response: Mapping[str, Any],
) -> Iterator[Tuple[str, Any]]:
    """
    Extracts token counts from response.
    """
    from haystack.dataclasses import ChatMessage

    token_usage = None
    if (
        isinstance(response_meta := response.get("meta"), Sequence)
        and response_meta
        and isinstance(response_meta[0], dict)
        and isinstance(usage := response_meta[0].get("usage"), dict)
    ):
        token_usage = usage
    elif (
        isinstance(replies := response.get("replies"), Sequence)
        and replies
        and isinstance(reply := replies[0], ChatMessage)
        and isinstance(usage := reply.meta.get("usage"), dict)
    ):
        token_usage = usage
    if token_usage is not None:
        if (completion_tokens := token_usage.get("completion_tokens")) is not None:
            yield GEN_AI_USAGE_OUTPUT_TOKENS, completion_tokens
        if (prompt_tokens := token_usage.get("prompt_tokens")) is not None:
            yield GEN_AI_USAGE_INPUT_TOKENS, prompt_tokens
        if (total_tokens := token_usage.get("total_tokens")) is not None:
            yield GEN_AI_USAGE_TOTAL_TOKENS, total_tokens


def _get_llm_prompt_template_attributes_from_prompt_builder(
    component: "Component", run_bound_args: BoundArguments
) -> Iterator[Tuple[str, str]]:
    """
    Extracts prompt template attributes from a prompt builder component.

    This duplicates logic from `PromptBuilder.run`. See:
    https://github.com/deepset-ai/haystack/blob/21c507331c98c76aed88cd8046373dfa2a3590e7/haystack/components/builders/prompt_builder.py#L194
    """
    template = (
        t
        if (t := run_bound_args.arguments.get("template")) is not None
        else getattr(component, "_template_string", None)
    )
    if template is not None:
        yield GEN_AI_PROMPT_TEMPLATE_NAME, template
    if (
        template_variables := {
            **run_bound_args.kwargs,
            **(
                tvs
                if isinstance(
                    tvs := run_bound_args.arguments.get("template_variables"), dict
                )
                else {}
            ),
        }
    ) is not None:
        yield GEN_AI_PROMPT_TEMPLATE_VARIABLES, safe_json_dumps(template_variables)


def _get_output_attributes_for_prompt_builder(
    response: Mapping[str, Any],
) -> Iterator[Tuple[str, Any]]:
    """
    Yields output attributes for prompt builder.
    """
    if isinstance(prompt := response.get("prompt"), str):
        yield OUTPUT_MIME_TYPE, TEXT
        yield OUTPUT_VALUE, prompt
    else:
        yield from _get_output_attributes(response)


def _get_reranker_model_attributes(component: "Component") -> Iterator[Tuple[str, Any]]:
    """
    A best-effort attempt to get the model name from a ranker component.
    """
    if isinstance(
        model_name := (
            getattr(component, "model_name", None) or getattr(component, "model", None)
        ),
        str,
    ):
        yield RERANKER_MODEL_NAME, model_name


def _get_reranker_request_attributes(
    arguments: Mapping[str, Any],
) -> Iterator[Tuple[str, Any]]:
    """
    Extracts re-ranker attributes from arguments.
    """
    if isinstance(query := arguments.get("query"), str):
        yield RERANKER_QUERY, query
    if isinstance(top_k := arguments.get("top_k"), int):
        yield RERANKER_TOP_K, top_k
    if _is_list_of_documents(documents := arguments.get("documents")):
        for doc_index, doc in enumerate(documents):
            if (id := doc.id) is not None:
                yield f"{RERANKER_INPUT_DOCUMENTS}.{doc_index}.{DOCUMENT_ID}", id
            if (content := doc.content) is not None:
                yield (
                    f"{RERANKER_INPUT_DOCUMENTS}.{doc_index}.{DOCUMENT_CONTENT}",
                    content,
                )


def _get_reranker_response_attributes(
    response: Mapping[str, Any],
) -> Iterator[Tuple[str, Any]]:
    """
    Extracts re-ranker attributes from response.
    """
    if _is_list_of_documents(documents := response.get("documents")):
        for doc_index, doc in enumerate(documents):
            if (id := doc.id) is not None:
                yield f"{RERANKER_OUTPUT_DOCUMENTS}.{doc_index}.{DOCUMENT_ID}", id
            if (content := doc.content) is not None:
                yield (
                    f"{RERANKER_OUTPUT_DOCUMENTS}.{doc_index}.{DOCUMENT_CONTENT}",
                    content,
                )
            if (score := doc.score) is not None:
                yield f"{RERANKER_OUTPUT_DOCUMENTS}.{doc_index}.{DOCUMENT_SCORE}", score


def _get_retriever_response_attributes(
    response: Mapping[str, Any],
) -> Iterator[Tuple[str, Any]]:
    """
    Extracts retriever-related attributes from the response.
    """
    from haystack import Document

    if (
        (documents := response.get("documents")) is None
        or not isinstance(documents, Sequence)
        or not all(map(lambda x: isinstance(x, Document), documents))
    ):
        return
    for doc_index, doc in enumerate(documents):
        if (content := doc.content) is not None:
            yield f"{RETRIEVAL_DOCUMENTS}.{doc_index}.{DOCUMENT_CONTENT}", content
        if (id := doc.id) is not None:
            yield f"{RETRIEVAL_DOCUMENTS}.{doc_index}.{DOCUMENT_ID}", id
        if (score := doc.score) is not None:
            yield f"{RETRIEVAL_DOCUMENTS}.{doc_index}.{DOCUMENT_SCORE}", score
        if (metadata := doc.meta) is not None:
            yield (
                f"{RETRIEVAL_DOCUMENTS}.{doc_index}.{DOCUMENT_METADATA}",
                safe_json_dumps(metadata),
            )


def _get_embedding_model_attributes(
    component: "Component",
) -> Iterator[Tuple[str, Any]]:
    """
    Yields attributes for embedding model.
    """

    if (
        model := (
            getattr(component, "model", None) or getattr(component, "model_name", None)
        )
    ) and isinstance(model, str):
        yield EMBEDDING_MODEL_NAME, model


def _get_embedding_attributes(
    arguments: Mapping[str, Any], response: Mapping[str, Any]
) -> Iterator[Tuple[str, Any]]:
    """
    Extracts embedding attributes from an embedder response.
    """
    if isinstance(documents := response.get("documents"), Sequence) and all(
        map(_is_embedding_doc, documents)
    ):
        for doc_index, doc in enumerate(documents):
            yield f"{EMBEDDING_EMBEDDINGS}.{doc_index}.{EMBEDDING_TEXT}", doc.content
            yield (
                f"{EMBEDDING_EMBEDDINGS}.{doc_index}.{EMBEDDING_VECTOR}",
                list(doc.embedding),
            )
    elif _is_vector(embedding := response.get("embedding")) and isinstance(
        text := arguments.get("text"), str
    ):
        yield f"{EMBEDDING_EMBEDDINGS}.0.{EMBEDDING_TEXT}", text
        yield (
            f"{EMBEDDING_EMBEDDINGS}.0.{EMBEDDING_VECTOR}",
            list(embedding),
        )


def _is_embedding_doc(maybe_doc: Any) -> bool:
    """
    Returns true if the input is a `haystack.Document` with embedding
    attributes.
    """
    from haystack import Document

    return (
        isinstance(maybe_doc, Document)
        and isinstance(maybe_doc.content, str)
        and _is_vector(maybe_doc.embedding)
    )


def _mask_embedding_vectors(key: str, value: Any) -> Tuple[str, Any]:
    """
    Masks embeddings.
    """
    from haystack import Document

    if isinstance(key, str) and "embedding" in key and _is_vector(value):
        return key, f"<{len(value)}-dimensional vector>"
    if isinstance(value, Document):
        value = value.to_dict()
    if isinstance(value, Mapping):
        value = dict(_mask_embedding_vectors(k, v) for k, v in value.items())
    elif isinstance(value, (list, tuple)):
        value = [_mask_embedding_vectors("", item)[1] for item in value]
    return key, value


def _is_vector(
    value: Any,
) -> TypeGuard[Sequence[Union[int, float]]]:
    """
    Checks for sequences of numbers.
    """

    is_sequence_of_numbers = isinstance(value, Sequence) and all(
        map(lambda x: isinstance(x, (int, float)), value)
    )
    return is_sequence_of_numbers


def _is_list_of_documents(value: Any) -> TypeGuard[Sequence["Document"]]:
    """
    Checks for a list of documents.
    """

    from haystack import Document

    return isinstance(value, Sequence) and all(
        map(lambda x: isinstance(x, Document), value)
    )


def _get_bound_arguments(
    function: Callable[..., Any], *args: Any, **kwargs: Any
) -> BoundArguments:
    """
    Safely returns bound arguments from the current context.
    """
    sig = signature(function)
    accepts_arbitrary_kwargs = any(
        param.kind == Parameter.VAR_KEYWORD for param in sig.parameters.values()
    )
    valid_kwargs = {
        key: value
        for key, value in kwargs.items()
        if accepts_arbitrary_kwargs or key in sig.parameters
    }
    return sig.bind(*args, **valid_kwargs)


CHAIN = FiSpanKindValues.CHAIN.value
EMBEDDING = FiSpanKindValues.EMBEDDING.value
LLM = FiSpanKindValues.LLM.value
RERANKER = FiSpanKindValues.RERANKER.value
RETRIEVER = FiSpanKindValues.RETRIEVER.value

JSON = FiMimeTypeValues.JSON.value
TEXT = FiMimeTypeValues.TEXT.value

ASSISTANT = "assistant"
USER = "user"

DOCUMENT_CONTENT = DocumentAttributes.DOCUMENT_CONTENT
DOCUMENT_ID = DocumentAttributes.DOCUMENT_ID
DOCUMENT_SCORE = DocumentAttributes.DOCUMENT_SCORE
DOCUMENT_METADATA = DocumentAttributes.DOCUMENT_METADATA
EMBEDDING_EMBEDDINGS = SpanAttributes.EMBEDDING_EMBEDDINGS
EMBEDDING_MODEL_NAME = SpanAttributes.EMBEDDING_MODEL_NAME
EMBEDDING_TEXT = EmbeddingAttributes.EMBEDDING_TEXT
EMBEDDING_VECTOR = EmbeddingAttributes.EMBEDDING_VECTOR
INPUT_MIME_TYPE = SpanAttributes.INPUT_MIME_TYPE
INPUT_VALUE = SpanAttributes.INPUT_VALUE
GEN_AI_INPUT_MESSAGES = SpanAttributes.GEN_AI_INPUT_MESSAGES
GEN_AI_REQUEST_PARAMETERS = SpanAttributes.GEN_AI_REQUEST_PARAMETERS
GEN_AI_REQUEST_MODEL = SpanAttributes.GEN_AI_REQUEST_MODEL
GEN_AI_OUTPUT_MESSAGES = SpanAttributes.GEN_AI_OUTPUT_MESSAGES
GEN_AI_PROMPTS = SpanAttributes.GEN_AI_PROMPTS
GEN_AI_PROMPT_TEMPLATE_NAME = SpanAttributes.GEN_AI_PROMPT_TEMPLATE_NAME
GEN_AI_PROMPT_TEMPLATE_VARIABLES = SpanAttributes.GEN_AI_PROMPT_TEMPLATE_VARIABLES
GEN_AI_PROMPT_TEMPLATE_VERSION = SpanAttributes.GEN_AI_PROMPT_TEMPLATE_VERSION
GEN_AI_USAGE_OUTPUT_TOKENS = SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS
GEN_AI_USAGE_INPUT_TOKENS = SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS
GEN_AI_USAGE_TOTAL_TOKENS = SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS
MESSAGE_CONTENT = MessageAttributes.MESSAGE_CONTENT
MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON = (
    MessageAttributes.MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON
)
MESSAGE_FUNCTION_CALL_NAME = MessageAttributes.MESSAGE_FUNCTION_CALL_NAME
MESSAGE_NAME = MessageAttributes.MESSAGE_NAME
MESSAGE_ROLE = MessageAttributes.MESSAGE_ROLE
MESSAGE_TOOL_CALLS = MessageAttributes.MESSAGE_TOOL_CALLS
METADATA = SpanAttributes.METADATA
GEN_AI_SPAN_KIND = SpanAttributes.GEN_AI_SPAN_KIND
OUTPUT_MIME_TYPE = SpanAttributes.OUTPUT_MIME_TYPE
OUTPUT_VALUE = SpanAttributes.OUTPUT_VALUE
RERANKER_INPUT_DOCUMENTS = RerankerAttributes.RERANKER_INPUT_DOCUMENTS
RERANKER_MODEL_NAME = RerankerAttributes.RERANKER_MODEL_NAME
RERANKER_OUTPUT_DOCUMENTS = RerankerAttributes.RERANKER_OUTPUT_DOCUMENTS
RERANKER_QUERY = RerankerAttributes.RERANKER_QUERY
RERANKER_TOP_K = RerankerAttributes.RERANKER_TOP_K
RETRIEVAL_DOCUMENTS = SpanAttributes.RETRIEVAL_DOCUMENTS
GEN_AI_CONVERSATION_ID = SpanAttributes.GEN_AI_CONVERSATION_ID
TAG_TAGS = SpanAttributes.TAG_TAGS
TOOL_CALL_FUNCTION_ARGUMENTS_JSON = ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON
TOOL_CALL_FUNCTION_NAME = ToolCallAttributes.TOOL_CALL_FUNCTION_NAME
USER_ID = SpanAttributes.USER_ID
