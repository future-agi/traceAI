"""Transport-independent core of the wire layer.

Matches an HTTP exchange against the shared catalog (``wire-protocols.json``) and turns the
request and response bodies into span attributes. Transport adapters (httpx, requests,
aiohttp) only collect bytes and timings; everything protocol-specific lives here so the
golden fixtures in ``spec/auto-instrumentation/fixtures`` can exercise it directly.
"""

import base64
import json
import re
import zlib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from fi_instrumentation.auto.wire import _privacy

AttributeValue = Any
Attributes = Dict[str, AttributeValue]

_CATALOG_PATH = Path(__file__).with_name("wire-protocols.json")
_NAMED_GROUP = re.compile(r"\(\?<(?=[A-Za-z_])")

LAYER = "wire"
DEFAULT_MAX_BODY_BYTES = 4 * 1024 * 1024

# Request keys that carry content rather than invocation parameters.
_CONTENT_KEYS = frozenset(
    {"messages", "input", "prompt", "contents", "system", "systemInstruction", "system_instruction",
     "tools", "documents", "text", "file", "transcript", "instructions", "chat_history", "message",
     "preamble", "texts", "inputs", "query", "content", "requests", "audio_url", "url", "file_url", "image_url"}
)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Endpoint:
    id: str
    methods: Tuple[str, ...]
    path: "re.Pattern[str]"
    format: str
    operation: str
    span_kind: str
    providers: Optional[frozenset]
    exclude_providers: frozenset
    only_when: Optional[str]
    default_provider: Optional[str]


@dataclass(frozen=True)
class Provider:
    name: str
    hosts: frozenset
    host_suffixes: Tuple[str, ...]
    host_prefixes: Tuple[str, ...]
    ports: frozenset
    match_all: bool

    def matches(self, host: str, port: Optional[int]) -> bool:
        checks = []
        if self.hosts:
            checks.append(host in self.hosts)
        if self.host_suffixes:
            checks.append(any(host.endswith(s) for s in self.host_suffixes))
        if self.host_prefixes:
            checks.append(any(host.startswith(p) for p in self.host_prefixes))
        if self.ports:
            checks.append(port in self.ports)
        if not checks:
            return False
        return all(checks) if self.match_all else any(checks)


@dataclass(frozen=True)
class Catalog:
    default_provider: str
    providers: Tuple[Provider, ...]
    endpoints: Tuple[Endpoint, ...]


def _portable_regex(pattern: str) -> "re.Pattern[str]":
    # The catalog writes named groups as (?<name>...), which JS, Go, .NET and Java accept;
    # Python spells them (?P<name>...).
    return re.compile(_NAMED_GROUP.sub("(?P<", pattern))


@lru_cache(maxsize=1)
def load_catalog(path: Optional[str] = None) -> Catalog:
    raw = json.loads(Path(path or _CATALOG_PATH).read_text(encoding="utf-8"))
    providers = tuple(
        Provider(
            name=p["name"],
            hosts=frozenset(h.lower() for h in p.get("hosts", ())),
            host_suffixes=tuple(s.lower() for s in p.get("host_suffixes", ())),
            host_prefixes=tuple(s.lower() for s in p.get("host_prefixes", ())),
            ports=frozenset(p.get("ports", ())),
            match_all=p.get("match") == "all",
        )
        for p in raw["providers"]
    )
    endpoints = tuple(
        Endpoint(
            id=e["id"],
            methods=tuple(m.upper() for m in e["methods"]),
            path=_portable_regex(e["path"]),
            format=e["format"],
            operation=e["operation"],
            span_kind=e["span_kind"],
            providers=frozenset(e["providers"]) if e.get("providers") else None,
            exclude_providers=frozenset(e.get("exclude_providers", ())),
            only_when=e.get("only_when"),
            default_provider=e.get("default_provider"),
        )
        for e in raw["endpoints"]
    )
    return Catalog(raw["default_provider"], providers, endpoints)


@dataclass
class Match:
    endpoint: Endpoint
    provider: str
    host: str
    port: Optional[int]
    path: str
    query: Dict[str, List[str]]
    groups: Dict[str, str]


def resolve_provider(host: str, port: Optional[int], catalog: Optional[Catalog] = None) -> Optional[str]:
    """The provider that owns this host, or None for a proxy, gateway or self-hosted server."""
    catalog = catalog or load_catalog()
    host = host.lower()
    # Specificity is global, not catalog order: an exact host always wins over
    # another provider's suffix/prefix or port (notably Ollama's 11434).
    for rule in ("hosts", "host_suffixes", "host_prefixes", "ports"):
        for provider in catalog.providers:
            if provider.match_all and not provider.matches(host, port):
                continue
            values = getattr(provider, rule)
            matched = (host in values if rule == "hosts" else
                       any(host.endswith(v) for v in values) if rule == "host_suffixes" else
                       any(host.startswith(v) for v in values) if rule == "host_prefixes" else
                       port in values)
            if matched:
                return provider.name
    return None


def match(method: str, url: str, catalog: Optional[Catalog] = None) -> Optional[Match]:
    """Return the catalog endpoint for this request, or None when it is not AI traffic."""
    catalog = catalog or load_catalog()
    method = method.upper()
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if port is None:
        port = 443 if parts.scheme == "https" else 80 if parts.scheme == "http" else None
    path = parts.path or "/"
    host_provider: Optional[str] = None
    resolved = False
    for endpoint in catalog.endpoints:
        if method not in endpoint.methods:
            continue
        found = endpoint.path.search(path)
        if not found:
            continue
        if not resolved:
            host_provider, resolved = resolve_provider(host, port, catalog), True
        provider = host_provider or endpoint.default_provider or catalog.default_provider
        if endpoint.providers is not None and provider not in endpoint.providers:
            continue
        if provider in endpoint.exclude_providers:
            continue
        groups = {k: unquote(v) for k, v in found.groupdict().items() if v is not None}
        return Match(endpoint, provider, host, port, path, parse_qs(parts.query), groups)
    return None


# ---------------------------------------------------------------------------
# Exchange data collected by transports
# ---------------------------------------------------------------------------


@dataclass
class FilePart:
    name: str
    filename: Optional[str]
    content_type: Optional[str]
    size: Optional[int]


@dataclass
class RequestData:
    method: str
    url: str
    headers: Dict[str, str] = field(default_factory=dict)
    body: Optional[bytes] = None
    fields: Optional[Dict[str, str]] = None  # multipart text fields
    files: List[FilePart] = field(default_factory=list)
    body_size: Optional[int] = None
    truncated: bool = False
    unavailable: bool = False
    undecoded_encoding: Optional[str] = None
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    decoded: bool = False

    @property
    def content_type(self) -> str:
        return _header(self.headers, "content-type")


@dataclass
class ResponseData:
    status: Optional[int] = None
    headers: Dict[str, str] = field(default_factory=dict)
    body: Optional[bytes] = None
    size: int = 0
    truncated: bool = False
    first_chunk_seconds: Optional[float] = None
    # Cumulative retained-byte offsets with their transport arrival times.
    chunk_arrivals: List[Tuple[int, float]] = field(default_factory=list)
    timing_truncated: bool = False
    error: Optional[BaseException] = None
    incomplete: bool = False
    decoded: bool = False
    undecoded_encoding: Optional[str] = None
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES

    @property
    def content_type(self) -> str:
        return _header(self.headers, "content-type")


@dataclass
class SpanResult:
    name: str
    attributes: Attributes
    ok: bool
    description: Optional[str] = None


def _header(headers: Dict[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name:
            return value.split(";")[0].strip().lower()
    return ""


def is_binary_content_type(content_type: str) -> bool:
    """Audio and other opaque payloads are counted, never buffered."""
    return (
        content_type.startswith(("audio/", "video/", "image/"))
        or content_type in ("application/octet-stream", "application/vnd.amazon.eventstream")
    )


def parse_multipart(body: bytes, content_type_header: str) -> Tuple[Dict[str, str], List[FilePart]]:
    """Extract text fields and file descriptors from a multipart body. File bytes are dropped."""
    found = re.search(r'boundary="?([^";]+)"?', content_type_header)
    fields: Dict[str, str] = {}
    files: List[FilePart] = []
    if not found or len(found.group(1)) > 256:
        return fields, files
    boundary = b"--" + found.group(1).encode()
    start = body.find(boundary)
    # Bound metadata independently of file size, and never copy a file payload.
    for _ in range(128):
        if start < 0:
            break
        start += len(boundary)
        if body[start:start + 2] == b"--":
            break
        if body[start:start + 2] == b"\r\n":
            start += 2
        end = body.find(b"\r\n" + boundary, start)
        if end < 0:
            break
        head_end = body.find(b"\r\n\r\n", start, min(end, start + 8192))
        if head_end < 0:
            start = end + 2
            continue
        headers = body[start:head_end].decode("utf-8", "replace")
        payload_start = head_end + 4
        name = re.search(r'(?:^|[;\s])name="([^"]*)"', headers)
        if name:
            field_name = name.group(1)[:256]
            filename = re.search(r'filename="([^"]*)"', headers)
            if filename:
                ctype = re.search(r"content-type:\s*([^\r\n;]+)", headers, re.I)
                files.append(FilePart(field_name, filename.group(1)[:1024],
                                      ctype.group(1).strip()[:256] if ctype else None, end - payload_start))
            else:
                fields[field_name] = body[payload_start:min(end, payload_start + 65536)].decode("utf-8", "replace")
        start = end + 2
    return fields, files


# ---------------------------------------------------------------------------
# Body decoding
# ---------------------------------------------------------------------------


def _json_or_none(data: Optional[bytes]) -> Any:
    if not data:
        return None
    try:
        return json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None


def parse_sse(data: bytes) -> List[Tuple[Optional[str], Any]]:
    """Decode complete SSE frames only; a trailing unterminated frame is unobserved."""
    events: List[Tuple[Optional[str], Any]] = []
    text = data.decode("utf-8", "replace").replace("\r\n", "\n")
    for block in text.split("\n\n")[:-1]:
        event_name: Optional[str] = None
        data_lines: List[str] = []
        for line in block.split("\n"):
            if not line or line.startswith(":"):
                continue
            key, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if key == "event":
                event_name = value
            elif key == "data":
                data_lines.append(value)
        if not data_lines:
            continue
        payload = "\n".join(data_lines)
        if payload == "[DONE]":
            continue
        try:
            events.append((event_name, json.loads(payload)))
        except ValueError:
            events.append((event_name, payload))
    return events


def _json_object_frames(data: bytes) -> Iterable[Tuple[int, Any]]:
    """Yield complete JSON objects with byte ends, including observed array prefixes.

    Scan bytes so split UTF-8 in a later member cannot hide an earlier complete
    member, and arrival association needs no character-to-byte offset conversion.
    JSON decoding validates each balanced frame; strings/escapes hide delimiters.
    """
    size, index = len(data), 0
    while index < size and data[index] in b" \t\r\n":
        index += 1
    array = index < size and data[index] == ord("[")
    if array:
        index += 1
    while index < size:
        while index < size and data[index] in b" \t\r\n":
            index += 1
        if index == size or data[index] != ord("{"):
            return
        start, depth, quoted, escaped = index, 0, False, False
        for index in range(start, size):
            byte = data[index]
            if quoted:
                if escaped:
                    escaped = False
                elif byte == ord("\\"):
                    escaped = True
                elif byte == ord('"'):
                    quoted = False
            elif byte == ord('"'):
                quoted = True
            elif byte in b"{[":
                depth += 1
            elif byte in b"}]":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    try:
                        payload = json.loads(data[start:end])
                    except (ValueError, UnicodeDecodeError):
                        return
                    yield end, payload
                    index = end
                    break
        else:
            return
        if not array:
            return
        while index < size and data[index] in b" \t\r\n":
            index += 1
        if index == size or data[index] != ord(","):
            return
        index += 1


def parse_ndjson(data: bytes) -> List[Any]:
    items = []
    for line in data.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line:
            try:
                items.append(json.loads(line))
            except ValueError:
                pass
    return items


@dataclass
class Body:
    kind: str  # json | sse | ndjson | text | binary | empty
    json: Any = None
    events: List[Tuple[Optional[str], Any]] = field(default_factory=list)
    text: Optional[str] = None

    @property
    def streamed(self) -> bool:
        return self.kind in ("sse", "ndjson")

    def payloads(self) -> List[Any]:
        return [payload for _, payload in self.events if isinstance(payload, dict)]


def decode_encoded(data: bytes, encoding: str, limit: int) -> Tuple[Optional[bytes], bool]:
    """Decode encoded transport bytes with a hard bound on decompressed output."""
    try:
        if encoding in ("gzip", "deflate"):
            window = 16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS
            try:
                result = zlib.decompressobj(window).decompress(data, limit + 1)
            except zlib.error:
                if encoding != "deflate":
                    raise
                result = zlib.decompressobj(-zlib.MAX_WBITS).decompress(data, limit + 1)
        elif encoding == "br":
            # Python's Brotli/brotlicffi streaming APIs can allocate arbitrarily
            # expanded output in a single process() call, even for one input byte.
            # They offer no hard max-output contract. Treat encoded Brotli as
            # unsupported rather than decompress first and check after allocation.
            # Bytes already decoded by the application's HTTP client remain usable.
            return None, False
        elif encoding == "zstd":
            import io
            import zstandard

            with zstandard.ZstdDecompressor(max_window_size=max(1024, limit // 1024)).stream_reader(io.BytesIO(data)) as reader:
                result = reader.read(limit + 1)
        else:
            return None, False
        return (None, True) if len(result) > limit else (result, False)
    except Exception:
        return None, False


def decode_response(resp: ResponseData) -> Body:
    encoding = _header(resp.headers, "content-encoding")
    if encoding and encoding != "identity" and not resp.decoded and resp.body is not None:
        resp.body, exceeded = decode_encoded(resp.body, encoding, resp.max_body_bytes)
        resp.decoded = True
        resp.truncated = resp.truncated or exceeded
        if resp.body is None and not exceeded:
            resp.undecoded_encoding = encoding
    ctype = resp.content_type
    if is_binary_content_type(ctype):
        return Body("binary")
    if resp.body is None or resp.truncated:
        return Body("empty")
    if ctype == "text/event-stream":
        return Body("sse", events=parse_sse(resp.body))
    if ctype in ("application/x-ndjson", "application/jsonl", "application/ndjson"):
        return Body("ndjson", events=[(None, item) for item in parse_ndjson(resp.body)])
    try:
        return Body("json", json=json.loads(resp.body))
    except (ValueError, UnicodeDecodeError):
        pass
    if resp.body.lstrip().startswith(b"["):
        # Gemini can fail/close after complete array members but before ']'.
        # Retain those frames, never fields from the partial trailing member.
        items = [payload for _, payload in _json_object_frames(resp.body)]
        if items:
            return Body("json", json=items)
    # Ollama streams NDJSON with an application/json content type.
    lines = [line for line in resp.body.splitlines() if line.strip()]
    if len(lines) > 1:
        items = parse_ndjson(resp.body)
        if items:
            return Body("ndjson", events=[(None, item) for item in items])
    return Body("text", text=resp.body.decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# Attribute helpers
# ---------------------------------------------------------------------------


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _put(attrs: Attributes, key: str, value: Any) -> None:
    if value is None or value == "" or value == []:
        return
    if isinstance(value, (dict, list)) and not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
        value = _dumps(value)
    attrs[key] = value


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def text_of(content: Any) -> Optional[str]:
    """Plain text of a message content value: strings, part arrays, or {text} objects."""
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        if "parts" in content:
            return text_of(content["parts"])
        return None
    if isinstance(content, list):
        texts = []
        for part in content:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict):
                kind = part.get("type")
                text = part.get("text")
                if isinstance(text, str) and kind in (None, "text", "input_text", "output_text"):
                    texts.append(text)
        return "\n".join(texts) if texts else None
    return None


@dataclass
class ToolCall:
    id: Optional[str]
    name: Optional[str]
    arguments: Optional[str]


@dataclass
class Message:
    role: str
    content: Optional[str] = None
    tool_calls: List[ToolCall] = field(default_factory=list)
    tool_call_id: Optional[str] = None


def _args_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value if isinstance(value, str) else _dumps(value)


def _put_messages(attrs: Attributes, prefix: str, messages: Iterable[Message]) -> None:
    for i, message in enumerate(messages):
        base = f"{prefix}.{i}.message"
        _put(attrs, f"{base}.role", message.role)
        _put(attrs, f"{base}.content", message.content)
        _put(attrs, f"{base}.tool_call_id", message.tool_call_id)
        for k, call in enumerate(message.tool_calls):
            call_base = f"{base}.tool_calls.{k}.tool_call"
            _put(attrs, f"{call_base}.id", call.id)
            _put(attrs, f"{call_base}.function.name", call.name)
            _put(attrs, f"{call_base}.function.arguments", call.arguments)


@dataclass
class Parsed:
    """What a format parser extracts; turned into attributes by ``build``."""

    request_model: Optional[str] = None
    response_model: Optional[str] = None
    response_id: Optional[str] = None
    finish_reasons: List[str] = field(default_factory=list)
    input_messages: List[Message] = field(default_factory=list)
    output_messages: List[Message] = field(default_factory=list)
    tools: List[Any] = field(default_factory=list)
    params: Dict[str, Any] = field(default_factory=dict)
    usage: Dict[str, Optional[int]] = field(default_factory=dict)
    raw_usage: Dict[str, Any] = field(default_factory=dict)
    usage_family: Optional[str] = None
    input_value: Optional[str] = None
    input_mime: Optional[str] = None
    output_value: Optional[str] = None
    output_mime: Optional[str] = None
    extra: Attributes = field(default_factory=dict)
    skip: bool = False  # parser decided this exchange is not a capture (only_when, etc.)
    streamed: bool = False


def _usage(parsed: Parsed, **values: Any) -> None:
    for key, value in values.items():
        number = _int(value)
        if number is not None:
            parsed.usage[key] = number


def _provider_usage(parsed: Parsed, family: str, raw: Dict[str, Any]) -> None:
    """Merge cumulative snapshots, including explicit null/zero, before A8 math."""
    parsed.usage_family = family

    def merge(target: Dict[str, Any], source: Dict[str, Any], depth: int = 0) -> None:
        if depth > 8:
            return
        for key, value in source.items():
            if isinstance(value, dict):
                if not isinstance(target.get(key), dict):
                    target[key] = {}
                merge(target[key], value, depth + 1)
            else:
                target[key] = value

    merge(parsed.raw_usage, raw)


def _normalized_usage(parsed: Parsed) -> Dict[str, Optional[int]]:
    family, raw = parsed.usage_family, parsed.raw_usage
    out = dict(parsed.usage)
    if family is None:
        return out

    def number(mapping: Dict[str, Any], key: str, optional: bool = False) -> Optional[int]:
        if key not in mapping:
            return 0 if optional else None
        value = mapping[key]
        return value if type(value) is int and value >= 0 else None

    def inclusive(base: str, *additions: str) -> Optional[int]:
        values = [number(raw, base)] + [number(raw, key, True) for key in additions]
        return None if any(value is None for value in values) else sum(values)

    for key in ("input", "output", "total", "cache_read", "cache_write", "reasoning"):
        out.pop(key, None)
    provider_total = number(raw, "total_tokens")
    if family == "anthropic":
        input_count = inclusive("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        output_count = number(raw, "output_tokens")
        out.update(cache_read=number(raw, "cache_read_input_tokens"), cache_write=number(raw, "cache_creation_input_tokens"))
    elif family == "bedrock.converse":
        input_count = inclusive("inputTokens", "cacheReadInputTokens", "cacheWriteInputTokens")
        output_count, provider_total = number(raw, "outputTokens"), number(raw, "totalTokens")
        out.update(cache_read=number(raw, "cacheReadInputTokens"), cache_write=number(raw, "cacheWriteInputTokens"))
    elif family == "gemini":
        input_count = inclusive("promptTokenCount", "toolUsePromptTokenCount")
        output_count = inclusive("candidatesTokenCount", "thoughtsTokenCount")
        provider_total = number(raw, "totalTokenCount")
        out.update(cache_read=number(raw, "cachedContentTokenCount"), reasoning=number(raw, "thoughtsTokenCount"))
    else:
        ikey, okey = (("prompt_tokens", "completion_tokens") if "prompt_tokens" in raw or "completion_tokens" in raw
                      else ("input_tokens", "output_tokens"))
        input_count, output_count = number(raw, ikey), number(raw, okey)
        details, out_details = raw.get(ikey + "_details"), raw.get(okey + "_details")
        out.update(cache_read=number(details, "cached_tokens") if isinstance(details, dict) else None,
                   reasoning=number(out_details, "reasoning_tokens") if isinstance(out_details, dict) else None)
    total = provider_total
    if input_count is not None and output_count is not None:
        total = input_count + output_count
        if provider_total is not None and provider_total != total:
            out["provider_total"] = provider_total
    out.update(input=input_count, output=output_count, total=total)
    return out


# ---------------------------------------------------------------------------
# Request parsers. Each returns False when required keys are missing.
# ---------------------------------------------------------------------------


def _openai_message(raw: Dict[str, Any]) -> Message:
    message = Message(role=str(raw.get("role") or "user"), content=text_of(raw.get("content")))
    message.tool_call_id = raw.get("tool_call_id")
    for call in raw.get("tool_calls") or []:
        function = call.get("function") or {}
        message.tool_calls.append(ToolCall(call.get("id"), function.get("name"), _args_str(function.get("arguments"))))
    return message


def _common_params(body: Dict[str, Any], parsed: Parsed) -> None:
    for key in ("temperature", "top_p", "frequency_penalty", "presence_penalty"):
        if _num(body.get(key)) is not None:
            parsed.params[key] = body[key]
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        if _int(body.get(key)) is not None:
            parsed.params["max_tokens"] = body[key]
            break
    if _int(body.get("seed")) is not None:
        parsed.params["seed"] = body["seed"]


def _req_openai_chat(body: Dict[str, Any], parsed: Parsed, m: Match) -> bool:
    if isinstance(body.get("messages"), list):
        parsed.input_messages = [_openai_message(x) for x in body["messages"] if isinstance(x, dict)]
    elif isinstance(body.get("prompt"), str):
        parsed.input_messages = [Message("user", body["prompt"])]
    else:
        return False
    parsed.tools = list(body.get("tools") or [])
    _common_params(body, parsed)
    return True


def _req_openai_completions(body: Dict[str, Any], parsed: Parsed, m: Match) -> bool:
    prompt = body.get("prompt")
    if prompt is None:
        return False
    text = prompt if isinstance(prompt, str) else "\n".join(p for p in prompt if isinstance(p, str)) if isinstance(prompt, list) else None
    parsed.input_messages = [Message("user", text)]
    _common_params(body, parsed)
    return True


def _req_openai_responses(body: Dict[str, Any], parsed: Parsed, m: Match) -> bool:
    if "input" not in body and "model" not in body:
        return False
    if isinstance(body.get("instructions"), str):
        parsed.input_messages.append(Message("system", body["instructions"]))
    value = body.get("input")
    if isinstance(value, str):
        parsed.input_messages.append(Message("user", value))
    elif isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "function_call_output":
                parsed.input_messages.append(Message("tool", text_of(item.get("output")) or _args_str(item.get("output")), tool_call_id=item.get("call_id")))
            elif kind == "function_call":
                parsed.input_messages.append(Message("assistant", tool_calls=[ToolCall(item.get("call_id"), item.get("name"), _args_str(item.get("arguments")))]))
            elif "role" in item:
                parsed.input_messages.append(Message(str(item["role"]), text_of(item.get("content"))))
    parsed.tools = list(body.get("tools") or [])
    _common_params(body, parsed)
    return True


def _req_openai_embeddings(body: Dict[str, Any], parsed: Parsed, m: Match) -> bool:
    value = body.get("input")
    if value is None:
        return False
    count = len(value) if isinstance(value, list) and value and not isinstance(value[0], int) else 1
    parsed.extra["gen_ai.request.embedding_inputs_count"] = count
    return True


def _req_openai_speech(body: Dict[str, Any], parsed: Parsed, m: Match) -> bool:
    if not isinstance(body.get("input"), str):
        return False
    parsed.input_value, parsed.input_mime = body["input"], "text/plain"
    parsed.extra["gen_ai.audio.transcript"] = body["input"]
    _put(parsed.extra, "gen_ai.request.voice", body.get("voice") if isinstance(body.get("voice"), str) else None)
    return True


def _req_openai_images(body: Dict[str, Any], parsed: Parsed, m: Match) -> bool:
    if not isinstance(body.get("prompt"), str):
        return False
    parsed.input_messages = [Message("user", body["prompt"])]
    return True


def _req_openai_moderations(body: Dict[str, Any], parsed: Parsed, m: Match) -> bool:
    return body.get("input") is not None


def _req_rerank(body: Dict[str, Any], parsed: Parsed, m: Match) -> bool:
    if not isinstance(body.get("query"), str) or not isinstance(body.get("documents"), list):
        return False
    parsed.input_messages = [Message("user", body["query"])]
    _put(parsed.extra, "gen_ai.request.top_n", _int(body.get("top_n") if body.get("top_n") is not None else body.get("top_k")))
    parsed.extra["gen_ai.rerank.documents_count"] = len(body["documents"])
    return True


def _anthropic_blocks(role: str, content: Any) -> Message:
    message = Message(role=role)
    if isinstance(content, str):
        message.content = content
        return message
    texts = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif kind == "tool_use":
            message.tool_calls.append(ToolCall(block.get("id"), block.get("name"), _args_str(block.get("input"))))
        elif kind == "tool_result":
            message.tool_call_id = block.get("tool_use_id")
            result = text_of(block.get("content"))
            if result:
                texts.append(result)
    message.content = "\n".join(texts) if texts else None
    return message


def _req_anthropic(body: Dict[str, Any], parsed: Parsed, m: Match) -> bool:
    if not isinstance(body.get("messages"), list):
        return False
    system = body.get("system")
    if system:
        parsed.input_messages.append(Message("system", text_of(system)))
    for raw in body["messages"]:
        if isinstance(raw, dict):
            parsed.input_messages.append(_anthropic_blocks(str(raw.get("role") or "user"), raw.get("content")))
    parsed.tools = list(body.get("tools") or [])
    _common_params(body, parsed)
    return True


def _gemini_message(raw: Dict[str, Any]) -> Message:
    role = raw.get("role") or "user"
    message = Message("assistant" if role == "model" else str(role))
    texts = []
    for part in raw.get("parts") or []:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("text"), str) and not part.get("thought"):
            texts.append(part["text"])
        elif isinstance(part.get("functionCall"), dict):
            call = part["functionCall"]
            message.tool_calls.append(ToolCall(call.get("id"), call.get("name"), _args_str(call.get("args"))))
        elif isinstance(part.get("functionResponse"), dict):
            reply = part["functionResponse"]
            message.role = "tool"
            message.tool_call_id = reply.get("id") or reply.get("name")
            texts.append(_args_str(reply.get("response")) or "")
    message.content = "\n".join(texts) if texts else None
    return message


def _req_gemini(body: Dict[str, Any], parsed: Parsed, m: Match) -> bool:
    contents = body.get("contents")
    if contents is None:
        return False
    system = body.get("systemInstruction") or body.get("system_instruction")
    if system:
        parsed.input_messages.append(Message("system", text_of(system)))
    if isinstance(contents, str):
        parsed.input_messages.append(Message("user", contents))
    else:
        for raw in contents if isinstance(contents, list) else [contents]:
            if isinstance(raw, dict):
                parsed.input_messages.append(_gemini_message(raw))
    for tool in body.get("tools") or []:
        parsed.tools.append(tool)
    config = body.get("generationConfig") or body.get("generation_config") or {}
    mapping = {"temperature": "temperature", "topP": "top_p", "maxOutputTokens": "max_tokens", "seed": "seed",
               "frequencyPenalty": "frequency_penalty", "presencePenalty": "presence_penalty"}
    for src, dst in mapping.items():
        if _num(config.get(src)) is not None:
            parsed.params[dst] = config[src]
    return True


def _req_gemini_embed(body: Dict[str, Any], parsed: Parsed, m: Match) -> bool:
    if "content" in body:
        parsed.extra["gen_ai.request.embedding_inputs_count"] = 1
        return True
    if isinstance(body.get("requests"), list):
        parsed.extra["gen_ai.request.embedding_inputs_count"] = len(body["requests"])
        return True
    return False


def _req_cohere_chat(body: Dict[str, Any], parsed: Parsed, m: Match) -> bool:
    if isinstance(body.get("messages"), list):
        parsed.input_messages = [_openai_message(x) for x in body["messages"] if isinstance(x, dict)]
    elif isinstance(body.get("message"), str):
        if isinstance(body.get("preamble"), str):
            parsed.input_messages.append(Message("system", body["preamble"]))
        for turn in body.get("chat_history") or []:
            if isinstance(turn, dict):
                role = {"USER": "user", "CHATBOT": "assistant", "SYSTEM": "system"}.get(str(turn.get("role")).upper(), "user")
                parsed.input_messages.append(Message(role, text_of(turn.get("message"))))
        parsed.input_messages.append(Message("user", body["message"]))
    else:
        return False
    parsed.tools = list(body.get("tools") or [])
    _common_params(body, parsed)
    if _num(body.get("p")) is not None:
        parsed.params["top_p"] = body["p"]
    return True


def _req_cohere_embed(body: Dict[str, Any], parsed: Parsed, m: Match) -> bool:
    items = body.get("texts") or body.get("inputs") or body.get("images")
    if not isinstance(items, list):
        return False
    parsed.extra["gen_ai.request.embedding_inputs_count"] = len(items)
    return True


def _bedrock_message(raw: Dict[str, Any]) -> Message:
    message = Message(str(raw.get("role") or "user"))
    texts = []
    for block in raw.get("content") or []:
        if not isinstance(block, dict):
            continue
        if isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif isinstance(block.get("toolUse"), dict):
            use = block["toolUse"]
            message.tool_calls.append(ToolCall(use.get("toolUseId"), use.get("name"), _args_str(use.get("input"))))
        elif isinstance(block.get("toolResult"), dict):
            result = block["toolResult"]
            message.tool_call_id = result.get("toolUseId")
            texts.extend(t for t in (text_of(c) for c in result.get("content") or []) if t)
    message.content = "\n".join(texts) if texts else None
    return message


def _req_bedrock_converse(body: Dict[str, Any], parsed: Parsed, m: Match) -> bool:
    if not isinstance(body.get("messages"), list):
        return False
    system = body.get("system")
    if isinstance(system, list):
        text = "\n".join(t for t in (text_of(b) for b in system) if t)
        if text:
            parsed.input_messages.append(Message("system", text))
    parsed.input_messages.extend(_bedrock_message(x) for x in body["messages"] if isinstance(x, dict))
    config = body.get("inferenceConfig") or {}
    for src, dst in (("temperature", "temperature"), ("topP", "top_p"), ("maxTokens", "max_tokens")):
        if _num(config.get(src)) is not None:
            parsed.params[dst] = config[src]
    tool_config = body.get("toolConfig") or {}
    parsed.tools = list(tool_config.get("tools") or [])
    return True


def _req_bedrock_invoke(body: Any, parsed: Parsed, m: Match) -> bool:
    if isinstance(body, dict) and isinstance(body.get("messages"), list):
        _req_anthropic(body, parsed, m)
    return True


def _req_ollama_chat(body: Dict[str, Any], parsed: Parsed, m: Match) -> bool:
    if not isinstance(body.get("messages"), list):
        return False
    parsed.input_messages = [_openai_message(x) for x in body["messages"] if isinstance(x, dict)]
    parsed.tools = list(body.get("tools") or [])
    _ollama_options(body, parsed)
    return True


def _ollama_options(body: Dict[str, Any], parsed: Parsed) -> None:
    options = body.get("options") or {}
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p"), ("num_predict", "max_tokens"), ("seed", "seed")):
        if _num(options.get(src)) is not None:
            parsed.params[dst] = options[src]


def _req_ollama_generate(body: Dict[str, Any], parsed: Parsed, m: Match) -> bool:
    if not isinstance(body.get("prompt"), str):
        return False
    if isinstance(body.get("system"), str):
        parsed.input_messages.append(Message("system", body["system"]))
    parsed.input_messages.append(Message("user", body["prompt"]))
    _ollama_options(body, parsed)
    return True


def _req_ollama_embed(body: Dict[str, Any], parsed: Parsed, m: Match) -> bool:
    value = body.get("input", body.get("prompt"))
    if value is None:
        return False
    parsed.extra["gen_ai.request.embedding_inputs_count"] = len(value) if isinstance(value, list) else 1
    return True


def _req_text_field(key: str) -> Callable[[Dict[str, Any], Parsed, Match], bool]:
    def parse(body: Dict[str, Any], parsed: Parsed, m: Match) -> bool:
        if not isinstance(body.get(key), str):
            return False
        parsed.input_value, parsed.input_mime = body[key], "text/plain"
        parsed.extra["gen_ai.audio.transcript"] = body[key]
        return True

    return parse


def _req_any(body: Any, parsed: Parsed, m: Match) -> bool:
    return True


# ---------------------------------------------------------------------------
# Response parsers
# ---------------------------------------------------------------------------


def _openai_usage(parsed: Parsed, usage: Any) -> None:
    if not isinstance(usage, dict):
        return
    _provider_usage(parsed, "openai", usage)
    _usage(
        parsed,
        input=usage.get("prompt_tokens", usage.get("input_tokens")),
        output=usage.get("completion_tokens", usage.get("output_tokens")),
        total=usage.get("total_tokens"),
    )
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    if isinstance(details, dict):
        _usage(parsed, cache_read=details.get("cached_tokens"), input_audio=details.get("audio_tokens"))
    out_details = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
    if isinstance(out_details, dict):
        _usage(parsed, reasoning=out_details.get("reasoning_tokens"), output_audio=out_details.get("audio_tokens"))


def _resp_openai_chat(body: Body, parsed: Parsed, m: Match) -> None:
    if body.kind == "json" and isinstance(body.json, dict):
        data = body.json
        parsed.response_id, parsed.response_model = data.get("id"), data.get("model")
        for choice in data.get("choices") or []:
            message = choice.get("message") or {}
            if message:
                out = _openai_message(message)
                out.role = out.role or "assistant"
                parsed.output_messages.append(out)
            elif isinstance(choice.get("text"), str):
                parsed.output_messages.append(Message("assistant", choice["text"]))
            if choice.get("finish_reason"):
                parsed.finish_reasons.append(str(choice["finish_reason"]))
        _openai_usage(parsed, data.get("usage"))
        return
    if body.kind != "sse":
        return
    texts: Dict[int, List[str]] = {}
    roles: Dict[int, str] = {}
    calls: Dict[int, Dict[int, Dict[str, Any]]] = {}
    finish: Dict[int, str] = {}
    for chunk in body.payloads():
        parsed.response_id = parsed.response_id or chunk.get("id")
        parsed.response_model = parsed.response_model or chunk.get("model")
        if chunk.get("usage"):
            _openai_usage(parsed, chunk["usage"])
        for choice in chunk.get("choices") or []:
            index = choice.get("index", 0)
            delta = choice.get("delta") or {}
            if delta.get("role"):
                roles[index] = delta["role"]
            if isinstance(delta.get("content"), str):
                texts.setdefault(index, []).append(delta["content"])
            if isinstance(choice.get("text"), str):
                texts.setdefault(index, []).append(choice["text"])
            for call in delta.get("tool_calls") or []:
                slot = calls.setdefault(index, {}).setdefault(call.get("index", 0), {"id": None, "name": None, "args": []})
                function = call.get("function") or {}
                slot["id"] = slot["id"] or call.get("id")
                slot["name"] = slot["name"] or function.get("name")
                if isinstance(function.get("arguments"), str):
                    slot["args"].append(function["arguments"])
            if choice.get("finish_reason"):
                finish[index] = str(choice["finish_reason"])
    for index in sorted(set(texts) | set(calls) | set(roles)):
        content = "".join(texts.get(index, [])) or None
        message = Message(roles.get(index, "assistant"), content)
        for _, slot in sorted(calls.get(index, {}).items()):
            message.tool_calls.append(ToolCall(slot["id"], slot["name"], "".join(slot["args"]) or None))
        parsed.output_messages.append(message)
    parsed.finish_reasons = [finish[i] for i in sorted(finish)]


def _responses_output(parsed: Parsed, response: Dict[str, Any]) -> None:
    parsed.response_id = response.get("id") or parsed.response_id
    parsed.response_model = response.get("model") or parsed.response_model
    message = Message("assistant")
    texts = []
    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    texts.append(part["text"])
        elif item.get("type") == "function_call":
            message.tool_calls.append(ToolCall(item.get("call_id"), item.get("name"), _args_str(item.get("arguments"))))
    message.content = "\n".join(texts) if texts else None
    if message.content is not None or message.tool_calls:
        parsed.output_messages = [message]
    if response.get("status"):
        parsed.finish_reasons = [str(response["status"])]
    _openai_usage(parsed, response.get("usage"))


def _resp_openai_responses(body: Body, parsed: Parsed, m: Match) -> None:
    if body.kind == "json" and isinstance(body.json, dict):
        _responses_output(parsed, body.json)
        return
    if body.kind != "sse":
        return
    deltas = []
    calls: Dict[Any, Dict[str, Any]] = {}
    item_indices: Dict[str, Any] = {}
    final = None
    for event in body.payloads():
        kind = event.get("type")
        _openai_usage(parsed, event.get("usage"))
        if isinstance(event.get("response"), dict):
            _openai_usage(parsed, event["response"].get("usage"))
        if kind in ("response.completed", "response.incomplete", "response.failed") and isinstance(event.get("response"), dict):
            final = event["response"]
        elif kind in ("response.created", "response.in_progress") and isinstance(event.get("response"), dict):
            parsed.response_id = event["response"].get("id") or parsed.response_id
            parsed.response_model = event["response"].get("model") or parsed.response_model
        elif kind == "response.output_text.delta" and isinstance(event.get("delta"), str):
            deltas.append(event["delta"])
        elif kind in ("response.output_item.added", "response.output_item.done"):
            item = event.get("item") or {}
            if item.get("type") != "function_call":
                continue
            index = event.get("output_index", item.get("id", 0))
            if item.get("id"):
                item_indices[item["id"]] = index
            slot = calls.setdefault(index, {})
            for key in ("call_id", "name"):
                if item.get(key) is not None:
                    slot[key] = item[key]
            if item.get("arguments") or "arguments" not in slot:
                slot["arguments"] = _args_str(item.get("arguments")) or ""
        elif kind in ("response.function_call_arguments.delta", "response.function_call_arguments.done"):
            index = event.get("output_index", item_indices.get(event.get("item_id"), event.get("item_id", 0)))
            slot = calls.setdefault(index, {})
            if kind.endswith(".done") and isinstance(event.get("arguments"), str):
                slot["arguments"] = event["arguments"]
            elif isinstance(event.get("delta"), str):
                slot["arguments"] = slot.get("arguments", "") + event["delta"]
    if deltas or calls:
        message = Message("assistant", "".join(deltas) or None)
        for slot in calls.values():
            message.tool_calls.append(ToolCall(slot.get("call_id"), slot.get("name"), slot.get("arguments")))
        parsed.output_messages = [message]
    if final is not None:
        # The final response is authoritative; partial deltas remain useful if a
        # failed/incomplete response omitted its output.
        _responses_output(parsed, final)


def _resp_openai_embeddings(body: Body, parsed: Parsed, m: Match) -> None:
    data = body.json if body.kind == "json" and isinstance(body.json, dict) else None
    if data is None:
        return
    parsed.response_model = data.get("model")
    items = data.get("data") or []
    if items and isinstance(items[0], dict) and isinstance(items[0].get("embedding"), list):
        parsed.extra["gen_ai.embeddings.dimension.count"] = len(items[0]["embedding"])
    _openai_usage(parsed, data.get("usage"))


def _set_transcript(parsed: Parsed, text: Optional[str]) -> None:
    if isinstance(text, str):
        parsed.extra["gen_ai.audio.transcript"] = text
        parsed.output_value, parsed.output_mime = text, "text/plain"


def _resp_openai_transcription(body: Body, parsed: Parsed, m: Match) -> None:
    if body.kind == "text":
        _set_transcript(parsed, body.text)
        return
    if body.kind == "sse":
        deltas, done = [], None
        for event in body.payloads():
            if event.get("type") == "transcript.text.delta" and isinstance(event.get("delta"), str):
                deltas.append(event["delta"])
            elif event.get("type") == "transcript.text.done":
                done = event
        data = done or {"text": "".join(deltas)}
    elif body.kind == "json" and isinstance(body.json, dict):
        data = body.json
    else:
        return
    _set_transcript(parsed, data.get("text"))
    _put(parsed.extra, "gen_ai.audio.language", data.get("language") if isinstance(data.get("language"), str) else None)
    if _num(data.get("duration")) is not None:
        parsed.extra["gen_ai.audio.duration_secs"] = data["duration"]
    usage = data.get("usage")
    if isinstance(usage, dict):
        if usage.get("type") == "duration" and _num(usage.get("seconds")) is not None:
            parsed.extra.setdefault("gen_ai.audio.duration_secs", usage["seconds"])
        else:
            _openai_usage(parsed, usage)


def _resp_audio_out(body: Body, parsed: Parsed, m: Match) -> None:
    """TTS formats: audio bytes are counted by the transport; only JSON/SSE variants need work here."""
    if body.kind == "json" and isinstance(body.json, dict):
        encoded = body.json.get("audio_base64")
        if isinstance(encoded, str):
            parsed.extra["traceai.audio.output_bytes"] = _b64_len(encoded)
    elif body.kind == "sse":
        total = 0
        for event in body.payloads():
            if isinstance(event.get("data"), str) and event.get("type", "chunk") == "chunk":
                total += _b64_len(event["data"])
        parsed.extra["traceai.audio.output_bytes"] = total


def _b64_len(value: str) -> int:
    try:
        return len(base64.b64decode(value, validate=False))
    except (ValueError, TypeError):
        return 0


def _resp_openai_images(body: Body, parsed: Parsed, m: Match) -> None:
    if body.kind != "json" or not isinstance(body.json, dict):
        return
    data = json.loads(_dumps(body.json))
    for item in data.get("data") or []:
        if isinstance(item, dict) and "b64_json" in item:
            item["b64_json"] = "<base64 omitted>"
    parsed.output_value, parsed.output_mime = _dumps(data), "application/json"
    _openai_usage(parsed, data.get("usage"))


def _resp_openai_moderations(body: Body, parsed: Parsed, m: Match) -> None:
    if body.kind != "json" or not isinstance(body.json, dict):
        return
    parsed.response_id, parsed.response_model = body.json.get("id"), body.json.get("model")
    results = body.json.get("results") or []
    parsed.extra["traceai.moderation.flagged"] = any(isinstance(r, dict) and r.get("flagged") for r in results)
    parsed.output_value, parsed.output_mime = _dumps(body.json), "application/json"


def _resp_rerank(body: Body, parsed: Parsed, m: Match) -> None:
    if body.kind != "json" or not isinstance(body.json, dict):
        return
    data = body.json
    parsed.response_id, parsed.response_model = data.get("id"), data.get("model")
    results = data.get("results") or data.get("data") or []
    ranked = [{"index": r.get("index"), "relevance_score": r.get("relevance_score")} for r in results if isinstance(r, dict)]
    parsed.output_value, parsed.output_mime = _dumps(ranked), "application/json"
    usage = data.get("usage")
    if isinstance(usage, dict):
        _usage(parsed, total=usage.get("total_tokens"))


def _resp_anthropic(body: Body, parsed: Parsed, m: Match) -> None:
    def usage(raw: Any) -> None:
        if isinstance(raw, dict):
            _provider_usage(parsed, "anthropic", raw)
            _usage(parsed, input=raw.get("input_tokens"), output=raw.get("output_tokens"),
                   cache_read=raw.get("cache_read_input_tokens"), cache_write=raw.get("cache_creation_input_tokens"))

    if body.kind == "json" and isinstance(body.json, dict) and "content" in body.json:
        data = body.json
        parsed.response_id, parsed.response_model = data.get("id"), data.get("model")
        parsed.output_messages = [_anthropic_blocks(str(data.get("role") or "assistant"), data.get("content"))]
        if data.get("stop_reason"):
            parsed.finish_reasons = [str(data["stop_reason"])]
        usage(data.get("usage"))
        return
    if body.kind != "sse":
        return
    blocks: Dict[int, Dict[str, Any]] = {}
    for event in body.payloads():
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message") or {}
            parsed.response_id, parsed.response_model = message.get("id"), message.get("model")
            usage(message.get("usage"))
        elif kind == "content_block_start":
            block = dict(event.get("content_block") or {})
            block["_parts"] = [block.get("text") or ""] if block.get("type") == "text" else []
            blocks[event.get("index", len(blocks))] = block
        elif kind == "content_block_delta":
            block = blocks.setdefault(event.get("index", 0), {"type": "text", "_parts": []})
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                block["_parts"].append(delta.get("text") or "")
            elif delta.get("type") == "input_json_delta":
                block["_parts"].append(delta.get("partial_json") or "")
        elif kind == "message_delta":
            delta = event.get("delta") or {}
            if delta.get("stop_reason"):
                parsed.finish_reasons = [str(delta["stop_reason"])]
            usage(event.get("usage"))
    message = Message("assistant")
    texts = []
    for _, block in sorted(blocks.items()):
        if block.get("type") == "text":
            texts.append("".join(block["_parts"]))
        elif block.get("type") == "tool_use":
            args = "".join(block["_parts"]) or _args_str(block.get("input"))
            message.tool_calls.append(ToolCall(block.get("id"), block.get("name"), args))
    message.content = "\n".join(texts) or None
    if message.content is not None or message.tool_calls:
        parsed.output_messages = [message]


def _gemini_response(parsed: Parsed, items: List[Dict[str, Any]]) -> None:
    message = Message("assistant")
    texts = []
    for data in items:
        parsed.response_model = data.get("modelVersion") or parsed.response_model
        parsed.response_id = data.get("responseId") or parsed.response_id
        candidates = data.get("candidates") or []
        if candidates and isinstance(candidates[0], dict):
            first = candidates[0]
            out = _gemini_message(first.get("content") or {})
            if out.content:
                texts.append(out.content)
            message.tool_calls.extend(out.tool_calls)
            reasons = [str(c["finishReason"]) for c in candidates if isinstance(c, dict) and c.get("finishReason")]
            if reasons:
                parsed.finish_reasons = reasons
        meta = data.get("usageMetadata")
        if isinstance(meta, dict):
            _provider_usage(parsed, "gemini", meta)
            _usage(parsed, input=meta.get("promptTokenCount"), output=meta.get("candidatesTokenCount"),
                   total=meta.get("totalTokenCount"), cache_read=meta.get("cachedContentTokenCount"),
                   reasoning=meta.get("thoughtsTokenCount"))
    message.content = "".join(texts) or None
    if message.content is not None or message.tool_calls:
        parsed.output_messages = [message]


def _resp_gemini(body: Body, parsed: Parsed, m: Match) -> None:
    if body.kind == "json":
        items = body.json if isinstance(body.json, list) else [body.json]
        _gemini_response(parsed, [x for x in items if isinstance(x, dict)])
    elif body.kind == "sse":
        _gemini_response(parsed, body.payloads())


def _resp_gemini_embed(body: Body, parsed: Parsed, m: Match) -> None:
    if body.kind != "json" or not isinstance(body.json, dict):
        return
    data = body.json
    first = data.get("embedding") or (data.get("embeddings") or [None])[0]
    if isinstance(first, dict) and isinstance(first.get("values"), list):
        parsed.extra["gen_ai.embeddings.dimension.count"] = len(first["values"])


def _resp_cohere_chat(body: Body, parsed: Parsed, m: Match) -> None:
    if body.kind in ("sse", "ndjson"):
        texts = []
        calls: Dict[Any, Dict[str, Any]] = {}
        for event_name, event in body.events:
            if not isinstance(event, dict):
                continue
            kind = event.get("type") or event.get("event_type") or event_name
            delta = event.get("delta") or {}
            if kind in ("message-start", "stream-start"):
                parsed.response_id = event.get("id") or event.get("generation_id") or parsed.response_id
            elif kind == "content-delta":
                text = text_of((delta.get("message") or {}).get("content"))
                if text is not None:
                    texts.append(text)
            elif kind == "text-generation" and isinstance(event.get("text"), str):
                texts.append(event["text"])
            elif kind in ("tool-call-start", "tool-call-delta"):
                call = (delta.get("message") or {}).get("tool_calls") or {}
                if isinstance(call, list):
                    call = call[0] if call else {}
                function = call.get("function") or {}
                slot = calls.setdefault(event.get("index", 0), {"arguments": ""})
                slot["id"] = call.get("id") or slot.get("id")
                slot["name"] = function.get("name") or slot.get("name")
                if isinstance(function.get("arguments"), str):
                    slot["arguments"] += function["arguments"]
            elif kind == "tool-calls-generation":
                for index, call in enumerate(event.get("tool_calls") or []):
                    if isinstance(call, dict):
                        calls[index] = {"id": call.get("id"), "name": call.get("name"),
                                        "arguments": _args_str(call.get("parameters")) or ""}
            elif kind == "message-end":
                if delta.get("finish_reason"):
                    parsed.finish_reasons = [str(delta["finish_reason"])]
                usage = delta.get("usage") or {}
                tokens = usage.get("tokens") or usage.get("billed_units") or {}
                _usage(parsed, input=tokens.get("input_tokens"), output=tokens.get("output_tokens"))
            elif kind == "stream-end":
                if event.get("finish_reason"):
                    parsed.finish_reasons = [str(event["finish_reason"])]
                if isinstance(event.get("response"), dict):
                    _resp_cohere_chat(Body("json", json=event["response"]), parsed, m)
        if texts or calls:
            message = Message("assistant", "".join(texts) or None)
            for slot in calls.values():
                message.tool_calls.append(ToolCall(slot.get("id"), slot.get("name"), slot["arguments"] or None))
            parsed.output_messages = [message]
        return
    if body.kind != "json" or not isinstance(body.json, dict):
        return
    data = body.json
    parsed.response_id = data.get("id") or data.get("generation_id") or parsed.response_id
    if isinstance(data.get("message"), dict):
        message = data["message"]
        out = Message(str(message.get("role") or "assistant"), text_of(message.get("content")))
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            out.tool_calls.append(ToolCall(call.get("id"), function.get("name"), _args_str(function.get("arguments"))))
        parsed.output_messages = [out]
    elif isinstance(data.get("text"), str) or data.get("tool_calls"):
        out = Message("assistant", data.get("text"))
        for call in data.get("tool_calls") or []:
            if isinstance(call, dict):
                out.tool_calls.append(ToolCall(call.get("id"), call.get("name"), _args_str(call.get("parameters"))))
        parsed.output_messages = [out]
    if data.get("finish_reason"):
        parsed.finish_reasons = [str(data["finish_reason"])]
    usage = data.get("usage") or data.get("meta") or {}
    tokens = usage.get("tokens") or usage.get("billed_units") or {}
    if isinstance(tokens, dict):
        _usage(parsed, input=tokens.get("input_tokens"), output=tokens.get("output_tokens"))


def _resp_cohere_embed(body: Body, parsed: Parsed, m: Match) -> None:
    if body.kind != "json" or not isinstance(body.json, dict):
        return
    embeddings = body.json.get("embeddings")
    if isinstance(embeddings, dict):
        embeddings = embeddings.get("float")
    if isinstance(embeddings, list) and embeddings and isinstance(embeddings[0], list):
        parsed.extra["gen_ai.embeddings.dimension.count"] = len(embeddings[0])
    meta = body.json.get("meta") or {}
    units = meta.get("billed_units") or {}
    _usage(parsed, input=units.get("input_tokens"))


def _resp_bedrock_converse(body: Body, parsed: Parsed, m: Match) -> None:
    if body.kind != "json" or not isinstance(body.json, dict):
        return
    data = body.json
    message = (data.get("output") or {}).get("message")
    if isinstance(message, dict):
        parsed.output_messages = [_bedrock_message(message)]
    if data.get("stopReason"):
        parsed.finish_reasons = [str(data["stopReason"])]
    usage = data.get("usage") or {}
    _provider_usage(parsed, "bedrock.converse", usage)
    _usage(parsed, input=usage.get("inputTokens"), output=usage.get("outputTokens"), total=usage.get("totalTokens"),
           cache_read=usage.get("cacheReadInputTokens"), cache_write=usage.get("cacheWriteInputTokens"))


def _resp_bedrock_invoke(body: Body, parsed: Parsed, m: Match) -> None:
    if body.kind == "json" and isinstance(body.json, dict) and "content" in body.json:
        _resp_anthropic(body, parsed, m)
    elif body.kind == "json":
        parsed.output_value, parsed.output_mime = _dumps(body.json), "application/json"


def _resp_ollama(body: Body, parsed: Parsed, m: Match) -> None:
    if body.kind == "json" and isinstance(body.json, dict):
        items = [body.json]
    elif body.kind == "ndjson":
        items = body.payloads()
    else:
        return
    texts = []
    calls = []
    for item in items:
        parsed.response_model = item.get("model") or parsed.response_model
        message = item.get("message") or {}
        calls.extend(_openai_message(message).tool_calls)
        if isinstance(message.get("content"), str):
            texts.append(message["content"])
        if isinstance(item.get("response"), str):
            texts.append(item["response"])
        if item.get("done"):
            if item.get("done_reason"):
                parsed.finish_reasons = [str(item["done_reason"])]
            _usage(parsed, input=item.get("prompt_eval_count"), output=item.get("eval_count"))
    text = "".join(texts)
    if text or calls:
        parsed.output_messages = [Message("assistant", text or None, tool_calls=calls)]


def _resp_ollama_embed(body: Body, parsed: Parsed, m: Match) -> None:
    if body.kind != "json" or not isinstance(body.json, dict):
        return
    vectors = body.json.get("embeddings")
    first = vectors[0] if isinstance(vectors, list) and vectors else body.json.get("embedding")
    if isinstance(first, list):
        parsed.extra["gen_ai.embeddings.dimension.count"] = len(first)
    _usage(parsed, input=body.json.get("prompt_eval_count"))


def _resp_deepgram_listen(body: Body, parsed: Parsed, m: Match) -> None:
    if body.kind != "json" or not isinstance(body.json, dict):
        return
    meta = body.json.get("metadata") or {}
    parsed.response_id = meta.get("request_id")
    if _num(meta.get("duration")) is not None:
        parsed.extra["gen_ai.audio.duration_secs"] = meta["duration"]
    channels = (body.json.get("results") or {}).get("channels") or []
    if channels:
        alternatives = channels[0].get("alternatives") or []
        if alternatives:
            _set_transcript(parsed, alternatives[0].get("transcript"))


def _resp_elevenlabs_stt(body: Body, parsed: Parsed, m: Match) -> None:
    if body.kind != "json" or not isinstance(body.json, dict):
        return
    _set_transcript(parsed, body.json.get("text"))
    _put(parsed.extra, "gen_ai.audio.language", body.json.get("language_code"))
    parsed.response_id = body.json.get("transcription_id")


def _resp_assemblyai(body: Body, parsed: Parsed, m: Match) -> None:
    if body.kind != "json" or not isinstance(body.json, dict):
        return
    data = body.json
    if m.endpoint.only_when == "completed" and data.get("status") not in ("completed", "error"):
        parsed.skip = True
        return
    parsed.response_id = data.get("id")
    parsed.response_model = data.get("speech_model") if isinstance(data.get("speech_model"), str) else None
    if data.get("status"):
        parsed.finish_reasons = [str(data["status"])]
    if data.get("status") == "completed":
        _set_transcript(parsed, data.get("text"))
    if _num(data.get("audio_duration")) is not None:
        parsed.extra["gen_ai.audio.duration_secs"] = data["audio_duration"]
    _put(parsed.extra, "gen_ai.audio.language", data.get("language_code"))


def _resp_none(body: Body, parsed: Parsed, m: Match) -> None:
    return None


RequestParser = Callable[[Any, Parsed, Match], bool]
ResponseParser = Callable[[Body, Parsed, Match], None]

# format -> (request parser, response parser, request body kind)
FORMATS: Dict[str, Tuple[RequestParser, ResponseParser, str]] = {
    "openai.chat": (_req_openai_chat, _resp_openai_chat, "json"),
    "openai.completions": (_req_openai_completions, _resp_openai_chat, "json"),
    "openai.responses": (_req_openai_responses, _resp_openai_responses, "json"),
    "openai.embeddings": (_req_openai_embeddings, _resp_openai_embeddings, "json"),
    "openai.audio_transcription": (_req_any, _resp_openai_transcription, "multipart"),
    "openai.audio_speech": (_req_openai_speech, _resp_audio_out, "json"),
    "openai.images": (_req_openai_images, _resp_openai_images, "json"),
    "openai.moderations": (_req_openai_moderations, _resp_openai_moderations, "json"),
    "rerank": (_req_rerank, _resp_rerank, "json"),
    "anthropic.messages": (_req_anthropic, _resp_anthropic, "json"),
    "gemini.generate_content": (_req_gemini, _resp_gemini, "json"),
    "gemini.embed_content": (_req_gemini_embed, _resp_gemini_embed, "json"),
    "cohere.chat": (_req_cohere_chat, _resp_cohere_chat, "json"),
    "cohere.embed": (_req_cohere_embed, _resp_cohere_embed, "json"),
    "bedrock.converse": (_req_bedrock_converse, _resp_bedrock_converse, "json"),
    "bedrock.invoke": (_req_bedrock_invoke, _resp_bedrock_invoke, "json"),
    "ollama.chat": (_req_ollama_chat, _resp_ollama, "json"),
    "ollama.generate": (_req_ollama_generate, _resp_ollama, "json"),
    "ollama.embed": (_req_ollama_embed, _resp_ollama_embed, "json"),
    "deepgram.listen": (_req_any, _resp_deepgram_listen, "any"),
    "deepgram.speak": (_req_text_field("text"), _resp_audio_out, "json"),
    "elevenlabs.tts": (_req_text_field("text"), _resp_audio_out, "json"),
    "elevenlabs.stt": (_req_any, _resp_elevenlabs_stt, "multipart"),
    "assemblyai.transcript": (_req_any, _resp_assemblyai, "any"),
    "cartesia.tts": (_req_text_field("transcript"), _resp_audio_out, "json"),
}

_MULTIPART_MODEL_FIELD = {"openai.audio_transcription": "model", "elevenlabs.stt": "model_id"}
_JSON_MODEL_KEYS = ("model", "model_id", "speech_model", "modelId")


def _error_message(body: Body) -> Optional[str]:
    data = body.json if body.kind == "json" else None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = data[0]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
        if isinstance(error, str):
            return error
        for key in ("message", "detail", "err_msg"):
            if isinstance(data.get(key), str):
                return data[key]
        if isinstance(data.get("detail"), dict) and isinstance(data["detail"].get("message"), str):
            return data["detail"]["message"]
    if body.kind == "text" and body.text:
        return body.text[:500]
    return None


def check_request(m: Match, req: RequestData) -> Optional[Tuple[Parsed, Any]]:
    """Validate the request body for the matched format. Returns None when not a capture."""
    request_parser, _, body_kind = FORMATS[m.endpoint.format]
    parsed = Parsed()
    body: Any = None
    encoding = _header(req.headers, "content-encoding")
    if req.body is not None and encoding not in ("", "identity") and not req.decoded:
        req.body, exceeded = decode_encoded(req.body, encoding, req.max_body_bytes)
        req.decoded = True
        req.truncated = req.truncated or exceeded
        if req.body is None and not exceeded:
            req.undecoded_encoding = encoding
    unavailable = req.truncated or req.unavailable or (req.body is None and req.fields is None and req.method.upper() != "GET")
    if unavailable:
        if resolve_provider(m.host, m.port) is None:
            return None
        req.unavailable = not req.truncated
    elif body_kind == "multipart":
        fields = req.fields
        if fields is None and req.body is not None and "multipart/" in req.content_type:
            fields, req.files = parse_multipart(req.body, _raw_header(req.headers, "content-type"))
        fields = {str(k)[:256]: str(v)[:65536].encode("utf-8")[:65536].decode("utf-8", "ignore")
                  for k, v in list((fields or {}).items())[:128]}
        _privacy.observe_body(fields)
        model_field = _MULTIPART_MODEL_FIELD.get(m.endpoint.format)
        if model_field and not fields.get(model_field):
            return None
        parsed.request_model = fields.get(model_field) if model_field else None
        _put(parsed.extra, "gen_ai.audio.language", fields.get("language") or fields.get("language_code"))
        params = {k: v for k, v in fields.items() if k not in _CONTENT_KEYS and k != model_field}
        if params:
            parsed.params["_json"] = params
        described = dict(fields)
        for part in req.files:
            described[part.name] = {"filename": part.filename, "content_type": part.content_type, "bytes": part.size}
        parsed.input_value, parsed.input_mime = _dumps(described), "application/json"
        if not request_parser(fields, parsed, m):
            return None
        body = fields
    elif req.body is not None and is_binary_content_type(req.content_type):
        if body_kind == "json":
            return None
        body = None
    elif req.body is not None:
        try:
            body = json.loads(req.body)
            valid_json = True
        except (ValueError, UnicodeDecodeError):
            valid_json = False
        if body_kind == "json" and not valid_json:
            return None
        if valid_json:
            _privacy.observe_body(body)
            if body_kind == "json" and not isinstance(body, dict) and m.endpoint.format != "bedrock.invoke":
                return None
            if not request_parser(body, parsed, m):
                return None
            if parsed.input_value is None:
                parsed.input_value, parsed.input_mime = req.body.decode("utf-8", "replace"), "application/json"
            if isinstance(body, dict):
                parsed.streamed = body.get("stream") is True or (
                    m.endpoint.format in ("ollama.chat", "ollama.generate") and body.get("stream") is not False
                )
                for key in _JSON_MODEL_KEYS:
                    if isinstance(body.get(key), str):
                        parsed.request_model = body[key]
                        break
                params = {k: v for k, v in body.items() if k not in _CONTENT_KEYS}
                if params:
                    parsed.params.setdefault("_json", params)
    else:
        # No body: either a GET (AssemblyAI polling) or one too large to buffer, which is still
        # recorded, flagged as truncated.
        if body_kind == "json" and not req.truncated:
            return None
        request_parser({}, parsed, m)
    if parsed.request_model is None:
        parsed.request_model = m.groups.get("model") or (m.query.get("model") or [None])[0]
    if m.endpoint.format == "deepgram.listen":
        _put(parsed.extra, "gen_ai.audio.language", (m.query.get("language") or [None])[0])
    if "voice" in m.groups:
        parsed.extra["gen_ai.request.voice"] = m.groups["voice"]
    for part in req.files[:1]:
        _put(parsed.extra, "gen_ai.audio.mime_type", part.content_type)
        if part.size is not None:
            parsed.extra["traceai.audio.input_bytes"] = part.size
    if req.body is not None and is_binary_content_type(req.content_type):
        parsed.extra["gen_ai.audio.mime_type"] = req.content_type
        parsed.extra["traceai.audio.input_bytes"] = req.body_size if req.body_size is not None else len(req.body)
    return parsed, body


def _raw_header(headers: Dict[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return ""


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and len(value) > 0


def _tool_output(function: Any, arguments: str = "arguments") -> bool:
    """A model-requested tool name or argument payload; IDs and empty scaffolds are not.

    Gemini, Ollama and Cohere v1 can carry decoded argument objects rather than JSON
    strings; only a nonempty object qualifies without a name.
    """
    if not isinstance(function, dict):
        return False
    value = function.get(arguments)
    return _nonempty(function.get("name")) or _nonempty(value) or (isinstance(value, dict) and len(value) > 0)


def _tool_calls_output(calls: Any) -> bool:
    if isinstance(calls, dict):
        calls = [calls]
    return isinstance(calls, list) and any(
        isinstance(call, dict) and _tool_output(call.get("function")) for call in calls)


def _content_output(content: Any) -> bool:
    """String or typed text/thinking parts, with one nested Mistral thinking-text level.

    Only model-content fields are walked, never metadata or tool results: a list, a
    type tag, a citation or an empty thinking block alone is not output.
    """
    if _nonempty(content):
        return True
    parts = [content] if isinstance(content, dict) else content if isinstance(content, list) else []
    for part in parts:
        if not isinstance(part, dict) or part.get("type") not in (None, "text", "thinking"):
            continue
        if _nonempty(part.get("text")) or _nonempty(part.get("thinking")):
            return True
        thinking = part.get("thinking")
        if isinstance(thinking, list) and any(
                isinstance(piece, dict) and piece.get("type") == "text" and _nonempty(piece.get("text"))
                for piece in thinking):
            return True
    return False


_RESPONSES_OUTPUT_DELTAS = frozenset({
    "response.output_text.delta", "response.refusal.delta", "response.reasoning_text.delta",
    "response.reasoning_summary_text.delta", "response.function_call_arguments.delta",
    "response.mcp_call_arguments.delta", "response.code_interpreter_call_code.delta",
    "response.custom_tool_call_input.delta", "response.audio.delta", "response.audio.transcript.delta",
})


def _has_output_delta(event: Any, event_name: Optional[str] = None) -> bool:
    """A10 model output (spec README table), not role/ID/usage scaffolding or playback.

    Mirrors the normative oracle ``first_output`` in spec/auto-instrumentation/generate_fixtures.py.
    """
    if not isinstance(event, dict):
        return False
    nonempty = _nonempty
    # OpenAI Completions text and Chat deltas (every choice, every content part).
    for choice in event.get("choices") or []:
        if nonempty(choice.get("text")):
            return True
        delta = choice.get("delta") or {}
        if _content_output(delta.get("content")) or any(
                nonempty(delta.get(key)) for key in ("refusal", "reasoning_content", "reasoning")):
            return True
        audio = delta.get("audio")
        if isinstance(audio, dict) and any(nonempty(audio.get(key)) for key in ("data", "transcript")):
            return True
        if _tool_calls_output(delta.get("tool_calls")) or _tool_output(delta.get("function_call")):
            return True
    # The SSE ``event:`` header supplies the kind when JSON omits its redundant type.
    kind = event.get("type") or event.get("event_type") or event_name or event.get("event")
    # Responses tool names/input/code can precede their argument deltas.
    if kind == "response.output_item.added":
        item = event.get("item") or {}
        item_type = item.get("type")
        if item_type in ("function_call", "tool_call", "custom_tool_call", "mcp_call") and (
                _tool_output(item) or _tool_output(item.get("function"))):
            return True
        if item_type == "custom_tool_call" and nonempty(item.get("input")):
            return True
        if item_type == "code_interpreter_call" and nonempty(item.get("code")):
            return True
    if kind in _RESPONSES_OUTPUT_DELTAS and nonempty(event.get("delta")):
        return True
    # Anthropic text, thinking, model-requested (client/server/MCP) tool names and partial JSON.
    if kind == "content_block_start":
        block = event.get("content_block") or {}
        if any(nonempty(block.get(key)) for key in ("text", "thinking")) or (
                block.get("type") in ("tool_use", "server_tool_use", "mcp_tool_use") and nonempty(block.get("name"))):
            return True
    if kind == "content_block_delta":
        delta = event.get("delta") or {}
        if any(nonempty(delta.get(key)) for key in ("text", "thinking", "partial_json")):
            return True
    # Gemini text (thought=true included), tool requests, generated code and generated audio.
    for candidate in event.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            if nonempty(part.get("text")) or _tool_output(part.get("functionCall"), "args"):
                return True
            code = part.get("executableCode") or {}
            if isinstance(code, dict) and nonempty(code.get("code")):
                return True
            inline = part.get("inlineData") or part.get("inline_data") or {}
            mime = inline.get("mimeType") or inline.get("mime_type")
            if isinstance(mime, str) and mime.lower().startswith("audio/") and nonempty(inline.get("data")):
                return True
    # Bedrock Converse decoded event payloads (binary framing is separate).
    delta = (event.get("contentBlockDelta") or {}).get("delta") or {}
    if nonempty(delta.get("text")) or _tool_output(delta.get("toolUse"), "input"):
        return True
    if nonempty((delta.get("reasoningContent") or {}).get("text")):
        return True
    start = (event.get("contentBlockStart") or {}).get("start") or {}
    if _tool_output(start.get("toolUse"), "input"):
        return True
    # Cohere v2 content/thinking/tool deltas nest in delta.message (objects or lists).
    if kind in ("content-start", "content-delta", "tool-call-start", "tool-call-delta", "tool-plan-delta"):
        message = (event.get("delta") or {}).get("message") or {}
        if kind in ("content-start", "content-delta"):
            if _content_output(message.get("content")):
                return True
        elif kind == "tool-plan-delta":
            if nonempty(message.get("tool_plan")):
                return True
        elif _tool_calls_output(message.get("tool_calls")):
            return True
    # Cohere v1: the first tool-calls-chunk payload counts; do not wait for generation.
    if kind == "tool-calls-generation":
        if any(_tool_output(call, "parameters") for call in event.get("tool_calls") or []):
            return True
    if kind == "tool-calls-chunk":
        call = event.get("tool_call_delta")
        if nonempty(event.get("text")) or _tool_output(call, "parameters") or (
                isinstance(call, dict) and nonempty(call.get("text"))):
            return True
    if kind == "text-generation" and nonempty(event.get("text")):
        return True
    # Ollama records are untyped; a typed keep-alive/error message is not output.
    if kind is None:
        message = event.get("message")
        if isinstance(message, dict) and (
                any(nonempty(message.get(key)) for key in ("content", "thinking"))
                or _tool_calls_output(message.get("tool_calls"))):
            return True
        if nonempty(event.get("response")) or nonempty(event.get("thinking")):
            return True
    # Cartesia TTS SSE: generated base64 audio in chunk.data (never playback).
    return kind == "chunk" and nonempty(event.get("data"))


def _first_output_seconds(resp: ResponseData, body: Body) -> Optional[float]:
    """Associate complete frames with recorded arrival offsets, never parse time."""
    if not resp.body or not resp.chunk_arrivals or body.kind not in ("sse", "ndjson", "json"):
        return None
    if body.kind == "json":
        frames = _json_object_frames(resp.body)
    else:
        boundary = re.compile(rb"\r?\n\r?\n" if body.kind == "sse" else rb"\r?\n")
        frames = ((match.end(), None) for match in boundary.finditer(resp.body))
    start, arrival_index = 0, 0
    for index, (end, payload) in enumerate(frames):
        if index >= 16384:
            resp.timing_truncated = True
            break
        while arrival_index < len(resp.chunk_arrivals) and resp.chunk_arrivals[arrival_index][0] < end:
            arrival_index += 1
        if arrival_index >= len(resp.chunk_arrivals):
            return None
        if body.kind == "json":
            events = [(None, payload)]
        else:
            frame = resp.body[start:end]
            start = end
            if body.kind == "sse":
                events = parse_sse(frame)
            else:
                events = [(None, event) for event in parse_ndjson(frame)]
        for event_name, event in events:
            try:
                if _has_output_delta(event, event_name):
                    return resp.chunk_arrivals[arrival_index][1]
            except (TypeError, AttributeError):
                # Malformed scaffolding cannot discard the otherwise valid span.
                continue
    return None


def build(m: Match, req: RequestData, resp: ResponseData, parsed: Optional[Parsed] = None) -> Optional[SpanResult]:
    """Build the span for a finished exchange, or None when it must not be captured."""
    if parsed is None:
        checked = check_request(m, req)
        if checked is None:
            return None
        parsed = checked[0]
    endpoint = m.endpoint
    _, response_parser, _ = FORMATS[endpoint.format]
    encoded_arrivals = _header(resp.headers, "content-encoding") not in ("", "identity") and not resp.decoded
    body = decode_response(resp)
    ok = resp.error is None and (resp.status or 0) < 400
    if ok or resp.error is not None:
        try:
            response_parser(body, parsed, m)
        except Exception:  # a malformed body must never break capture
            pass
        if parsed.skip:
            return None
    elif endpoint.only_when == "completed" and resp.error is None:
        return None

    attrs: Attributes = {
        "gen_ai.span.kind": endpoint.span_kind,
        "gen_ai.operation.name": endpoint.operation,
        "gen_ai.provider.name": m.provider,
        "traceai.capture.layer": LAYER,
        "traceai.wire.endpoint": endpoint.id,
        "server.address": m.host,
        "http.request.method": req.method.upper(),
    }
    if m.port is not None:
        attrs["server.port"] = m.port
    if resp.status is not None:
        attrs["http.response.status_code"] = resp.status
    _put(attrs, "gen_ai.request.model", parsed.request_model)
    _put(attrs, "gen_ai.response.model", parsed.response_model)
    _put(attrs, "gen_ai.response.id", parsed.response_id)
    _put(attrs, "gen_ai.response.finish_reasons", parsed.finish_reasons)
    params = dict(parsed.params)
    json_params = params.pop("_json", None)
    for key in ("temperature", "top_p", "max_tokens", "seed", "frequency_penalty", "presence_penalty"):
        if key in params:
            attrs[f"gen_ai.request.{key}"] = params[key]
    if json_params:
        _put(attrs, "gen_ai.request.parameters", json_params)
    lost_arrivals = False
    if (body.streamed or parsed.streamed
            or resp.content_type in ("text/event-stream", "application/x-ndjson", "application/jsonl", "application/ndjson")
            or m.path.endswith((":streamGenerateContent", ":streamRawPredict", "/converse-stream", "/invoke-with-response-stream"))):
        attrs["gen_ai.request.stream"] = True
        if resp.first_chunk_seconds is not None:
            attrs["gen_ai.response.time_to_first_chunk"] = round(resp.first_chunk_seconds, 6)
        first_output = None if encoded_arrivals else _first_output_seconds(resp, body)
        if first_output is not None:
            attrs["gen_ai.response.time_to_first_text"] = round(first_output, 6)
        if resp.timing_truncated:
            attrs["traceai.wire.timing_truncated"] = True
        # A10: output was observed but its arrival association was lost (transport-encoded
        # bytes, or bounded timing evidence exhausted): omit the milestone, mark incomplete.
        lost_arrivals = bool(first_output is None and resp.chunk_arrivals
                             and (encoded_arrivals or resp.timing_truncated)
                             and (parsed.output_messages or parsed.output_value is not None))
    usage = _normalized_usage(parsed)
    _put(attrs, "gen_ai.usage.input_tokens", usage.get("input"))
    _put(attrs, "gen_ai.usage.output_tokens", usage.get("output"))
    total = usage.get("total")
    if total is None and usage.get("input") is not None and usage.get("output") is not None:
        total = usage["input"] + usage["output"]
    _put(attrs, "gen_ai.usage.total_tokens", total)
    _put(attrs, "gen_ai.usage.provider_total_tokens", usage.get("provider_total"))
    _put(attrs, "gen_ai.usage.cache_read_tokens", usage.get("cache_read"))
    _put(attrs, "gen_ai.usage.cache_write_tokens", usage.get("cache_write"))
    _put(attrs, "gen_ai.usage.output_tokens.reasoning", usage.get("reasoning"))
    _put(attrs, "gen_ai.usage.input_tokens.audio", usage.get("input_audio"))
    _put(attrs, "gen_ai.usage.output_tokens.audio", usage.get("output_audio"))
    _put_messages(attrs, "gen_ai.input.messages", parsed.input_messages)
    _put_messages(attrs, "gen_ai.output.messages", parsed.output_messages)
    for i, tool in enumerate(parsed.tools):
        attrs[f"gen_ai.tool.definitions.{i}.tool.json_schema"] = _dumps(tool)
    if parsed.input_value is not None:
        attrs["input.value"] = parsed.input_value
        attrs["input.mime_type"] = parsed.input_mime or "application/json"
    output_value, output_mime = parsed.output_value, parsed.output_mime
    if output_value is None and parsed.output_messages:
        message = parsed.output_messages[0]
        if message.content:
            output_value, output_mime = message.content, "text/plain"
        elif message.tool_calls:
            output_value = _dumps([{"id": c.id, "name": c.name, "arguments": c.arguments} for c in message.tool_calls])
            output_mime = "application/json"
    if output_value is not None:
        attrs["output.value"] = output_value
        attrs["output.mime_type"] = output_mime or "text/plain"
    for key, value in parsed.extra.items():
        _put(attrs, key, value)
    if body.kind == "binary" and ok:
        attrs.setdefault("gen_ai.audio.mime_type", resp.content_type)
        if resp.content_type.startswith("audio/") or endpoint.operation == "text_to_speech":
            attrs["traceai.audio.output_bytes"] = resp.size
    if req.unavailable:
        attrs["traceai.wire.body_unavailable"] = True
    if resp.incomplete or lost_arrivals:
        attrs["traceai.wire.incomplete"] = True
    if resp.undecoded_encoding or req.undecoded_encoding:
        attrs["traceai.wire.undecoded_encoding"] = resp.undecoded_encoding or req.undecoded_encoding
    if req.truncated or resp.truncated:
        attrs["traceai.wire.truncated"] = True

    model = parsed.request_model or parsed.response_model
    name = f"{endpoint.operation} {model}" if model else endpoint.operation
    description = None
    if not ok:
        if resp.error is not None:
            attrs["error.type"] = type(resp.error).__name__
            description = str(resp.error) or type(resp.error).__name__
        else:
            attrs["error.type"] = str(resp.status)
            description = _error_message(body) or f"HTTP {resp.status}"
    credentials = _privacy.secrets(req.headers, url=req.url)
    return SpanResult(name=_privacy.scrub(name, credentials), attributes=_privacy.scrub(attrs, credentials),
                      ok=ok, description=_privacy.scrub(description, credentials))
