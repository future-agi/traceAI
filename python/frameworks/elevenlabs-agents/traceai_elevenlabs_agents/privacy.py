"""What may leave the forwarder.

Two layers, in this order:

1. **Never exported, in any mode.** Things that describe the machine or the person
   behind it rather than the conversation: resource attributes outside a short
   allowlist (no ``host.*``, ``process.*``, ``os.*`` ...), span attributes under
   environment or identity prefixes (``host.``, ``process.``, ``user.``, ``enduser.``,
   ``code.``, ``*.stacktrace``, credential-named keys), signed media URLs, and raw stack
   traces. Free-form metadata (dynamic variables, analysis and data-collection results,
   unknown attributes, status and exception messages) is kept only after email
   addresses, local file paths and private host names are replaced with
   ``<EMAIL_ADDRESS>``, ``<FILE_PATH>`` and ``<HOSTNAME>``. Error text is cut to one
   line. A ``user.id`` that is an email address is not written.

2. **Conversation content: the standard traceAI switch.** Exactly what every traceAI
   instrumentor uses, ``fi_instrumentation.TraceConfig``, read from the standard
   ``FI_HIDE_*`` environment variables unless you pass one. Content is captured by
   default. In an ElevenLabs conversation the turns are the messages, so:

   * ``hide_inputs`` / ``hide_input_messages`` / ``hide_input_text``
     (``FI_HIDE_INPUTS``, ``FI_HIDE_INPUT_MESSAGES``, ``FI_HIDE_INPUT_TEXT``) drop what
     goes in: user turn text, tool arguments, RAG queries, dynamic variables, ``user.id``
     and every ``input.value``;
   * ``hide_outputs`` / ``hide_output_messages`` / ``hide_output_text``
     (``FI_HIDE_OUTPUTS`` ...) drop what comes out: agent turn text, tool results,
     reasoning, analysis and data-collection results, status and exception messages
     and every ``output.value``;
   * either side drops the combined transcript and every attribute this module cannot
     place on one side.

   ``FI_PII_REDACTION`` (``TraceConfig(pii_redaction=True)``) also works as everywhere
   else in traceAI: emails, phone numbers, card numbers ... inside the content are
   replaced before export.

Identifiers, timings, counts, status codes, model names and token numbers are kept
in every mode.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from fi_instrumentation import TraceConfig

INPUT_FLAGS = ("hide_inputs", "hide_input_messages", "hide_input_text")
OUTPUT_FLAGS = ("hide_outputs", "hide_output_messages", "hide_output_text")

# Attribute classes.
SAFE = "safe"  # identifiers and numbers: always kept
INPUT = "input"  # kept unless an input-side hide flag is on
OUTPUT = "output"  # kept unless an output-side hide flag is on
CONTENT = "content"  # cannot be placed on one side: kept only when nothing is hidden
NEVER = "never"  # never exported

#: Resource attributes that may be exported. Everything else on the payload's resource
#: (``host.name``, ``process.executable.path``, ``os.*`` ...) is dropped in every mode.
RESOURCE_ALLOWLIST = frozenset(
    {
        "service.name",
        "service.version",
        "service.namespace",
        "deployment.environment",
        "deployment.environment.name",
        "telemetry.sdk.name",
        "telemetry.sdk.language",
        "telemetry.sdk.version",
        "elevenlabs.conversation_id",
        "elevenlabs.agent_id",
        "elevenlabs.environment",
        "elevenlabs.source",
    }
)

_NEVER_PREFIXES = (
    "host.",
    "process.",
    "os.",
    "device.",
    "container.",
    "k8s.",
    "cloud.",
    "enduser.",
    "user.",
    "code.",
    "net.host.",
    "client.",
    "source.address",
    "exception.stacktrace",
)
_NEVER_KEY = re.compile(
    r"(stack_?trace|secret|passw(or)?d|api[_\-]?key|authorization|cookie|access[_\-]?token|auth[_\-]?token|bearer)",
    re.IGNORECASE,
)

# Conversation text: kept as said, governed only by the hide flags (and FI_PII_REDACTION).
_INPUT_TEXT = ("elevenlabs.user.text", "elevenlabs.tool.params", "elevenlabs.rag.retrieval_query")
_OUTPUT_TEXT = ("elevenlabs.agent.text", "elevenlabs.tool.result", "elevenlabs.reasoning")
# Free-form metadata on one side: scrubbed of identity, then governed by the hide flags.
_INPUT_META = ("elevenlabs.dynamic_variable", "elevenlabs.user")
_OUTPUT_META = ("elevenlabs.analysis", "elevenlabs.data_collection")
# Numbers under these prefixes can still be content (a collected age, an account
# number), so they follow their side instead of being treated as plain metadata.
_CONTENT_PREFIXES = _INPUT_TEXT + _OUTPUT_TEXT + _INPUT_META + _OUTPUT_META + ("elevenlabs.event.data",)

ATTR_LLM_USAGE_PREFIX = "elevenlabs.llm_usage."
ATTR_TIME_IN_CALL = "time_in_call_secs"
_SAFE_STRING_KEYS = frozenset(
    {
        "service.name",
        "elevenlabs.conversation_id",
        "elevenlabs.agent_id",
        "elevenlabs.source",
        "elevenlabs.environment",
        "elevenlabs.cost_fiat",
        "elevenlabs.producing_llm",
        "elevenlabs.rag.embedding_model",
    }
)
#: Never passed through the TraceConfig mask: ids and kinds the Voice tab groups on.
STRUCTURAL_KEYS = frozenset(
    {
        "session.id",
        "fi.span.kind",
        "gen_ai.system",
        "gen_ai.request.model",
        "tool.name",
        "elevenlabs.conversation_id",
        "elevenlabs.agent_id",
    }
)

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_WINDOWS_PATH = re.compile(r"\b[A-Za-z]:\\(?:[^\\\s\"'<>|]+\\)*[^\\\s\"'<>|]*")
_HOME_PATH = re.compile(r"(?<![\w/])~/[^\s\"'<>|]*")
_FILE_URL = re.compile(r"\bfile://[^\s\"'<>|]+", re.IGNORECASE)
# An absolute POSIX path with at least two segments, not part of a URL or a word.
_POSIX_PATH = re.compile(r"(?<![\w.:/\-])(?:/[\w.@~+\-]+){2,}/?")
_PRIVATE_HOST = re.compile(
    r"\b[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9\-]+)*"
    r"\.(?:local|lan|internal|localdomain|home|corp|intranet)\b",
    re.IGNORECASE,
)
_PY_TRACEBACK = re.compile(r"Traceback \(most recent call last\)")
_FRAME = re.compile(r"(^|\n)\s*(File \"[^\"]+\", line \d+|at [\w$.<>]+\([^)]*:\d+\))")
_SIGNED_URL = re.compile(r"^https?://\S+\?\S*$", re.IGNORECASE)
MAX_ERROR_TEXT = 512


@dataclass(frozen=True)
class Capture:
    """The two content sides, resolved once from a ``TraceConfig``."""

    input: bool
    output: bool
    config: TraceConfig

    @classmethod
    def from_trace_config(cls, config: Optional[TraceConfig] = None) -> "Capture":
        config = TraceConfig() if config is None else config
        if not isinstance(config, TraceConfig):
            raise TypeError("trace_config must be a fi_instrumentation.TraceConfig")
        return cls(
            input=not any(bool(getattr(config, flag, False)) for flag in INPUT_FLAGS),
            output=not any(bool(getattr(config, flag, False)) for flag in OUTPUT_FLAGS),
            config=config,
        )

    @property
    def everything(self) -> bool:
        return self.input and self.output

    def allows(self, cls_: str) -> bool:
        if cls_ == SAFE:
            return True
        if cls_ == INPUT:
            return self.input
        if cls_ == OUTPUT:
            return self.output
        if cls_ == CONTENT:
            return self.everything
        return False

    def mask(self, key: str, value: Any) -> Any:
        """``TraceConfig.mask``, as ``FITracer`` applies it to every attribute it sets."""
        if key in STRUCTURAL_KEYS or isinstance(value, (bool, int, float)):
            return value
        return self.config.mask(key, value)


# --------------------------------------------------------------------------- scrubbing


def looks_like_stack_trace(text: str) -> bool:
    """A Python traceback header, or a multi-line text with a ``File "...", line N`` /
    ``at pkg.Class(File.java:N)`` frame in it."""
    if _PY_TRACEBACK.search(text):
        return True
    return "\n" in text and bool(_FRAME.search(text))


def scrub_identity(text: str) -> str:
    """Replace email addresses, local file paths and private host names."""
    if not text:
        return text
    text = _EMAIL.sub("<EMAIL_ADDRESS>", text)
    text = _FILE_URL.sub("<FILE_PATH>", text)
    text = _WINDOWS_PATH.sub("<FILE_PATH>", text)
    text = _HOME_PATH.sub("<FILE_PATH>", text)
    text = _POSIX_PATH.sub("<FILE_PATH>", text)
    text = _PRIVATE_HOST.sub("<HOSTNAME>", text)
    return text


def error_summary(message: Any) -> Optional[str]:
    """One line of error text: the exception line of a traceback, else the first line.
    Never the frames, never a path, a host name or an email address."""
    if not isinstance(message, str):
        return None
    lines = [line.strip() for line in message.strip().splitlines() if line.strip()]
    if not lines:
        return None
    line = lines[-1] if _PY_TRACEBACK.search(lines[0]) else lines[0]
    line = scrub_identity(line)
    if len(line) > MAX_ERROR_TEXT:
        line = line[: MAX_ERROR_TEXT - 1] + "…"
    return line or None


def conversation_text(text: Any) -> Any:
    """Conversation content is exported as said, except a raw stack trace inside it."""
    if isinstance(text, str) and looks_like_stack_trace(text):
        return error_summary(text)
    return text


def contains_signed_url(value: Any) -> bool:
    if isinstance(value, str):
        return bool(_SIGNED_URL.match(value.strip()))
    if isinstance(value, list):
        return any(contains_signed_url(v) for v in value)
    if isinstance(value, dict):
        return any(contains_signed_url(v) for v in value.values())
    return False


# --------------------------------------------------------------------------- classification


def is_never(key: str) -> bool:
    return key.startswith(_NEVER_PREFIXES) or bool(_NEVER_KEY.search(key))


def is_conversation_text(key: str) -> bool:
    return key.startswith(_INPUT_TEXT + _OUTPUT_TEXT)


def classify(key: str, value: Any, *, parse_llm_usage: Any) -> str:
    """Return the class of one payload span attribute (see the module docstring)."""
    if is_never(key):
        return NEVER
    if key in _SAFE_STRING_KEYS:
        return SAFE if isinstance(value, (str, int, float, bool)) else CONTENT
    if key.startswith(ATTR_LLM_USAGE_PREFIX):
        return SAFE if parse_llm_usage(value) is not None else CONTENT
    if key == ATTR_TIME_IN_CALL:
        return SAFE if isinstance(value, (int, float)) and not isinstance(value, bool) else CONTENT
    if key.startswith(_INPUT_TEXT + _INPUT_META):
        return INPUT
    if key.startswith(_OUTPUT_TEXT + _OUTPUT_META):
        return OUTPUT
    if (
        key.startswith("elevenlabs.")
        and not key.startswith(_CONTENT_PREFIXES)
        and isinstance(value, (int, float, bool))
    ):
        return SAFE
    return CONTENT


def scrub_any_value(value: Mapping[str, Any], *, text: bool) -> Optional[Dict[str, Any]]:
    """Copy an OTLP ``AnyValue``, cleaning every string leaf.

    ``text=True``: conversation content, only stack traces are cut. ``text=False``:
    free-form metadata, identity is scrubbed too. Returns ``None`` for an empty result.
    """
    if not isinstance(value, Mapping):
        return None
    out = copy.deepcopy(dict(value))
    if "stringValue" in out:
        s = out["stringValue"]
        if isinstance(s, str):
            cleaned = conversation_text(s) if text else scrub_identity(s)
            if looks_like_stack_trace(cleaned or ""):
                cleaned = error_summary(cleaned)
            out["stringValue"] = cleaned if cleaned is not None else ""
        return out
    if "arrayValue" in out:
        values = [scrub_any_value(v, text=text) for v in (out["arrayValue"] or {}).get("values", [])]
        out["arrayValue"] = {"values": [v for v in values if v is not None]}
        return out
    if "kvlistValue" in out:
        kept = []
        for kv in (out["kvlistValue"] or {}).get("values", []):
            if not isinstance(kv, Mapping) or is_never(str(kv.get("key", ""))):
                continue
            inner = scrub_any_value(kv.get("value", {}), text=text)
            if inner is not None:
                kept.append({"key": kv.get("key", ""), "value": inner})
        out["kvlistValue"] = {"values": kept}
        return out
    return out
