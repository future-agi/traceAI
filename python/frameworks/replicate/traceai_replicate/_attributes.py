"""Attribute extraction for Replicate calls.

Pure functions: they read the arguments, the client and the returned objects
and build span attributes. They never perform I/O, never read a file output's
bytes, and never read ``REPLICATE_API_TOKEN`` from the environment.
"""

from __future__ import annotations

import inspect
import json
import re
import types
from collections.abc import Mapping
from typing import Any, Dict, Iterable, List, Optional, Tuple

PROVIDER = "gen_ai.provider.name"
REQUEST_MODEL = "gen_ai.request.model"
REQUEST_PARAMETERS = "gen_ai.request.parameters"
SPAN_KIND = "gen_ai.span.kind"
INPUT_VALUE = "input.value"
INPUT_MIME_TYPE = "input.mime_type"
OUTPUT_VALUE = "output.value"
OUTPUT_MIME_TYPE = "output.mime_type"
PREDICTION_ID = "replicate.prediction.id"
PREDICTION_STATUS = "replicate.prediction.status"
PREDICTION_VERSION = "replicate.prediction.version"
PREDICT_TIME = "replicate.metrics.predict_time"
DEPLOYMENT = "replicate.deployment"
OUTPUT_TYPE = "replicate.output.type"
STREAM_FILE_COUNT = "replicate.stream.file_count"
# Why a stream span has no final status from the opt-in read after ``done``:
# skipped, timeout, failed, unfinished, flushed, shutdown, uninstrumented.
STREAM_READ = "replicate.stream.read"
# Why that read was not made, when ``replicate.stream.read`` is ``skipped``.
STREAM_SKIP_REASON = "replicate.stream.skip_reason"
# The ``reason`` in a stream's ``done`` event data (``canceled``, ``error``).
STREAM_DONE_REASON = "replicate.stream.done_reason"
# True when the event stream ended without a ``done`` event.
STREAM_TRUNCATED = "replicate.stream.truncated"
CANCELLED = "replicate.cancelled"
INPUT_TOKENS = "gen_ai.usage.input_tokens"
OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
TOTAL_TOKENS = "gen_ai.usage.total_tokens"

REPLICATE = "replicate"
LLM = "LLM"
CHAIN = "CHAIN"
JSON = "application/json"
TEXT = "text/plain"
TOKEN_REDACTED = "[redacted]"

# Statuses a prediction does not leave: the three on which Prediction.wait()
# stops polling (replicate/prediction.py), and ``aborted`` (its deadline passed
# before it started; replicate 1.x's Prediction model does not list it, so only
# the read after a stream, which parses the JSON itself, can see it).
TERMINAL_STATUSES = ("succeeded", "failed", "canceled", "aborted")
# Replicate's official models (e.g. meta/meta-llama-3-8b-instruct) report this
# placeholder instead of a version id (live run 2026-10-20); it is not a version.
_PLACEHOLDER_VERSIONS = frozenset({"hidden"})

# Invocation parameters recorded by value, and ones recorded only as present:
# a webhook URL is the customer's endpoint and can carry a secret.
_PARAMETERS = ("wait", "stream", "use_file_output", "file_encoding_strategy", "webhook_events_filter")
_PRESENCE_ONLY = ("webhook", "webhook_completed")
_REF = re.compile(r"^(?P<owner>[^/]+)/(?P<name>[^/:]+)(:(?P<version>.+))?$")
_MAX_DEPTH = 32
_MIN_TOKEN_LENGTH = 8
_SIGNATURES: Dict[Any, Optional[inspect.Signature]] = {}


# -- API token -------------------------------------------------------------------


def replicate_client(instance: Any) -> Any:
    """The ``replicate.Client`` behind a client, a namespace or a prediction."""
    # Client keeps its constructor kwargs; checked first because Client._client
    # is a property that would build an httpx client as a side effect.
    if hasattr(instance, "_client_kwargs"):
        return instance
    client = getattr(instance, "_client", None)
    return client if hasattr(client, "_client_kwargs") else None


def _authorization(headers: Any) -> List[str]:
    if headers is None or not hasattr(headers, "items"):
        return []
    found = []
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == "authorization" and isinstance(value, str):
            scheme, _, credential = value.partition(" ")
            found.append(credential if credential else scheme)
    return found


def api_tokens(client: Any) -> Tuple[str, ...]:
    """Every copy of the API token the SDK has stored on ``client``.

    replicate 1.x keeps the ``api_token`` argument in ``_api_token``, caller
    headers in ``_client_kwargs["headers"]``, and puts ``Authorization: Bearer
    <token>`` on the httpx clients it builds lazily (that is where a token
    read from ``REPLICATE_API_TOKEN`` ends up). The environment is not read.
    """
    if client is None:
        return ()
    found: List[str] = []
    token = getattr(client, "_api_token", None)
    if isinstance(token, str):
        found.append(token)
    client_kwargs = getattr(client, "_client_kwargs", None)
    if isinstance(client_kwargs, Mapping):
        found.extend(_authorization(client_kwargs.get("headers")))
    state = getattr(client, "__dict__", {})
    for name in ("_Client__client", "_Client__async_client"):
        found.extend(_authorization(getattr(state.get(name), "headers", None)))
    return tuple(t for t in dict.fromkeys(found) if len(t) >= _MIN_TOKEN_LENGTH)


def redact(value: Any, tokens: Iterable[str]) -> Any:
    """Replace each token inside a string (or a list of strings)."""
    if isinstance(value, str):
        for token in tokens:
            if token in value:
                value = value.replace(token, TOKEN_REDACTED)
        return value
    if isinstance(value, (list, tuple)):
        return [redact(item, tokens) for item in value]
    return value


# -- request ---------------------------------------------------------------------


def _signature(function: Any) -> Optional[inspect.Signature]:
    key = getattr(function, "__func__", function)
    if key not in _SIGNATURES:
        try:
            _SIGNATURES[key] = inspect.signature(key)
        except (TypeError, ValueError):
            _SIGNATURES[key] = None
    return _SIGNATURES[key]


def _arguments(wrapped: Any, instance: Any, args: tuple, kwargs: Mapping) -> Dict[str, Any]:
    """Bind the call to the installed method's own signature.

    Version differences (1.0.0 takes ``use_file_output`` positionally, later
    releases keyword-only) are handled by the signature, not by guessing.
    """
    signature = _signature(wrapped)
    if signature is None:
        return dict(kwargs)
    try:
        bound = signature.bind(instance, *args, **kwargs)
    except TypeError:
        return dict(kwargs)
    arguments: Dict[str, Any] = {}
    names = list(signature.parameters)
    for name, value in bound.arguments.items():
        kind = signature.parameters[name].kind
        if name == names[0]:
            continue  # self
        if kind is inspect.Parameter.VAR_KEYWORD:
            arguments.update(value)
        elif kind is inspect.Parameter.VAR_POSITIONAL:
            arguments["*args"] = tuple(value)
        else:
            arguments[name] = value
    return arguments


def _model_ref(ref: Any) -> Tuple[Optional[str], Optional[str]]:
    """``(owner/name, version id)`` from a ref string, tuple, Model or Version."""
    if isinstance(ref, str):
        match = _REF.match(ref)
        if match:
            return "{0}/{1}".format(match["owner"], match["name"]), real_version(match["version"])
        return None, None
    if isinstance(ref, tuple) and len(ref) >= 2 and all(isinstance(p, str) for p in ref[:2]):
        version = real_version(ref[2]) if len(ref) > 2 else None
        return "{0}/{1}".format(ref[0], ref[1]), version
    owner, name = getattr(ref, "owner", None), getattr(ref, "name", None)
    if isinstance(owner, str) and isinstance(name, str):
        return "{0}/{1}".format(owner, name), None
    if hasattr(ref, "openapi_schema"):  # replicate.version.Version
        return None, real_version(getattr(ref, "id", None))
    return None, None


def real_version(value: Any) -> Optional[str]:
    """``value`` when it is a version id; None when it is empty, not a string,
    or the placeholder official models report instead of one."""
    if isinstance(value, str) and value and value not in _PLACEHOLDER_VERSIONS:
        return value
    return None


def _version_id(version: Any) -> Optional[str]:
    if isinstance(version, str):
        return real_version(version)
    return real_version(getattr(version, "id", None))


def _deployment_ref(deployment: Any) -> Optional[str]:
    if isinstance(deployment, str):
        return deployment
    model, _ = _model_ref(deployment)
    return model


def _jsonable(value: Any, depth: int = 0) -> Any:
    """A JSON-safe copy of ``value`` that never consumes or reads it.

    File handles, paths, bytes and generators are described, not read; a
    ``data:`` URI keeps only its media type (it is the file's bytes).
    """
    if depth > _MAX_DEPTH:
        return "<...>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return shorten_data_uri(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item, depth + 1) for item in value]
    url = _file_url(value)
    if url is not None:
        return shorten_data_uri(url)
    if isinstance(value, (bytes, bytearray)):
        return "<{0} bytes>".format(len(value))
    return "<{0}>".format(type(value).__name__)


def _dumps(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False)


def request_attributes(
    operation: str, wrapped: Any, instance: Any, args: tuple, kwargs: Mapping
) -> Dict[str, Any]:
    """Request attributes for one call.

    ``operation`` is ``run`` / ``stream`` (Client), ``create`` (Predictions),
    ``models.create``, ``deployments.create``, ``deployment.create``, or
    ``prediction`` for wait/cancel on a Prediction.
    """
    attributes: Dict[str, Any] = {PROVIDER: REPLICATE}
    if operation == "prediction":
        attributes.update(prediction_attributes(instance))
        return attributes
    if operation == "cancel":
        prediction_id = args[0] if args else kwargs.get("id")
        if isinstance(prediction_id, str):
            attributes[PREDICTION_ID] = prediction_id
        return attributes

    arguments = _arguments(wrapped, instance, args, kwargs)
    model = version = deployment = None
    if operation in ("run", "stream"):
        model, version = _model_ref(arguments.pop("ref", None))
    elif operation == "create":
        positional = arguments.pop("*args", ())
        version_arg = arguments.pop("version", None)
        if positional:
            version_arg = positional[0]
            if len(positional) > 1:
                arguments["input"] = positional[1]
        version = _version_id(version_arg)
        model, _ = _model_ref(arguments.pop("model", None))
        deployment = _deployment_ref(arguments.pop("deployment", None))
    elif operation == "models.create":
        model, _ = _model_ref(arguments.pop("model", None))
    elif operation == "deployments.create":
        deployment = _deployment_ref(arguments.pop("deployment", None))
    elif operation == "deployment.create":
        deployment = _deployment_ref(getattr(instance, "_deployment", None))

    if model:
        attributes[REQUEST_MODEL] = model
    if version:
        attributes[PREDICTION_VERSION] = version
    if deployment:
        attributes[DEPLOYMENT] = deployment

    if "input" in arguments and arguments["input"] is not None:
        attributes[INPUT_VALUE] = _dumps(arguments["input"])
        attributes[INPUT_MIME_TYPE] = JSON
    parameters = {name: _jsonable(arguments[name]) for name in _PARAMETERS if name in arguments}
    for name in _PRESENCE_ONLY:
        if arguments.get(name) is not None:
            parameters[name] = True
    if parameters:
        attributes[REQUEST_PARAMETERS] = json.dumps(parameters, sort_keys=True)
    return attributes


# -- prediction ------------------------------------------------------------------


def _token_count(metrics: Mapping, name: str) -> Optional[int]:
    value = metrics.get(name)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def prediction_attributes(prediction: Any) -> Dict[str, Any]:
    """Identity, status, timing and reported token usage of a prediction, as the client returned it."""
    attributes: Dict[str, Any] = {}
    for key, name in ((PREDICTION_ID, "id"), (PREDICTION_STATUS, "status")):
        value = getattr(prediction, name, None)
        if isinstance(value, str) and value:
            attributes[key] = value
    version = real_version(getattr(prediction, "version", None))
    if version:
        attributes[PREDICTION_VERSION] = version
    model = getattr(prediction, "model", None)
    if isinstance(model, str) and _REF.match(model):
        attributes[REQUEST_MODEL] = model
    metrics = getattr(prediction, "metrics", None)
    if isinstance(metrics, Mapping):
        predict_time = metrics.get("predict_time")
        if isinstance(predict_time, (int, float)) and not isinstance(predict_time, bool):
            attributes[PREDICT_TIME] = float(predict_time)
        # Language models report their token counts here; nothing is estimated.
        input_tokens = _token_count(metrics, "input_token_count")
        output_tokens = _token_count(metrics, "output_token_count")
        if input_tokens is not None:
            attributes[INPUT_TOKENS] = input_tokens
        if output_tokens is not None:
            attributes[OUTPUT_TOKENS] = output_tokens
        if input_tokens is not None and output_tokens is not None:
            attributes[TOTAL_TOKENS] = input_tokens + output_tokens
    return attributes


# -- output ----------------------------------------------------------------------


def _file_url(value: Any) -> Optional[str]:
    """The URL of a replicate ``FileOutput`` (read from ``.url``, never fetched)."""
    if type(value).__name__ == "FileOutput":
        url = getattr(value, "url", None)
        if isinstance(url, str):
            return url
    return None


def is_url(value: str) -> bool:
    return value.startswith(("https://", "http://", "data:"))


def shorten_data_uri(value: str) -> str:
    """Keep a ``data:`` URI's media type and drop its payload."""
    if value.startswith("data:"):
        header, comma, payload = value.partition(",")
        if comma:
            return "{0},<{1} characters omitted>".format(header, len(payload))
    return value


def _output_item(item: Any) -> Tuple[str, Any]:
    url = _file_url(item)
    if url is not None:
        return "url", shorten_data_uri(url)
    if isinstance(item, str):
        return ("url", shorten_data_uri(item)) if is_url(item) else ("text", item)
    return "other", _jsonable(item)


def _is_text(items: List[Tuple[str, Any]]) -> bool:
    """Text output: text and URL-like chunks only, at least one of them text.

    A language model can emit a chunk that starts with ``https://``,
    ``http://`` or ``data:`` (and replicate 1.0.7 turns such a chunk into a
    ``FileOutput``): it is part of the text. Output is files only when every
    chunk is a URL or a file.
    """
    kinds = {kind for kind, _ in items}
    return "text" in kinds and kinds <= {"text", "url"}


def output_attributes(output: Any) -> Dict[str, Any]:
    """Output type and value. Text is LLM output; URLs are kept as strings."""
    if output is None:
        return {}
    if isinstance(output, (list, tuple)):
        items = [_output_item(item) for item in output]
        if _is_text(items):
            return _output("text", "".join(value for _, value in items), TEXT)
        return _output("list", json.dumps([value for _, value in items], ensure_ascii=False), JSON)
    if isinstance(output, Mapping):
        return _output("object", _dumps(output), JSON)
    kind, value = _output_item(output)
    if kind in ("text", "url"):
        return _output(kind, value, TEXT)
    return _output("other", json.dumps(value, ensure_ascii=False), JSON)


def _output(kind: str, value: str, mime_type: str) -> Dict[str, Any]:
    return {OUTPUT_TYPE: kind, OUTPUT_VALUE: value, OUTPUT_MIME_TYPE: mime_type}


def span_kind(output: Dict[str, Any]) -> str:
    """LLM only when the output is text (PRD G2); CHAIN otherwise."""
    return LLM if output.get(OUTPUT_TYPE) == "text" else CHAIN


class OutputCollector:
    """Collects what a stream or an iterator yields, for one span.

    Every text or URL chunk is kept, in order. When any of them is text, the
    output is text: all of them joined in order, a URL-like chunk (or a
    ``FileOutput``, as its URL) included, since a language model can emit a
    chunk that starts with ``https://`` or ``data:``. Only when every chunk is
    a URL or a file is the output files: with ``count_files`` (SSE streams,
    PRD J4.3) they are counted, not inlined; otherwise their URLs are listed,
    as for a non-iterator ``run``.
    """

    __slots__ = ("_items", "_count_files")

    def __init__(self, count_files: bool) -> None:
        self._items: List[Tuple[str, Any]] = []
        self._count_files = count_files

    def add(self, item: Any) -> None:
        event = getattr(item, "event", None)
        if event is not None and hasattr(item, "data"):  # ServerSentEvent
            if getattr(event, "value", event) != "output":
                return
            item = item.data
        kind, value = _output_item(item)
        if kind in ("text", "url"):
            self._items.append((kind, value))

    def attributes(self) -> Dict[str, Any]:
        items = self._items
        if not items:
            return {}
        if _is_text(items):
            return _output("text", "".join(value for _, value in items), TEXT)
        attributes: Dict[str, Any] = {OUTPUT_TYPE: "url" if len(items) == 1 else "list"}
        if self._count_files:
            attributes[STREAM_FILE_COUNT] = len(items)
        else:
            urls = [value for _, value in items]
            attributes[OUTPUT_VALUE] = urls[0] if len(urls) == 1 else json.dumps(urls)
            attributes[OUTPUT_MIME_TYPE] = TEXT if len(urls) == 1 else JSON
        return attributes


def is_sync_iterator(value: Any) -> bool:
    return isinstance(value, types.GeneratorType)


def is_async_iterator(value: Any) -> bool:
    return isinstance(value, types.AsyncGeneratorType)
