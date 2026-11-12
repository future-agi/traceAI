"""LlamaIndex typed message media through the shared, provider-owned attachment manager.

Mirrors ``@traceai/framework-attachments`` (dialect ``llamaindex``). Only
``ChatMessage.blocks`` (llama-index-core 0.12.3+) of chat-model input and of the
non-streaming chat response are observed:

- ``ImageBlock.image``, ``AudioBlock.audio`` and ``DocumentBlock.data`` held as
  bytes (LlamaIndex stores them base64-encoded; raw bytes are accepted too);
- the magic bytes must match the admitted type.

An ``IOBase`` source is never read, a ``path`` is never opened, and a ``url`` goes
to the core's ``RemoteUrl`` policy without being fetched.

One model call, one owner: while attachments are enabled, an LLM ``chat`` /
``achat`` / ``stream_chat`` / ``astream_chat`` call (the calls whose messages are
observed here) in which this module observes at least one input media part runs
under the owner mark (``media_capture.owned_call``; a stream only while it
advances), so a provider instrumentor beneath LlamaIndex (``traceai_openai``, ...)
uploads nothing for that call; the LlamaIndex span holds the occurrence. A call
with no observed part is not marked: the OpenAI integration inlines a ``path``
image or document as a data URI before the SDK call, so the provider span captures
it. Residual: a call that mixes an observed part with a ``path`` (or ``IOBase``)
one is marked, so the provider skips that part too. Completion
calls are not observed here and are not marked. A DIFFERENT call with the same bytes
gets its own occurrence (byte deduplication is server-side).

This module imports ``fi_instrumentation.attachments``; the handler imports it only
when an enabled attachment manager is passed.
"""

from threading import RLock
from types import MappingProxyType
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence, Tuple

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
    looks_base64,
    owned_stream,
    owner_scope,
)
from wrapt import FunctionWrapper

__all__ = ("ChatOwnership", "capture_llamaindex_messages", "llamaindex_attachment_capabilities",
           "observes_llamaindex_messages")

llamaindex_attachment_capabilities = MappingProxyType({
    "schema_version": 1,
    "operations": ("llm.chat.input", "llm.chat.output"),
    "sources": ("block_bytes",),
    "unsupported": ("io_source", "local_path", "remote_url_fetch", "video"),
    "min_core": "0.12.3",
    "max_part_bytes": MAX_FRAMEWORK_BYTES,
    "max_parts_per_span": MAX_FRAMEWORK_PARTS,
})

_ROLES = ("user", "assistant", "tool", "system")
_AUDIO_FORMATS = {"wav": "audio/wav", "mp3": "audio/mpeg", "ogg": "audio/ogg", "flac": "audio/flac",
                  "webm": "audio/webm"}
# block_type -> (data field, MIME field, default family)
_BLOCKS = {"image": ("image", "image_mimetype", IMAGE_MIMES), "audio": ("audio", None, AUDIO_MIMES),
           "document": ("data", "document_mimetype", DOCUMENT_MIMES)}


def _field(block: Any, name: str) -> Any:
    """A declared model field only: never a property, resolver or ``__getattr__``."""
    try:
        values = vars(block)
    except TypeError:
        return None
    return values.get(name)


def _ordered(mime: Any, family: tuple) -> Optional[tuple]:
    if mime is None:
        return family
    if isinstance(mime, str) and mime in family:
        return (mime,) + tuple(m for m in family if m != mime)
    return None


def _role(message: Any) -> str:
    role = _field(message, "role")
    role = getattr(role, "value", role)
    return role if role in _ROLES else "unknown"


def _parts(messages: Sequence[Any], direction: str) -> Iterator[MediaPart]:
    for message_index, message in enumerate(list(messages)[:MAX_FRAMEWORK_PARTS]):
        role = _role(message)
        blocks = _field(message, "blocks")
        if not isinstance(blocks, list):
            continue
        for part_index, block in enumerate(blocks[:MAX_FRAMEWORK_PARTS]):
            spec = _BLOCKS.get(_field(block, "block_type"))
            if spec is None:
                continue
            data_field, mime_field, family = spec
            if mime_field is not None:
                mimes = _ordered(_field(block, mime_field), family)
            else:
                fmt = _field(block, "format")
                mimes = _ordered(_AUDIO_FORMATS.get(fmt), family) if fmt is not None else family
            if not mimes:
                continue
            purpose = ("model_output" if direction == "output" else "tool_result" if role == "tool"
                       else "document" if family is DOCUMENT_MIMES else "model_input")
            data = _field(block, data_field)
            url = _field(block, "url")
            if isinstance(data, (bytes, bytearray)):
                # Base64 ASCII when it parses strictly; otherwise complete raw bytes.
                kind = "inline" if looks_base64(data) else "bytes"
                yield MediaPart(kind, lambda d=data: d, mimes, purpose, message_index, part_index, role=role,
                                raw=True)
            elif data is None and url is not None and _field(block, "path") is None:
                yield MediaPart("url", lambda u=url: str(u), mimes, purpose, message_index, part_index,
                                role=role)
            # IOBase sources and paths: never read or opened.


def observes_llamaindex_messages(messages: Any) -> bool:
    """True when this module observes at least one input media part (owner-mark gate). Never raises.

    ``path`` and ``IOBase`` sources are never observed, so a call holding only those is
    left to the provider instrumentor. An error means False (no mark): the provider is
    never suppressed by mistake.
    """
    try:
        if not isinstance(messages, (list, tuple)):
            return False
        return next(_parts(messages, "input"), None) is not None
    except Exception:
        return False


def capture_llamaindex_messages(manager, span, messages: Any, *, direction: str, trace_config=None,
                                capture=None) -> Mapping[str, Occurrence]:
    """Capture typed block media of chat messages; returns ``"{dir}/{msg}/{part}"`` positions.

    Never raises and never changes ``messages``.
    """
    try:
        if not isinstance(messages, (list, tuple)):
            return EMPTY
        return capture_parts(manager, span, "llamaindex", _parts(messages, direction), direction=direction,
                             trace_configs=(trace_config,), capture=capture)
    except Exception:
        return EMPTY


# ------------------------------------------------------------------ one call, one owner
# The LLM methods whose messages this module observes (LLMChatStart/End events). Completion
# methods are not observed, so they are never marked.
_SYNC_METHODS = ("chat", "stream_chat")
_ASYNC_METHODS = ("achat", "astream_chat")
_NAMESPACE = "llamaindex"
_MISSING = object()


class ChatOwnership:
    """Run every LLM chat call whose input media is observed here under the owner mark.

    Only while ``manager`` can admit, and only for a call with at least one observed
    input part (``observes_llamaindex_messages``).

    Patched like ``StreamInstrumentation``: every loaded ``BaseLLM`` / ``MultiModalLLM``
    subclass that defines a chat method, plus an ``__init__`` hook for providers imported
    later. A returned stream is marked only while it advances. Install after and uninstall
    before ``StreamInstrumentation``.
    """

    def __init__(self, manager: Any) -> None:
        self._manager = manager
        self._patches: Dict[Tuple[type, str], Any] = {}
        self._lock = RLock()
        self._enabled = True

    def install(self) -> None:
        from llama_index.core.base.llms.base import BaseLLM
        from llama_index.core.multi_modal_llms import MultiModalLLM

        seen = set()

        def visit(cls: type) -> None:
            if cls in seen:
                return
            seen.add(cls)
            self._patch_class(cls)
            for child in cls.__subclasses__():
                visit(child)

        for base in (BaseLLM, MultiModalLLM):
            visit(base)
            self._patch(base, "__init__", self._initialize)

    def _patch_class(self, cls: type) -> None:
        for base in reversed(cls.__mro__):
            for name in _SYNC_METHODS + _ASYNC_METHODS:
                attr = base.__dict__.get(name)
                if attr is not None and not getattr(attr, "__isabstractmethod__", False):
                    self._patch(base, name, self._call_async if name in _ASYNC_METHODS else self._call)

    def _patch(self, cls: type, name: str, wrapper: Any) -> None:
        with self._lock:
            if not self._enabled or (cls, name) in self._patches:
                return
            attr = getattr(cls, name)
            self._patches[cls, name] = cls.__dict__.get(name, _MISSING)
            setattr(cls, name, FunctionWrapper(attr, wrapper))

    def _initialize(self, wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        # After the inner hooks, so a later provider class is patched in install order too.
        result = wrapped(*args, **kwargs)
        if self._enabled:
            self._patch_class(type(instance))
        return result

    def _owner(self, args: Any, kwargs: Any):
        """The mark for a call whose input media this module observes, else None (no mark)."""
        if not self._enabled:
            return None
        try:
            messages = args[0] if args else kwargs.get("messages")
        except Exception:
            return None
        if not observes_llamaindex_messages(messages):
            return None
        return call_owner(self._manager, _NAMESPACE)

    def _call(self, wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        owner = self._owner(args, kwargs)
        if owner is None:
            return wrapped(*args, **kwargs)
        with owner_scope(owner):
            result = wrapped(*args, **kwargs)
        return owned_stream(result, owner)

    async def _call_async(self, wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        owner = self._owner(args, kwargs)
        if owner is None:
            return await wrapped(*args, **kwargs)
        with owner_scope(owner):
            result = await wrapped(*args, **kwargs)
        return owned_stream(result, owner)

    def uninstall(self) -> None:
        self._enabled = False
        with self._lock:
            for (cls, name), original in reversed(list(self._patches.items())):
                attr = cls.__dict__.get(name)
                if (
                    not isinstance(attr, FunctionWrapper)
                    or getattr(attr._self_wrapper, "__self__", None) is not self
                ):
                    continue  # Do not overwrite another instrumentor's patch.
                if original is _MISSING:
                    delattr(cls, name)
                else:
                    setattr(cls, name, original)
            self._patches.clear()
