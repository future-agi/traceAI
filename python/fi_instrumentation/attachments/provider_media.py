"""Typed provider media extraction shared by the Python provider instrumentors.

Mirrors ``@traceai/provider-media`` (TypeScript): one helper, thin per-package
bindings, and a per-package capability table that is also the support matrix.

Rules (same as ``media_capture``, which this builds on):

- Typed shapes only: Gemini ``inline_data`` Parts, OpenAI-compatible
  ``image_url`` data URIs and ``input_audio``, Bedrock Converse/Nova
  ``image``/``document``/``video`` byte blocks, Anthropic-on-Bedrock base64
  blocks, Titan/Nova Canvas image fields, Ollama ``images`` and generated-image
  ``b64_json``. No recursive walk of unknown bodies, no URL fetch, no provider
  file or ``gs://``/S3 lookup, no filesystem access.
- ``manager.preflight`` runs BEFORE a source is read, sized beyond its shape,
  decoded or sniffed. A preflight denied only for size is retried once at the
  exact size; a source whose exact type differs from the gate is re-admitted
  under that type. Unused tickets are released at once.
- After admission the leading bytes must match the declared type (a PNG
  declared as ``audio/wav`` is not uploaded): a hide flag that applies to the
  real modality can never be bypassed through a wrong label.
- Local ``TraceConfig`` hide/PII flags, the per-call context policy
  (``media_capture.CAPTURE_POLICY_KEY``) and the owner mark
  (``media_capture.OWNER_KEY``) apply through ``media_capture`` (deny wins).
- Every public function is non-throwing and returns an empty mapping on denial.

Not in ``attachments.__all__``: an adapter-facing module, like ``media_capture``.
"""

import re
from collections.abc import Sequence
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Tuple

from opentelemetry import context as otel_context

from . import media_capture as mc
from .types import Bytes

KiB = 1024
MiB = 1024 * KiB
#: Bound on synchronous base64 decoding, independent of the manager queue (as in TypeScript).
#: Raw bytes the SDK already holds are bounded by the manager alone (``too_large``/``queue_full``).
MAX_DECODED_BYTES = 1 * MiB
MAX_PARTS = 64
DATA_URI_TYPES = ("image/png", "image/jpeg", "image/webp")
IMAGE_TYPES = DATA_URI_TYPES
DOCUMENT_TYPES = ("application/pdf",)
AUDIO_TYPES = ("audio/wav", "audio/mpeg", "audio/ogg", "audio/flac", "audio/webm")
VIDEO_TYPES = ("video/mp4", "video/webm")
# Gemini mime_type spellings that name an F01 type. Anything else (audio/L16 PCM, text/*,
# image/heic, ...) is not captured.
GEMINI_TYPES = {
    **{mime: mime for mime in IMAGE_TYPES + DOCUMENT_TYPES + AUDIO_TYPES + VIDEO_TYPES},
    "audio/mp3": "audio/mpeg", "audio/x-wav": "audio/wav", "audio/wave": "audio/wav",
    "audio/x-flac": "audio/flac", "image/jpg": "image/jpeg",
}
_COMPAT_AUDIO = {"wav": "audio/wav", "mp3": "audio/mpeg"}
_BEDROCK_IMAGE = {"png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg", "webp": "image/webp"}
_BEDROCK_VIDEO = {"mp4": "video/mp4", "webm": "video/webm"}
# Converse / Nova model families whose image and document blocks are qualified. Unknown model
# bodies are never scraped; a model-family name alone is not coverage.
_CONVERSE_MODELS = re.compile(
    r"(?:arn:aws[\w-]*:bedrock:[\w-]+:\d*:(?:inference-profile|foundation-model)/)?"
    r"(?:(?:us|eu|apac|global)\.)?(?:anthropic\.claude-(?:3|sonnet-4|opus-4|haiku-4)|amazon\.nova-(?:lite|pro|premier))",
    re.IGNORECASE)
_NOVA_VIDEO_MODELS = re.compile(
    r"(?:arn:aws[\w-]*:bedrock:[\w-]+:\d*:(?:inference-profile|foundation-model)/)?"
    r"(?:(?:us|eu|apac|global)\.)?amazon\.nova-(?:lite|pro|premier)", re.IGNORECASE)
_ANTHROPIC_MODELS = re.compile(
    r"(?:arn:aws[\w-]*:bedrock:[\w-]+:\d*:(?:inference-profile|foundation-model)/)?"
    r"(?:(?:us|eu|apac|global)\.)?anthropic\.claude-", re.IGNORECASE)
_IMAGE_GENERATION_MODELS = re.compile(
    r"(?:arn:aws[\w-]*:bedrock:[\w-]+:\d*:(?:inference-profile|foundation-model)/)?"
    r"(?:(?:us|eu|apac|global)\.)?amazon\.(?:titan-image-generator-v[12]|nova-canvas-v1)", re.IGNORECASE)
_COMPUTER_TOOL = re.compile(r"computer_\d{8}")
# Vertex publisher image-generation models (Imagen). Other prediction endpoints are never read.
_IMAGEN_ENDPOINT = re.compile(r"/models/(?:imagen-|imagegeneration)")
# Titan Image Generator / Nova Canvas request fields that carry one base64 image each.
_IMAGE_PARAMS = (
    ("inPaintingParams", "image"), ("outPaintingParams", "image"),
    ("colorGuidedGenerationParams", "referenceImage"), ("backgroundRemovalParams", "image"),
    ("textToImageParams", "conditionImage"),
)

EMPTY: Mapping[str, mc.Occurrence] = mc.EMPTY


# ------------------------------------------------------------------ manager and owner
def attachment_manager(manager: Any) -> Any:
    """``manager`` when it can admit, else None. Disabled, missing or foreign objects are inert."""
    return manager if mc.active(manager) else None


def manager_from_kwargs(kwargs: Mapping[str, Any]) -> Any:
    """The shared manager passed as ``attachments`` (or auto's ``attachment_manager``), if it can admit."""
    try:
        manager = kwargs.get("attachments")
        if manager is None:
            manager = kwargs.get("attachment_manager")
        return attachment_manager(manager)
    except Exception:
        return None


def claim(context=None):
    """The context with this call's media marked as owned (``media_capture.OWNER_KEY``).

    An adapter that captures a call's media itself runs the downstream SDK call in this
    context, so nested provider instrumentors (for example OpenAI under LiteLLM) skip it.
    """
    return otel_context.set_value(mc.OWNER_KEY, True, context)


# ------------------------------------------------------------------ type checks
def sniff(head: Any) -> Optional[str]:
    """The F01 type named by leading magic bytes the caller already holds; None when unknown."""
    if not isinstance(head, (bytes, bytearray, memoryview)):
        return None
    head = bytes(head[:16])
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:5] == b"%PDF-":
        return "application/pdf"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "video/mp4"
    return mc.sniff_audio(head)


def consistent(mime: str, data: Any) -> bool:
    """True when the leading bytes are the declared type (EBML may be audio or video WebM)."""
    found = sniff(data)
    if found is None:
        return False
    if found == "audio/webm":
        return mime in ("audio/webm", "video/webm")
    return found == mime


def _modality(mime: str) -> str:
    return mime.split("/", 1)[0]


# ------------------------------------------------------------------ sources
class _Source:
    """A source described from its shape: exact type, exact size, and a read that runs after admission."""

    __slots__ = ("mime", "size", "take")

    def __init__(self, mime: str, size: int, take: Callable[[int], Optional[bytes]]):
        self.mime, self.size, self.take = mime, size, take


def _bytes_source(data: Any, mime: Optional[str]) -> Optional[_Source]:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return None
    size = data.nbytes if isinstance(data, memoryview) else len(data)
    if size <= 0:
        return None
    if mime is None:
        mime = sniff(data[:16])
        if mime is None:
            return None

    def take(max_bytes: int) -> Optional[bytes]:
        return bytes(data) if size <= max_bytes else None

    return _Source(mime, size, take)


def _base64_source(encoded: Any, mime: Optional[str]) -> Optional[_Source]:
    size = mc.encoded_length(encoded, MAX_DECODED_BYTES)
    if not size:
        return None
    decoded = {}

    def decode(max_bytes: int) -> Optional[bytes]:
        if "data" not in decoded:
            decoded["data"] = mc.strict_decode(encoded, max_bytes, MAX_DECODED_BYTES)
        return decoded["data"]

    if mime is None:
        # Untyped base64 (Ollama, Titan): the exact type comes from the decoded magic bytes.
        # Only described after an exact-size image gate was admitted, so this is post-admission.
        data = decode(MAX_DECODED_BYTES)
        mime = sniff(data[:16]) if data else None
        if mime is None:
            return None
    return _Source(mime, size, decode)


def _data_uri_source(value: Any) -> Optional[_Source]:
    if not isinstance(value, str):
        return None
    for mime in DATA_URI_TYPES:
        prefix = "data:%s;base64," % mime
        if value.startswith(prefix):
            return _base64_source(value[len(prefix):], mime)
    return None


# ------------------------------------------------------------------ capture
class ProviderMedia:
    """One adapter observation of one span. Typed extraction only; never raises.

    ``span`` is the real OpenTelemetry span the instrumentor emitted (unwrap any
    ``_WithSpan`` helper first). ``namespace`` keeps positions of different
    adapters on the same span apart.
    """

    def __init__(self, manager, span, namespace: str, *, trace_config=None):
        self.manager, self.span, self.trace_config = attachment_manager(manager), span, trace_config
        self.observation = mc.Observation(self.manager, span, namespace) if self.manager is not None else None
        self._attempted = 0

    @property
    def usable(self) -> bool:
        return self.observation is not None and self.observation.usable

    @property
    def captured(self) -> Mapping[str, mc.Occurrence]:
        if not self.usable or not self.observation.captured:
            return EMPTY
        return MappingProxyType(dict(self.observation.captured))

    # -------------------------------------------------------------- admission
    def _admit(self, position: str, gate_mime: str, gate_bytes: int, describe: Callable[[], Optional[_Source]], *,
               direction: str, purpose: str, role: Optional[str] = None, message_index: Optional[int] = None,
               part_index: Optional[int] = None, tool_call_id: Optional[str] = None,
               error_detail: bool = False, exact: bool = True) -> None:
        """Gate, describe, (re-)admit at the exact type and size, read, check the magic bytes, attach.

        ``exact`` means ``gate_bytes`` is already the source's exact size (bytes length, or the
        decoded length from the base64 shape), so a size denial is final and nothing is described.
        """
        if not self.usable or self._attempted >= MAX_PARTS or gate_bytes <= 0:
            return
        self._attempted += 1
        manager, span = self.manager, self.span

        def policy(mime):
            return mc.local_capture_policy(self.trace_config, direction=direction, image=_modality(mime) == "image",
                                           error_detail=error_detail)

        def capture() -> Optional[mc.Occurrence]:
            taken = []
            attached = False

            def preflight(mime, size):
                ticket = manager.preflight(span, mime_type=mime, direction=direction, max_bytes=size,
                                           purpose=purpose, capture_policy=policy(mime))
                taken.append(ticket)
                return ticket

            try:
                ticket = preflight(gate_mime, gate_bytes)
                # too_large / queue_full come after every privacy, correlation and sampling step,
                # so describing the source to size it exactly is still safe.
                if not ticket.admitted and (exact or ticket.reason not in ("too_large", "queue_full")):
                    return None
                source = describe()
                if source is None or source.size <= 0:
                    return None
                if not ticket.admitted or source.mime != gate_mime or source.size > ticket.max_bytes:
                    ticket.release()
                    ticket = preflight(source.mime, source.size)
                    if not ticket.admitted:
                        return None
                data = source.take(ticket.max_bytes)
                if data is None or not consistent(source.mime, data):
                    return None
                attached = True
                handle = ticket.attach(Bytes(data), **mc._fields(message_index, part_index, role, tool_call_id))
                return mc.Occurrence(handle.occurrence_id, source.mime) if handle.admission == "queued" else None
            except Exception:
                return None
            finally:
                if not attached:
                    for ticket in taken:
                        try:
                            ticket.release()
                        except Exception:
                            pass

        try:
            self.observation.visit(position, capture)
        except Exception:
            pass

    def _typed(self, position: str, data: Any, mime: str, **fields) -> None:
        """Bytes or raw base64 of a declared type."""
        if isinstance(data, (bytes, bytearray, memoryview)):
            size = data.nbytes if isinstance(data, memoryview) else len(data)
            self._admit(position, mime, size, lambda: _bytes_source(data, mime), **fields)
        elif isinstance(data, str):
            size = mc.encoded_length(data, MAX_DECODED_BYTES)
            if size:
                self._admit(position, mime, size, lambda: _base64_source(data, mime), **fields)

    def _untyped_image(self, position: str, data: Any, **fields) -> None:
        """Bytes or raw base64 with no declared type: one image gate, then the magic bytes decide."""
        if isinstance(data, (bytes, bytearray, memoryview)):
            size = data.nbytes if isinstance(data, memoryview) else len(data)
            self._admit(position, "image/png", size, lambda: _image_only(_bytes_source(data, None)), **fields)
        elif isinstance(data, str):
            size = mc.encoded_length(data, MAX_DECODED_BYTES)
            if size:
                self._admit(position, "image/png", size, lambda: _image_only(_base64_source(data, None)), **fields)

    def _data_uri(self, position: str, read: Callable[[], Any], **fields) -> None:
        # Every candidate type is an image, so one image gate decides the part before the
        # string is parsed. A remote or signed URL is never kept as fallback metadata.
        self._admit(position, DATA_URI_TYPES[0], MAX_DECODED_BYTES, lambda: _data_uri_source(read()), exact=False,
                    **fields)

    # -------------------------------------------------------------- Gemini
    def gemini_contents(self, contents: Any, direction: str = "input") -> "ProviderMedia":
        """Gemini ``Content``/``Part`` values: ``inline_data`` only (``file_data`` URIs are never resolved)."""
        if not self.usable:
            return self
        try:
            values = _items(contents) if isinstance(contents, Sequence) and not isinstance(contents, str) else [contents]
            for m, content in enumerate(values):
                parts = mc.get(content, "parts")
                if parts is None and _inline(content) is not None:
                    parts = [content]  # A bare Part (or list of Parts) is one single-part message.
                role = mc.get(content, "role")
                role = "assistant" if role == "model" or direction == "output" else "user"
                for p, part in enumerate(_items(parts)[:MAX_PARTS]):
                    inline = _inline(part)
                    if inline is None:
                        continue
                    mime = GEMINI_TYPES.get(_lower(mc.get(inline, "mime_type") or mc.get(inline, "mimeType")))
                    if mime is None:
                        continue
                    self._typed("gemini/%s/%d/%d" % (direction, m, p), mc.get(inline, "data"), mime,
                                direction=direction, purpose=_purpose(direction, mime), role=role,
                                message_index=m, part_index=p)
        except Exception:
            pass
        return self

    def gemini_response(self, response: Any) -> "ProviderMedia":
        """Completed ``GenerateContentResponse``: candidate ``inline_data`` (generated images, audio, files)."""
        if not self.usable:
            return self
        try:
            candidates = _items(mc.get(response, "candidates"))[:MAX_PARTS]
            self.gemini_contents([mc.get(candidate, "content") for candidate in candidates], "output")
        except Exception:
            pass
        return self

    # -------------------------------------------------------------- OpenAI-compatible chat
    def compatible_messages(self, messages: Any, dialect: str = "openai") -> "ProviderMedia":
        """Chat ``image_url`` data URIs, plus ``input_audio`` (wav/mp3) for OpenAI-dialect routes.

        ``groq``, ``mistral`` and ``image`` dialects are image only; ``mistral`` also accepts
        ``image_url`` as a plain string.
        """
        if not self.usable:
            return self
        try:
            for m, message in enumerate(_items(messages)[:MAX_PARTS]):
                role = _role(mc.get(message, "role"))
                for p, part in enumerate(_items(mc.get(message, "content"))[:MAX_PARTS]):
                    kind = mc.get(part, "type")
                    fields = dict(direction="input", purpose="model_input", role=role, message_index=m, part_index=p)
                    if kind == "image_url":
                        def read(part=part):
                            image = mc.get(part, "image_url")
                            if image is None and dialect == "mistral":
                                image = mc.get(part, "imageUrl")
                            return image if isinstance(image, str) else mc.get(image, "url")

                        self._data_uri("chat/%d/%d" % (m, p), read, **fields)
                    elif kind == "input_audio" and dialect == "openai":
                        audio = mc.get(part, "input_audio")
                        mime = _COMPAT_AUDIO.get(mc.get(audio, "format"))
                        if mime is not None:
                            self._typed("chat/%d/%d" % (m, p), mc.get(audio, "data"), mime, **fields)
        except Exception:
            pass
        return self

    def ollama_images(self, images: Any, *, message_index: Optional[int] = None, role: str = "user") -> "ProviderMedia":
        """Ollama ``images``: bytes or base64 only. Path strings and ``Path`` objects are never opened."""
        if not self.usable:
            return self
        try:
            for p, image in enumerate(_items(images)[:MAX_PARTS]):
                value = mc.get(image, "value") if not isinstance(image, (str, bytes, bytearray)) else image
                position = "ollama/%s/%d" % ("-" if message_index is None else message_index, p)
                self._untyped_image(position, value, direction="input", purpose="model_input", role=role,
                                    message_index=message_index, part_index=p)
        except Exception:
            pass
        return self

    def ollama_messages(self, messages: Any) -> "ProviderMedia":
        if not self.usable:
            return self
        try:
            for m, message in enumerate(_items(messages)[:MAX_PARTS]):
                self.ollama_images(mc.get(message, "images"), message_index=m, role=_role(mc.get(message, "role")))
        except Exception:
            pass
        return self

    def generated_images(self, data: Any, *, field: str = "b64_json") -> "ProviderMedia":
        """Generated-image results that carry base64 (``b64_json``); URL results are never fetched."""
        if not self.usable:
            return self
        try:
            for index, item in enumerate(_items(data)[:MAX_PARTS]):
                self._untyped_image("generated/%d" % index, mc.get(item, field), direction="output",
                                    purpose="model_output", role="assistant", part_index=index)
        except Exception:
            pass
        return self

    # -------------------------------------------------------------- Bedrock
    def bedrock_converse(self, model_id: Any, messages: Any, additional_fields: Any = None) -> "ProviderMedia":
        """Converse (and Nova InvokeModel) ``image``/``document``/``video`` blocks of qualified model families."""
        if not self.usable or not isinstance(model_id, str) or not _CONVERSE_MODELS.match(model_id):
            return self
        try:
            video = bool(_NOVA_VIDEO_MODELS.match(model_id))
            messages = _items(messages)[:MAX_PARTS]
            computer = _computer_calls(messages, mc.get(additional_fields, "tools"), "toolUse", "toolUseId",
                                       nested="toolUse")
            for m, message in enumerate(messages):
                role = _role(mc.get(message, "role"))
                for p, block in enumerate(_items(mc.get(message, "content"))[:MAX_PARTS]):
                    self._bedrock_block("bedrock/%d/%d" % (m, p), block, video, role=role, message_index=m,
                                        part_index=p)
                    result = mc.get(block, "toolResult")
                    if result is None:
                        continue
                    tool_id = mc.get(result, "toolUseId")
                    if not isinstance(tool_id, str) or not mc.TOOL_ID.fullmatch(tool_id):
                        continue  # Invalid tool IDs fail closed instead of losing causal identity.
                    for q, child in enumerate(_items(mc.get(result, "content"))[:MAX_PARTS]):
                        self._bedrock_block("bedrock/%d/%d/%d" % (m, p, q), child, video, role="tool",
                                            message_index=m, part_index=p, tool_call_id=tool_id,
                                            screenshot=tool_id in computer,
                                            error_detail=mc.get(result, "status") == "error")
        except Exception:
            pass
        return self

    def _bedrock_block(self, position: str, block: Any, video: bool, *, tool_call_id: Optional[str] = None,
                       screenshot: bool = False, error_detail: bool = False, **fields) -> None:
        for kind in ("image", "document", "video"):
            media = mc.get(block, kind)
            if media is None:
                continue
            fmt = _lower(mc.get(media, "format"))
            if kind == "image":
                mime = _BEDROCK_IMAGE.get(fmt)
            elif kind == "document":
                mime = "application/pdf" if fmt == "pdf" else None
            else:
                mime = _BEDROCK_VIDEO.get(fmt) if video else None
            if mime is None:
                return
            # Only inline bytes (raw for Converse, base64 for Nova InvokeModel); S3 locations stay unsupported.
            data = mc.get(mc.get(media, "source"), "bytes")
            if tool_call_id is not None:
                purpose = "computer_screenshot" if screenshot and kind == "image" else "tool_result"
            else:
                purpose = "document" if kind == "document" else "model_input"
            self._typed(position, data, mime, direction="input", purpose=purpose, tool_call_id=tool_call_id,
                        error_detail=error_detail, **fields)
            return

    def anthropic_messages(self, messages: Any, tools: Any = None) -> "ProviderMedia":
        """Anthropic Messages bodies (Bedrock InvokeModel): base64 image/PDF blocks and one tool_result level."""
        if not self.usable:
            return self
        try:
            messages = _items(messages)[:MAX_PARTS]
            computer = _computer_calls(messages, tools, "tool_use", "id")
            for m, message in enumerate(messages):
                role = _role(mc.get(message, "role"))
                for p, block in enumerate(_items(mc.get(message, "content"))[:MAX_PARTS]):
                    if mc.get(block, "type") == "tool_result":
                        tool_id = mc.get(block, "tool_use_id")
                        if not isinstance(tool_id, str) or not mc.TOOL_ID.fullmatch(tool_id):
                            continue
                        for q, child in enumerate(_items(mc.get(block, "content"))[:MAX_PARTS]):
                            self._anthropic_block("anthropic/%d/%d/%d" % (m, p, q), child, role="tool",
                                                  message_index=m, part_index=q, tool_call_id=tool_id,
                                                  screenshot=tool_id in computer,
                                                  error_detail=mc.get(block, "is_error") is True)
                    else:
                        self._anthropic_block("anthropic/%d/%d" % (m, p), block, role=role, message_index=m,
                                              part_index=p)
        except Exception:
            pass
        return self

    def _anthropic_block(self, position: str, block: Any, *, tool_call_id: Optional[str] = None,
                         screenshot: bool = False, error_detail: bool = False, **fields) -> None:
        kind = mc.get(block, "type")
        source = mc.get(block, "source")
        if kind not in ("image", "document") or mc.get(source, "type") != "base64":
            return
        mime = mc.get(source, "media_type")
        if mime not in (IMAGE_TYPES if kind == "image" else DOCUMENT_TYPES):
            return
        if kind == "document":
            purpose = "document"
        elif tool_call_id is not None:
            purpose = "computer_screenshot" if screenshot else "tool_result"
        else:
            purpose = "model_input"
        self._typed(position, mc.get(source, "data"), mime, direction="input", purpose=purpose,
                    tool_call_id=tool_call_id, error_detail=error_detail, **fields)

    def imagen_predictions(self, endpoint: Any, predictions: Any) -> "ProviderMedia":
        """Vertex Imagen ``PredictResponse`` predictions: ``bytesBase64Encoded`` with a declared image type."""
        if not self.usable or not isinstance(endpoint, str) or not _IMAGEN_ENDPOINT.search(endpoint):
            return self
        try:
            for index, prediction in enumerate(_items(predictions)):
                mime = mc.get(prediction, "mimeType")
                if mime in IMAGE_TYPES:
                    self._typed("imagen/output/%d" % index, mc.get(prediction, "bytesBase64Encoded"), mime,
                                direction="output", purpose="model_output", role="assistant", part_index=index)
        except Exception:
            pass
        return self

    def bedrock_invoke_request(self, model_id: Any, body: Any) -> "ProviderMedia":
        """InvokeModel request bodies of named families: Anthropic Messages, Nova, Titan/Nova Canvas images."""
        if not self.usable or not isinstance(model_id, str) or not isinstance(body, Mapping):
            return self
        try:
            if _ANTHROPIC_MODELS.match(model_id) and "anthropic_version" in body:
                self.anthropic_messages(body.get("messages"), body.get("tools"))
            elif _CONVERSE_MODELS.match(model_id):
                self.bedrock_converse(model_id, body.get("messages"))
            elif _IMAGE_GENERATION_MODELS.match(model_id):
                index = 0
                variation = body.get("imageVariationParams")
                for image in _items(mc.get(variation, "images"))[:MAX_PARTS]:
                    self._untyped_image("titan/input/%d" % index, image, direction="input", purpose="model_input",
                                        role="user", part_index=index)
                    index += 1
                for params, field in _IMAGE_PARAMS:
                    image = mc.get(body.get(params), field)
                    if image is not None:
                        self._untyped_image("titan/input/%s" % params, image, direction="input",
                                            purpose="model_input", role="user", part_index=index)
                        index += 1
        except Exception:
            pass
        return self

    def bedrock_invoke_response(self, model_id: Any, body: Any) -> "ProviderMedia":
        """InvokeModel response bodies the instrumentor already buffered: Titan/Nova Canvas ``images``."""
        if not self.usable or not isinstance(model_id, str) or not isinstance(body, Mapping):
            return self
        try:
            if _IMAGE_GENERATION_MODELS.match(model_id):
                for index, image in enumerate(_items(body.get("images"))[:MAX_PARTS]):
                    self._untyped_image("titan/output/%d" % index, image, direction="output",
                                        purpose="model_output", role="assistant", part_index=index)
        except Exception:
            pass
        return self


def _items(value: Any) -> list:
    """A bounded list of a list, tuple or other sequence (proto repeated fields); never a str or bytes."""
    if isinstance(value, (str, bytes, bytearray, memoryview, Mapping)) or not isinstance(value, Sequence):
        return []
    try:
        return list(value[:MAX_PARTS])
    except Exception:
        return []


def _image_only(source: Optional[_Source]) -> Optional[_Source]:
    return source if source is not None and source.mime in IMAGE_TYPES else None


def _inline(part: Any) -> Any:
    inline = mc.get(part, "inline_data")
    return inline if inline is not None else mc.get(part, "inlineData")


def _lower(value: Any) -> str:
    return value.lower().split(";", 1)[0].strip() if isinstance(value, str) else ""


def _role(value: Any) -> str:
    return value if value in ("user", "assistant", "tool", "system") else "unknown"


def _purpose(direction: str, mime: str) -> str:
    if direction == "output":
        return "model_output"
    return "document" if mime in DOCUMENT_TYPES else "model_input"


def _computer_calls(messages, tools, use_type: str, id_field: str, nested: Optional[str] = None) -> frozenset:
    """Tool-call IDs of assistant calls to a declared typed computer tool (``computer_YYYYMMDD``)."""
    names = set()
    for tool in _items(tools):
        kind, name = mc.get(tool, "type"), mc.get(tool, "name")
        if isinstance(kind, str) and _COMPUTER_TOOL.fullmatch(kind) and isinstance(name, str):
            names.add(name)
    if not names:
        return frozenset()
    calls = set()
    for message in messages:
        if mc.get(message, "role") != "assistant":
            continue
        for block in _items(mc.get(message, "content")):
            use = mc.get(block, nested) if nested else (block if mc.get(block, "type") == use_type else None)
            call_id = mc.get(use, id_field)
            if use is not None and isinstance(call_id, str) and mc.get(use, "name") in names:
                calls.add(call_id)
    return frozenset(calls)


# ------------------------------------------------------------------ capability matrix
def _descriptor(operations: Tuple[str, ...], sources: Tuple[str, ...], unsupported: Tuple[str, ...],
                note: Optional[str] = None) -> Mapping[str, Any]:
    value = {"schema_version": 1, "operations": operations, "sources": sources, "unsupported": unsupported,
             "max_inline_bytes": MAX_DECODED_BYTES}
    if note:
        value["note"] = note
    return MappingProxyType(value)


_COMPAT_UNSUPPORTED = ("remote_url", "provider_file", "streamed_output", "output_media")

#: Typed per-package capability descriptors; this IS the Python support matrix. Anything not listed
#: (remote URLs, provider file IDs, gs://, S3, PIL images, async/long-running operations, unknown model
#: bodies, native speech/OCR/image routes) is unsupported and never captured. A package with no
#: operations has no qualified media surface: it keeps tracing and captures nothing.
PROVIDER_MEDIA_CAPABILITIES: Mapping[str, Mapping[str, Any]] = MappingProxyType({
    "traceai_google_genai": _descriptor(
        ("models.generate_content.input", "models.generate_content.output", "models.generate_content_stream.input"),
        ("sdk_bytes", "inline_base64"),
        ("file_data_uri", "gs_uri", "provider_file", "pil_image", "streamed_output", "models.generate_images",
         "models.generate_videos", "live", "audio/L16")),
    "traceai_vertexai": _descriptor(
        ("GenerativeModel.generate_content.input", "GenerativeModel.generate_content.output",
         "ImageGenerationModel.generate_images.output"),
        ("sdk_bytes",),
        ("file_data_uri", "gs_uri", "streamed_output", "ImageGenerationModel.edit_image",
         "prediction_service_raw", "audio/L16")),
    "traceai_bedrock": _descriptor(
        ("converse.input", "invoke_model.anthropic.input", "invoke_model.nova.input",
         "invoke_model.image_generation.input", "invoke_model.image_generation.output"),
        ("sdk_bytes", "inline_base64"),
        ("s3_location", "converse_stream", "invoke_model_with_response_stream", "unknown_model_body",
         "video.anthropic", "converse.output")),
    "traceai_litellm": _descriptor(
        ("completion.input", "acompletion.input", "image_generation.output", "aimage_generation.output"),
        ("data_uri", "inline_base64"),
        ("remote_url", "provider_file", "streamed_output", "image_generation.url", "image_edit",
         "speech", "transcription")),
    "traceai_groq": _descriptor(("chat.completions.create.input",), ("data_uri",), _COMPAT_UNSUPPORTED),
    "traceai_together": _descriptor(("chat.completions.create.input",), ("data_uri",),
                                    _COMPAT_UNSUPPORTED + ("images.generate",)),
    "traceai_fireworks": _descriptor(("chat.completions.create.input",), ("data_uri",), _COMPAT_UNSUPPORTED),
    "traceai_xai": _descriptor(("chat.completions.create.input",), ("data_uri",), _COMPAT_UNSUPPORTED),
    "traceai_mistralai": _descriptor(("chat.complete.input", "chat.stream.input"), ("data_uri",),
                                     _COMPAT_UNSUPPORTED + ("ocr", "document_url", "input_audio")),
    "traceai_openrouter": _descriptor(("chat.send.input",), ("data_uri", "inline_base64"), _COMPAT_UNSUPPORTED),
    "traceai_ollama": _descriptor(("chat.input", "generate.input"), ("sdk_bytes", "inline_base64"),
                                  ("image_path", "streamed_output", "output_media")),
    "traceai_portkey": _descriptor(("chat.completions.create.input",), ("data_uri", "inline_base64"),
                                   _COMPAT_UNSUPPORTED),
    "traceai_deepseek": _descriptor((), (), ("chat.completions.create",),
                                    "DeepSeek chat has no qualified media input or output; tracing only."),
    "traceai_cerebras": _descriptor((), (), ("chat.completions.create",),
                                    "Cerebras chat has no qualified media input or output; tracing only."),
})


def capabilities(package: str) -> Mapping[str, Any]:
    """The descriptor of one package; an empty-operations descriptor for unknown names."""
    return PROVIDER_MEDIA_CAPABILITIES.get(package) or _descriptor((), (), (), "not qualified")
