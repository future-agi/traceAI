"""Helpers that keep error details (messages, tracebacks, provider error bodies)
out of span data.

Error text is content: provider error bodies quote the prompt back, carry
request and session ids, and Python tracebacks carry local file paths. The
``FiSpan`` wrapper uses these helpers so every instrumentor that goes through
``FITracer`` gets the same treatment.
"""

import json
import re
from typing import Callable, Dict, Iterable, Mapping, NamedTuple, Optional, Tuple, Union

from opentelemetry.util.types import AttributeValue

EXCEPTION_EVENT_NAME = "exception"
EXCEPTION_TYPE = "exception.type"
EXCEPTION_MESSAGE = "exception.message"
EXCEPTION_STACKTRACE = "exception.stacktrace"
EXCEPTION_ESCAPED = "exception.escaped"
ERROR_TYPE = "error.type"
ERROR_MESSAGE = "error.message"

# Attribute keys that carry error text. They are dropped when
# ``TraceConfig.hide_error_details`` is on.
ERROR_DETAIL_KEYS = frozenset({EXCEPTION_MESSAGE, EXCEPTION_STACKTRACE, ERROR_MESSAGE})

# Instrumentor-specific keys follow the same shape, e.g. ``autogen.error.message``
# or ``autogen.tool.error_message``.
_ERROR_DETAIL_SUFFIXES = (
    ".exception.message",
    ".exception.stacktrace",
    ".error.message",
    ".error_message",
)

_TRACEBACK_MARKER = "Traceback (most recent call last)"

# A provider error code is a short identifier such as ``model_not_found``,
# ``ValidationException`` or ``NOT_FOUND``. Anything else (free text, JSON,
# a quoted prompt) is not used as ``error.type``.
_ERROR_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:\-]{0,63}$")

# Larger JSON error bodies are not parsed for a code.
_MAX_JSON_BODY = 16_384


def is_error_detail_key(key: str) -> bool:
    return key in ERROR_DETAIL_KEYS or key.endswith(_ERROR_DETAIL_SUFFIXES)


def strip_error_details(
    attributes: Optional[Mapping[str, AttributeValue]],
) -> Optional[Dict[str, AttributeValue]]:
    if attributes is None:
        return None
    return {k: v for k, v in attributes.items() if not is_error_detail_key(k)}


def qualified_exception_type(exception: BaseException) -> str:
    """Same format as the OpenTelemetry SDK's ``exception.type``."""
    cls = type(exception)
    module = cls.__module__
    qualname = cls.__qualname__
    if module and module != "builtins":
        return f"{module}.{qualname}"
    return qualname


def _as_code(value: object) -> Optional[str]:
    if isinstance(value, str) and _ERROR_CODE_RE.match(value):
        return value
    return None


def _code_from_error_body(body: object) -> Optional[str]:
    if not isinstance(body, Mapping):
        return None
    error = body.get("error")
    if isinstance(error, Mapping):
        for key in ("code", "type"):
            if code := _as_code(error.get(key)):
                return code
    return None


def _stainless_code(exception: BaseException) -> Optional[str]:
    # openai (and litellm, whose errors subclass openai's): `code` and `type`
    # from the error object, e.g. "model_not_found" / "invalid_request_error".
    if code := _as_code(getattr(exception, "code", None)):
        return code
    # groq: body = {"error": {"code": "model_not_found", ...}}; older anthropic:
    # body = {"type": "error", "error": {"type": "not_found_error", ...}}.
    if code := _code_from_error_body(getattr(exception, "body", None)):
        return code
    # openai without a `code`, current anthropic: `type`.
    return _as_code(getattr(exception, "type", None))


def _botocore_code(exception: BaseException) -> Optional[str]:
    # ClientError: response = {"Error": {"Code": "ValidationException", ...}}.
    response = getattr(exception, "response", None)
    if isinstance(response, Mapping):
        error = response.get("Error")
        if isinstance(error, Mapping):
            return _as_code(error.get("Code"))
    return None


def _google_genai_code(exception: BaseException) -> Optional[str]:
    # APIError: status = "NOT_FOUND" (`code` is the HTTP status int).
    return _as_code(getattr(exception, "status", None))


def _google_api_core_code(exception: BaseException) -> Optional[str]:
    # GoogleAPICallError: grpc_status_code = grpc.StatusCode.NOT_FOUND (None
    # when grpc is not installed; the class name is used then).
    return _as_code(getattr(getattr(exception, "grpc_status_code", None), "name", None))


def _mistral_code(exception: BaseException) -> Optional[str]:
    # MistralError / SDKError: body is the raw JSON text, e.g.
    # {"object": "error", "type": "invalid_model", "code": "1500", ...}.
    body = getattr(exception, "body", None)
    if not isinstance(body, str) or len(body) > _MAX_JSON_BODY:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, Mapping):
        return None
    if code := _code_from_error_body(data):
        return code
    for key in ("code", "type"):
        if code := _as_code(data.get(key)):
            return code
    return None


def _httpx_status_code(exception: BaseException) -> Optional[str]:
    # HTTPStatusError: the HTTP status as a string ("404"), as in the HTTP
    # semantic conventions. Other httpx errors have no response.
    status = getattr(getattr(exception, "response", None), "status_code", None)
    if isinstance(status, int) and 400 <= status <= 599:
        return str(status)
    return None


# Exception classes from these packages (or subclasses of them) carry a provider
# error code; every other exception gets its fully qualified class name. A
# generic `.code` / `.status` / `.type` lookup is not used, because unrelated
# libraries carry short codes there too (SQLAlchemy "e3q8", Django "invalid").
# cohere (Fern) errors carry only a status code and a free-text body, so they
# get the class name, e.g. "cohere.errors.not_found_error.NotFoundError".
_PROVIDER_CODE_READERS: Tuple[
    Tuple[Tuple[str, ...], Callable[[BaseException], Optional[str]]], ...
] = (
    (("openai", "anthropic", "groq", "litellm"), _stainless_code),
    (("botocore", "boto3", "aiobotocore"), _botocore_code),
    (("google.genai",), _google_genai_code),
    (("google.api_core",), _google_api_core_code),
    (("mistralai",), _mistral_code),
    (("httpx", "httpx2"), _httpx_status_code),
)


def _in_package(module: str, packages: Tuple[str, ...]) -> bool:
    return any(module == p or module.startswith(p + ".") for p in packages)


def _provider_error_code(exception: BaseException) -> Optional[str]:
    for cls in type(exception).__mro__:
        module = getattr(cls, "__module__", None)
        if not isinstance(module, str):
            continue
        for packages, read_code in _PROVIDER_CODE_READERS:
            if _in_package(module, packages):
                return read_code(exception)
    return None


def get_error_type(exception: BaseException) -> str:
    """Value for ``error.type``: the provider error code when the exception
    comes from a known provider SDK and carries one, otherwise the fully
    qualified exception class."""
    try:
        if code := _provider_error_code(exception):
            return code
    except Exception:
        pass
    return qualified_exception_type(exception)


def _exception_message(exception: BaseException) -> Optional[str]:
    try:
        message = str(exception)
    except Exception:
        return None
    return message or None


class ExceptionSummary(NamedTuple):
    """What the description cleanup needs from an exception: its class names
    and message. Spans keep this instead of the exception, so its traceback
    frames and their locals are not held."""

    type_names: Tuple[str, ...]
    message: Optional[str]


def summarize_exception(exception: BaseException) -> ExceptionSummary:
    names = (qualified_exception_type(exception), type(exception).__name__)
    return ExceptionSummary(tuple(dict.fromkeys(names)), _exception_message(exception))


def _cut_traceback(description: str) -> Optional[str]:
    index = description.find(_TRACEBACK_MARKER)
    if index == -1:
        return description
    return description[:index].rstrip() or None


def _type_prefix_pattern(summaries: Iterable[ExceptionSummary]) -> Optional[str]:
    # "<Type>: " at the start of a line, when the exception's own message
    # follows it up to the end of that line (or of the description).
    alternatives = []
    for summary in summaries:
        names = "|".join(re.escape(name) for name in summary.type_names)
        if summary.message:
            follows = re.escape(summary.message) + r"(?:\n|\Z)"
            alternatives.append(f"(?:{names}): (?={follows})")
        else:
            alternatives.append(rf"(?:{names}): ?(?=\n|\Z)")
    if not alternatives:
        return None
    return "^(?:" + "|".join(alternatives) + ")"


def clean_error_description(
    description: Optional[str],
    exceptions: Iterable[Union[BaseException, ExceptionSummary, None]],
) -> Optional[str]:
    """Reduce an ERROR status description to the exception message(s).

    ``use_span`` and most instrumentors build ``"<Type>: <message>"``, and some
    join one such line per exception (llama-index). The type already lives in
    ``error.type`` and ``exception.type``, so each ``"<Type>: "`` prefix of a
    known exception is removed. A Python traceback never stays in the
    description.
    """
    if not description:
        return description
    summaries = list(
        dict.fromkeys(
            e if isinstance(e, ExceptionSummary) else summarize_exception(e)
            for e in exceptions
            if e is not None
        )
    )
    cleaned = description
    if pattern := _type_prefix_pattern(summaries):
        # One pass, so a message that itself starts with "<Type>: " is kept.
        cleaned = re.sub(pattern, "", description, flags=re.MULTILINE)
        if cleaned != description:
            cleaned = cleaned.strip("\n")
    if _TRACEBACK_MARKER in cleaned:
        if summaries:
            message = summaries[0].message
            return _cut_traceback(message) if message else None
        return _cut_traceback(cleaned)
    return cleaned if cleaned.strip() else None
