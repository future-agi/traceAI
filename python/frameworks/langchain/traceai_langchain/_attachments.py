"""LangChain typed message media through the shared, provider-owned attachment manager.

Mirrors ``@traceai/framework-attachments`` (dialect ``langchain``). Only the typed
content arrays of chat-model messages are observed, in the serialized form the
tracer receives (``dumpd`` messages under ``kwargs.content``):

- OpenAI-style ``image_url`` / ``file`` / ``input_audio`` parts with inline data;
- LangChain 0.3 standard blocks (``source_type: "base64"``);
- LangChain 1.x content blocks (``base64`` + ``mime_type``);
- Anthropic-style ``source: {"type": "base64"}`` blocks.

Remote URLs go to the core's ``RemoteUrl`` policy and are never fetched; provider
file IDs and anything else are ignored.

One model call, one owner: while attachments are enabled, ``BaseChatModel``
``generate`` / ``agenerate`` / ``stream`` / ``astream`` run under the owner mark
(``media_capture.owned_call``), so a provider instrumentor beneath LangChain
(``traceai_openai``, ...) uploads nothing for that call; the LangChain span holds
the occurrence. A DIFFERENT call with the same bytes gets its own occurrence (byte
deduplication is server-side). The mark also carries LangChain's hide flags down:
what the LangChain span may not capture, the provider span does not capture either.

This module imports ``fi_instrumentation.attachments``; the tracer imports it only
when an enabled attachment manager is passed.
"""

from types import MappingProxyType
from typing import Any, Iterator, List, Mapping, Optional, Tuple

from fi_instrumentation.attachments.media_capture import (
    AUDIO_MIMES,
    DOCUMENT_MIMES,
    EMPTY,
    IMAGE_MIMES,
    MAX_FRAMEWORK_BYTES,
    MAX_FRAMEWORK_PARTS,
    MediaPart,
    Occurrence,
    call_owner,
    capture_parts,
    get,
    items,
    owned_stream,
    owner_scope,
)
from wrapt import FunctionWrapper

__all__ = ("capture_langchain_messages", "langchain_attachment_capabilities", "own_chat_model_calls",
           "release_chat_model_calls")

langchain_attachment_capabilities = MappingProxyType({
    "schema_version": 1,
    "operations": ("chat_model.input", "chat_model.output", "tool_message.input"),
    "sources": ("inline_base64", "data_uri"),
    "unsupported": ("remote_url_fetch", "provider_file", "local_path", "streamed_output", "audio_output"),
    "max_part_bytes": MAX_FRAMEWORK_BYTES,
    "max_parts_per_span": MAX_FRAMEWORK_PARTS,
})

_ROLES = {"HumanMessage": "user", "AIMessage": "assistant", "SystemMessage": "system", "ToolMessage": "tool",
          "HumanMessageChunk": "user", "AIMessageChunk": "assistant", "ToolMessageChunk": "tool"}
_AUDIO_FORMATS = {"wav": "audio/wav", "mp3": "audio/mpeg"}


def _family(mime: Any, default: tuple) -> Optional[tuple]:
    """The admitted MIME list for a declared type, the declared type first; None when unsupported."""
    if mime is None:
        return default
    if not isinstance(mime, str):
        return None
    for family in (IMAGE_MIMES, DOCUMENT_MIMES, AUDIO_MIMES):
        if mime in family:
            return (mime,) + tuple(m for m in family if m != mime)
    return None


def _purpose(mimes: tuple, direction: str, role: str) -> str:
    if direction == "output":
        return "model_output"
    if role == "tool":
        return "tool_result"
    return "document" if mimes[0] == "application/pdf" else "model_input"


def _part(block: Any, message_index: int, part_index: int, role: str, direction: str,
          tool_call_id: Optional[str]) -> Optional[MediaPart]:
    kind = get(block, "type")
    found = None  # (source kind, reader, mimes, raw)
    if kind == "image_url":
        image = get(block, "image_url")
        url = image if isinstance(image, str) else get(image, "url")
        if isinstance(url, str) and url.startswith("data:"):
            found = ("inline", lambda: url, IMAGE_MIMES, False)
        elif isinstance(url, str):
            found = ("url", lambda: url, IMAGE_MIMES, True)
    elif kind == "input_audio":
        audio = get(block, "input_audio")
        mimes = _family(_AUDIO_FORMATS.get(get(audio, "format")), ())
        if mimes:
            found = ("inline", lambda: get(audio, "data"), mimes, True)
    elif kind == "file" and isinstance(get(block, "file"), Mapping):
        data = get(get(block, "file"), "file_data")
        if isinstance(data, str) and data.startswith("data:"):
            found = ("inline", lambda: data, DOCUMENT_MIMES, False)
    elif kind in ("image", "audio", "file"):
        default = {"image": IMAGE_MIMES, "audio": AUDIO_MIMES, "file": DOCUMENT_MIMES}[kind]
        source = get(block, "source")
        if isinstance(source, Mapping):
            # Anthropic-style block.
            if get(source, "type") == "base64":
                mimes = _family(get(source, "media_type"), default)
                if mimes:
                    found = ("inline", lambda: get(source, "data"), mimes, True)
            elif get(source, "type") == "url" and isinstance(get(source, "url"), str):
                found = ("url", lambda: get(source, "url"), default, True)
        elif get(block, "source_type") == "base64":
            mimes = _family(get(block, "mime_type"), default)  # LangChain 0.3 standard block
            if mimes:
                found = ("inline", lambda: get(block, "data"), mimes, True)
        elif get(block, "source_type") == "url" and isinstance(get(block, "url"), str):
            found = ("url", lambda: get(block, "url"), default, True)
        elif get(block, "base64") is not None:
            mimes = _family(get(block, "mime_type"), default)  # LangChain 1.x content block
            if mimes:
                found = ("inline", lambda: get(block, "base64"), mimes, True)
        elif isinstance(get(block, "url"), str):
            found = ("url", lambda: get(block, "url"), default, True)
    if found is None:
        return None
    source_kind, read, mimes, raw = found
    return MediaPart(source_kind, read, mimes, _purpose(mimes, direction, role), message_index, part_index,
                     role=role, raw=raw, tool_call_id=tool_call_id)


def _message(message: Any) -> tuple:
    """(role, content, tool_call_id) of a serialized (``dumpd``) or plain message."""
    kwargs = get(message, "kwargs")
    if isinstance(kwargs, Mapping):
        ident = get(message, "id")
        name = ident[-1] if isinstance(ident, list) and ident and isinstance(ident[-1], str) else ""
        role = _ROLES.get(name, "unknown")
        if name.startswith("ChatMessage"):
            native = get(kwargs, "role")
            role = native if native in ("user", "assistant", "tool", "system") else "unknown"
        return role, get(kwargs, "content"), get(kwargs, "tool_call_id")
    role = get(message, "type")
    role = {"human": "user", "ai": "assistant", "system": "system", "tool": "tool"}.get(role, "unknown")
    return role, get(message, "content"), get(message, "tool_call_id")


def _parts(messages: List[Any], direction: str) -> Iterator[MediaPart]:
    for message_index, message in enumerate(items(messages)):
        role, content, tool_call_id = _message(message)
        tool_call_id = tool_call_id if role == "tool" and isinstance(tool_call_id, str) else None
        for part_index, block in enumerate(items(content)):
            if isinstance(block, Mapping):
                part = _part(block, message_index, part_index, role, direction, tool_call_id)
                if part is not None:
                    yield part


def capture_langchain_messages(manager, span, messages: Any, *, direction: str, trace_config=None,
                               capture=None) -> Mapping[str, Occurrence]:
    """Capture typed media of one chat-model run's first message batch or generations.

    ``messages`` is a list of serialized messages. Returns admitted positions keyed
    ``"{direction}/{message_index}/{part_index}"``; empty when no manager, disabled,
    unsampled or denied. Never raises and never changes ``messages``.
    """
    try:
        if not isinstance(messages, list):
            return EMPTY
        return capture_parts(manager, span, "langchain", _parts(messages, direction), direction=direction,
                             trace_configs=(trace_config,), capture=capture)
    except Exception:
        return EMPTY


def run_messages(run: Any, direction: str) -> Optional[list]:
    """The first message batch (input) or the first generation's messages (output) of a run."""
    try:
        if direction == "input":
            batches = get(get(run, "inputs"), "messages")
            first = batches[0] if isinstance(batches, list) and batches else None
            return first if isinstance(first, list) else None
        generations = get(get(run, "outputs"), "generations")
        first = generations[0] if isinstance(generations, list) and generations else None
        if not isinstance(first, list):
            return None
        return [message for message in (get(g, "message") for g in first[:MAX_FRAMEWORK_PARTS])
                if message is not None]
    except Exception:
        return None


# ------------------------------------------------------------------ one call, one owner
# BaseChatModel entry points that run one or more chat-model calls (invoke/batch go through
# generate/agenerate). The tracer captures inside them, so the mark carries this namespace.
_OWNED_METHODS = ("generate", "agenerate", "stream", "astream")
_NAMESPACE = "langchain"


def _owned(manager):
    def wrapper(wrapped, instance, args, kwargs):
        owner = call_owner(manager, _NAMESPACE)
        if owner is None:
            return wrapped(*args, **kwargs)
        with owner_scope(owner):
            result = wrapped(*args, **kwargs)
        # stream/astream return a lazy generator: mark each advance, never across a yield.
        return owned_stream(result, owner)
    return wrapper


def _owned_async(manager):
    async def wrapper(wrapped, instance, args, kwargs):
        owner = call_owner(manager, _NAMESPACE)
        if owner is None:
            return await wrapped(*args, **kwargs)
        with owner_scope(owner):
            return await wrapped(*args, **kwargs)
    return wrapper


def own_chat_model_calls(manager) -> List[Tuple[type, str, Any]]:
    """Run every chat-model call under the owner mark while ``manager`` can admit.

    A provider instrumentor beneath LangChain then uploads nothing for that call; the
    LangChain span owns its media. Returns the patches for ``release_chat_model_calls``.
    """
    from langchain_core.language_models.chat_models import BaseChatModel

    patches = []
    for name in _OWNED_METHODS:
        original = BaseChatModel.__dict__.get(name)
        if original is None:
            continue
        wrapper = _owned_async(manager) if name == "agenerate" else _owned(manager)
        setattr(BaseChatModel, name, FunctionWrapper(original, wrapper))
        patches.append((BaseChatModel, name, original))
    return patches


def release_chat_model_calls(patches: List[Tuple[type, str, Any]]) -> None:
    for cls, name, original in reversed(patches):
        setattr(cls, name, original)
