"""Trace the IBM ``ibm-watsonx-ai`` Python SDK (``ModelInference`` and ``Embeddings``).

Spans are created at the public SDK methods. The SDK's own methods call each other
(``generate_text`` -> ``generate``, ``embed_query`` -> ``generate``); only the outermost call is
traced, so one user call is one span. Do not also instrument HTTPX for the same requests.
"""
from __future__ import annotations

import contextvars
import json
from importlib import import_module
from typing import Any, Collection, Optional

from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes as A
from opentelemetry import trace
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.trace import Status, StatusCode
from wrapt import wrap_function_wrapper

from .version import __version__

PROVIDER = "ibm.watsonx.ai"
_LIMIT = 32768
_INFERENCE = "ibm_watsonx_ai.foundation_models.inference.model_inference"
_EMBEDDINGS = "ibm_watsonx_ai.foundation_models.embeddings.embeddings"

# (module, class, method, operation, is_async)
_METHODS = (
    (_INFERENCE, "ModelInference", "generate", "text_completion", False),
    (_INFERENCE, "ModelInference", "generate_text", "text_completion", False),
    (_INFERENCE, "ModelInference", "generate_text_stream", "text_completion", False),
    (_INFERENCE, "ModelInference", "agenerate", "text_completion", True),
    (_INFERENCE, "ModelInference", "agenerate_stream", "text_completion", True),
    (_INFERENCE, "ModelInference", "chat", "chat", False),
    (_INFERENCE, "ModelInference", "chat_stream", "chat", False),
    (_INFERENCE, "ModelInference", "achat", "chat", True),
    (_INFERENCE, "ModelInference", "achat_stream", "chat", True),
    (_EMBEDDINGS, "Embeddings", "generate", "embeddings", False),
    (_EMBEDDINGS, "Embeddings", "embed_documents", "embeddings", False),
    (_EMBEDDINGS, "Embeddings", "embed_query", "embeddings", False),
    (_EMBEDDINGS, "Embeddings", "agenerate", "embeddings", True),
    (_EMBEDDINGS, "Embeddings", "aembed_documents", "embeddings", True),
    (_EMBEDDINGS, "Embeddings", "aembed_query", "embeddings", True),
)

# The accumulator of the traced SDK call that is running, or None. The SDK's internal calls to
# other public methods do not create nested duplicate spans; their results feed this accumulator,
# so ``generate_text`` and ``embed_query`` still record usage and finish reason from ``generate``.
_INSIDE: contextvars.ContextVar[Optional["_Output"]] = contextvars.ContextVar(
    "traceai_watsonx_inside", default=None)

_REQUEST_ATTRIBUTES = (
    (A.GEN_AI_REQUEST_MAX_TOKENS, ("max_new_tokens", "max_tokens", "max_completion_tokens"), int),
    (A.GEN_AI_REQUEST_TEMPERATURE, ("temperature",), float),
    (A.GEN_AI_REQUEST_TOP_P, ("top_p",), float),
    (A.GEN_AI_REQUEST_TOP_K, ("top_k",), int),
    (A.GEN_AI_REQUEST_FREQUENCY_PENALTY, ("frequency_penalty",), float),
    (A.GEN_AI_REQUEST_PRESENCE_PENALTY, ("presence_penalty",), float),
    (A.GEN_AI_REQUEST_SEED, ("random_seed", "seed"), int),
)


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _number(value: Any, kind: type = int) -> Optional[Any]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if kind is int:
        return int(value) if float(value).is_integer() and value >= 0 else None
    return float(value)


def _argument(args: tuple, kwargs: dict, index: int, name: str) -> Any:
    if name in kwargs:
        return kwargs[name]
    return args[index] if len(args) > index else None


def _params(value: Any) -> dict:
    """Request parameters as a plain dict, from a dict or an SDK parameter object."""
    if hasattr(value, "to_dict"):
        try:
            value = value.to_dict()
        except Exception:
            return {}
    return dict(value) if isinstance(value, dict) else {}


def _json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def _text_content(content: Any) -> Optional[str]:
    """Plain text of a message; image and other non-text parts are never captured."""
    if isinstance(content, str):
        return content[:_LIMIT]
    if isinstance(content, (list, tuple)):
        parts = [p.get("text") for p in content if isinstance(p, dict) and p.get("type") == "text"]
        parts = [p for p in parts if isinstance(p, str)]
        return "".join(parts)[:_LIMIT] if parts else None
    return None


def _is_stream(result: Any) -> bool:
    return hasattr(result, "__anext__") or (hasattr(result, "__next__") and hasattr(result, "__iter__"))


def _capture_input(config: TraceConfig) -> bool:
    return not (config.hide_inputs or config.hide_input_text or config.hide_input_messages or config.hide_input_images)


def _capture_output(config: TraceConfig) -> bool:
    return not (config.hide_outputs or config.hide_output_text or config.hide_output_messages)


def _failure(span: Any, exc: BaseException) -> None:
    # Exception text may carry the request body, URL or credentials: record only the type.
    span.set_attribute("error.type", type(exc).__name__)
    span.set_status(Status(StatusCode.ERROR))
    span.end()


class _Output:
    """Accumulates response (or stream chunk) content, usage and finish reasons."""

    def __init__(self) -> None:
        self.text: dict[int, list[str]] = {}
        self.role: dict[int, str] = {}
        self.finish: dict[int, str] = {}
        self.model: Optional[str] = None
        self.id: Optional[str] = None
        self.tokens_in: Optional[int] = None
        self.tokens_out: Optional[int] = None
        self.tokens_total: Optional[int] = None
        self.vectors = 0
        self.dimension: Optional[int] = None
        self.size = 0
        self.absorbed = False  # an inner SDK call already supplied the response

    def _add(self, index: int, piece: Any) -> None:
        if isinstance(piece, str) and piece and self.size < _LIMIT:
            piece = piece[: _LIMIT - self.size]
            self.text.setdefault(index, []).append(piece)
            self.size += len(piece)

    def _tokens(self, attr: str, value: Any, total: bool) -> None:
        number = _number(value)
        if number is None:
            return
        current = getattr(self, attr)
        # Whole responses add up; stream chunks carry running counts, so keep the largest.
        setattr(self, attr, number if current is None else (current + number if total else max(current, number)))

    def feed(self, chunk: Any, offset: int = 0, total: bool = False) -> None:
        if isinstance(chunk, str):
            self._add(offset, chunk)
        elif isinstance(chunk, (list, tuple)) and chunk and all(_number(x, float) is not None for x in chunk):
            self.dimension = len(chunk)  # embed_query: one vector
        elif isinstance(chunk, (list, tuple)) and chunk and isinstance(chunk[0], (list, tuple)):
            first = chunk[0]  # embed_documents: a list of vectors
            if first and all(_number(x, float) is not None for x in first):
                self.dimension = len(first)
        elif isinstance(chunk, (list, tuple)):  # generate() over a list of prompts
            for position, item in enumerate(chunk):
                self.feed(item, offset=offset + position, total=True)
        elif isinstance(chunk, dict):
            model = chunk.get("model_id") or chunk.get("model")
            if isinstance(model, str):
                self.model = model
            if isinstance(chunk.get("id"), str):
                self.id = chunk["id"]
            for position, choice in enumerate(chunk.get("choices") or []):
                index = _number(_field(choice, "index")) or position
                message = _field(choice, "delta") or _field(choice, "message") or {}
                self._add(index, _text_content(_field(message, "content")))
                if isinstance(_field(message, "role"), str):
                    self.role[index] = message["role"]
                if isinstance(_field(choice, "finish_reason"), str):
                    self.finish[index] = choice["finish_reason"]
            for position, result in enumerate(chunk.get("results") or []):
                if not isinstance(result, dict):
                    continue
                index = offset + position
                self._add(index, result.get("generated_text"))
                reason = result.get("stop_reason")
                if isinstance(reason, str) and reason != "not_finished":
                    self.finish[index] = reason
                self._tokens("tokens_in", result.get("input_token_count"), total)
                self._tokens("tokens_out", result.get("generated_token_count"), total)
                embedding = result.get("embedding")
                if isinstance(embedding, (list, tuple)):
                    self.vectors += 1
                    self.dimension = len(embedding)
            usage = chunk.get("usage")
            if isinstance(usage, dict):
                self._tokens("tokens_in", usage.get("prompt_tokens"), False)
                self._tokens("tokens_out", usage.get("completion_tokens"), False)
                self._tokens("tokens_total", usage.get("total_tokens"), False)
            if "input_token_count" in chunk:  # embeddings response
                self._tokens("tokens_in", chunk.get("input_token_count"), total)

    def absorb(self, result: Any) -> None:
        """Take the result of an inner SDK call made by the traced call (never a stream)."""
        if _is_stream(result):
            return
        try:
            self.feed(result, total=True)
            self.absorbed = True
        except Exception:
            pass

    def apply(self, span: Any, operation: str, complete: bool = True, config: Optional[TraceConfig] = None) -> None:
        if self.model:
            span.set_attribute(A.GEN_AI_RESPONSE_MODEL, self.model)
        if self.id:
            span.set_attribute(A.GEN_AI_RESPONSE_ID, self.id)
        if self.tokens_in is not None:
            span.set_attribute(A.GEN_AI_USAGE_INPUT_TOKENS, self.tokens_in)
        if self.tokens_out is not None:
            span.set_attribute(A.GEN_AI_USAGE_OUTPUT_TOKENS, self.tokens_out)
        total = self.tokens_total
        if total is None and self.tokens_in is not None and self.tokens_out is not None:
            total = self.tokens_in + self.tokens_out
        if total is not None:
            span.set_attribute(A.GEN_AI_USAGE_TOTAL_TOKENS, total)
        if self.dimension is not None:
            span.set_attribute(A.GEN_AI_EMBEDDINGS_DIMENSION_COUNT, self.dimension)
        if self.finish:
            span.set_attribute(A.GEN_AI_RESPONSE_FINISH_REASONS, [self.finish[i] for i in sorted(self.finish)])
        if operation == "embeddings" or not complete:
            return  # vectors are never exported; abandoned streams export no partial output
        for position, index in enumerate(sorted(self.text)):
            prefix = f"{A.GEN_AI_OUTPUT_MESSAGES}.{position}.message"
            span.set_attribute(f"{prefix}.role", self.role.get(index, "assistant"))
            span.set_attribute(f"{prefix}.content", "".join(self.text[index]))
        if config is not None and _capture_output(config):
            texts = ["".join(self.text[index]) for index in sorted(self.text)]
            if len(texts) == 1:
                span.set_attribute(A.OUTPUT_VALUE, texts[0])
            elif texts:
                span.set_attribute(A.OUTPUT_VALUE, json.dumps(texts)[:_LIMIT])
                span.set_attribute(A.OUTPUT_MIME_TYPE, "application/json")


def _prompts(operation: str, args: tuple, kwargs: dict) -> list:
    if operation != "text_completion":
        return []
    prompt = _argument(args, kwargs, 0, "prompt")
    return [prompt] if isinstance(prompt, str) else (list(prompt) if isinstance(prompt, (list, tuple)) else [])


def _start(tracer: Any, config: TraceConfig, instance: Any, operation: str, args: tuple, kwargs: dict, method: str):
    embeddings = operation == "embeddings"
    model = getattr(instance, "model_id", None) or getattr(instance, "deployment_id", None)
    attrs: dict[str, Any] = {A.GEN_AI_PROVIDER_NAME: PROVIDER, A.GEN_AI_OPERATION_NAME: operation}
    if isinstance(model, str):
        attrs[A.GEN_AI_REQUEST_MODEL] = model
    # Parameters set when the SDK object was created apply to every call; call-level ones win.
    params = {**_params(getattr(instance, "params", None)), **_params(_argument(args, kwargs, 1, "params"))}
    # Template input is user content: it goes under the key core hides with the input flags,
    # never into the request parameters (core hides those only under hide_inputs).
    variables = params.pop("prompt_variables", None)
    if isinstance(variables, dict) and variables:
        attrs[A.GEN_AI_PROMPT_TEMPLATE_VARIABLES] = _json(variables)
    for attr, names, kind in _REQUEST_ATTRIBUTES:
        for name in names:
            number = _number(params.get(name), kind)
            if number is not None:
                attrs[attr] = number
                break
    stops = params.get("stop_sequences") or params.get("stop")
    if isinstance(stops, (list, tuple)) and all(isinstance(s, str) for s in stops):
        attrs[A.GEN_AI_REQUEST_STOP_SEQUENCES] = list(stops)
    if params:
        attrs[A.GEN_AI_REQUEST_PARAMETERS] = _json(params)

    if embeddings:
        inputs = _argument(args, kwargs, 0, "inputs") if method in ("generate", "agenerate") else (
            _argument(args, kwargs, 0, "texts") if "documents" in method else _argument(args, kwargs, 0, "text"))
        texts = [inputs] if isinstance(inputs, str) else inputs
        for position, text in enumerate(texts if isinstance(texts, (list, tuple)) else []):
            if isinstance(text, str):
                attrs[f"{A.EMBEDDING_EMBEDDINGS}.{position}.embedding.text"] = text[:_LIMIT]
    elif operation == "chat":
        messages = _argument(args, kwargs, 0, "messages")
        position = 0
        plain = []
        for message in messages if isinstance(messages, (list, tuple)) else []:
            content = _text_content(_field(message, "content"))
            role = _field(message, "role")
            if isinstance(role, str):
                attrs[f"{A.GEN_AI_INPUT_MESSAGES}.{position}.message.role"] = role
                if content is not None:
                    attrs[f"{A.GEN_AI_INPUT_MESSAGES}.{position}.message.content"] = content
                    if isinstance(_field(message, "content"), str):
                        plain.append({"role": role, "content": content})
                position += 1
        # Only plain-text conversations become input.value: it cannot tell text from an inline image.
        if messages and len(plain) == position == len(messages) and _capture_input(config):
            attrs[A.INPUT_VALUE] = json.dumps(plain)[:_LIMIT]
            attrs[A.INPUT_MIME_TYPE] = "application/json"
    else:
        prompts = _prompts(operation, args, kwargs)
        for position, text in enumerate(prompts):
            if isinstance(text, str):
                attrs[f"{A.GEN_AI_INPUT_MESSAGES}.{position}.message.role"] = "user"
                attrs[f"{A.GEN_AI_INPUT_MESSAGES}.{position}.message.content"] = text[:_LIMIT]
        if prompts and all(isinstance(t, str) for t in prompts) and _capture_input(config):
            value = prompts[0] if len(prompts) == 1 and isinstance(_argument(args, kwargs, 0, "prompt"), str) else prompts
            attrs[A.INPUT_VALUE] = json.dumps(value)[:_LIMIT]
            attrs[A.INPUT_MIME_TYPE] = "application/json"
    kind = FiSpanKindValues.EMBEDDING if embeddings else FiSpanKindValues.LLM
    return tracer.start_span(f"watsonx.{operation}", attributes=attrs, fi_span_kind=kind)


class _Stream:
    """Passes chunks through unchanged; the span ends when the stream ends or is closed."""

    def __init__(self, source: Any, span: Any, operation: str, config: TraceConfig, batch: Optional[list] = None):
        self._source, self._span, self._operation, self._config = source, span, operation, config
        self._output = _Output()
        self._ended = False
        # ``generate(prompt=[...], async_mode=True)`` yields one whole response per prompt, in
        # completion order; ``batch`` holds the prompts so each answer lands on its own index.
        self._batch = batch
        self._seen: set[int] = set()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._source, name)

    def _finish(self, complete: bool = True) -> None:
        if self._ended:
            return
        self._ended = True
        try:
            if not complete:
                self._span.set_attribute("watsonx.stream.incomplete", True)
            self._output.apply(self._span, self._operation, complete, self._config)
        finally:
            self._span.end()

    def _fail(self, exc: BaseException) -> None:
        if not self._ended:
            self._ended = True
            _failure(self._span, exc)

    def _slot(self, chunk: Any) -> int:
        results = chunk.get("results") if isinstance(chunk, dict) else None
        echoed = results[0].get("input_text") if results and isinstance(results[0], dict) else None
        for index, prompt in enumerate(self._batch or []):
            if index not in self._seen and prompt == echoed:
                break
        else:
            index = next((i for i in range(len(self._batch or [])) if i not in self._seen), len(self._seen))
        self._seen.add(index)
        return index

    def _observe(self, chunk: Any) -> None:
        try:
            if self._batch is not None:
                self._output.feed(chunk, offset=self._slot(chunk), total=True)
            else:
                self._output.feed(chunk)
        except Exception:  # never let tracing break the caller's stream
            pass

    def __iter__(self):
        return self

    def __next__(self):
        try:
            with trace.use_span(self._span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                chunk = next(self._source)
        except StopIteration:
            self._finish()
            raise
        except BaseException as exc:
            self._fail(exc)
            raise
        self._observe(chunk)
        return chunk

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            with trace.use_span(self._span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                chunk = await self._source.__anext__()
        except StopAsyncIteration:
            self._finish()
            raise
        except BaseException as exc:
            self._fail(exc)
            raise
        self._observe(chunk)
        return chunk

    def close(self):
        try:
            return self._source.close()
        finally:
            self._finish(complete=False)

    async def aclose(self):
        try:
            return await self._source.aclose()
        finally:
            self._finish(complete=False)

    def __del__(self):
        try:
            self._finish(complete=False)
        except Exception:
            pass


def _complete(span: Any, result: Any, operation: str, config: TraceConfig, output: _Output,
              method: str = "", prompts: Optional[list] = None) -> Any:
    if _is_stream(result):
        # A generator from ``generate`` is the multi-prompt async_mode batch, not a token stream.
        batch = prompts if method == "generate" and hasattr(result, "__next__") else None
        return _Stream(result, span, operation, config, batch)
    try:
        if not output.absorbed:  # otherwise an inner SDK call already supplied the response
            output.feed(result, total=True)
        output.apply(span, operation, config=config)
    except Exception:  # never let tracing break a successful call
        pass
    finally:
        try:
            span.end()
        except Exception:
            pass
    return result


def _wrap(tracer: Any, config: TraceConfig, operation: str, method: str, is_async: bool):
    def begin(instance: Any, args: tuple, kwargs: dict):
        """The span and its accumulator, or None when tracing itself fails."""
        try:
            owner, rest = (args[0], args[1:]) if instance is None and args else (instance, args)
            return _start(tracer, config, owner, operation, rest, kwargs, method), _prompts(operation, rest, kwargs)
        except Exception:
            return None

    if is_async:
        async def wrapped(wrapped, instance, args, kwargs):
            outer = _INSIDE.get()
            if outer is not None:
                result = await wrapped(*args, **kwargs)
                outer.absorb(result)
                return result
            started = begin(instance, args, kwargs)
            if started is None:
                return await wrapped(*args, **kwargs)
            span, prompts = started
            output = _Output()
            token = _INSIDE.set(output)
            try:
                with trace.use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                    result = await wrapped(*args, **kwargs)
            except BaseException as exc:
                _failure(span, exc)
                raise
            finally:
                _INSIDE.reset(token)
            return _complete(span, result, operation, config, output, method, prompts)
    else:
        def wrapped(wrapped, instance, args, kwargs):
            outer = _INSIDE.get()
            if outer is not None:
                result = wrapped(*args, **kwargs)
                outer.absorb(result)
                return result
            started = begin(instance, args, kwargs)
            if started is None:
                return wrapped(*args, **kwargs)
            span, prompts = started
            output = _Output()
            token = _INSIDE.set(output)
            try:
                with trace.use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                    result = wrapped(*args, **kwargs)
            except BaseException as exc:
                _failure(span, exc)
                raise
            finally:
                _INSIDE.reset(token)
            return _complete(span, result, operation, config, output, method, prompts)
    return wrapped


class WatsonxInstrumentor(BaseInstrumentor):
    """Trace ``ModelInference`` text generation and chat, and ``Embeddings``, in ibm-watsonx-ai."""

    def instrumentation_dependencies(self) -> Collection[str]:
        return ("ibm-watsonx-ai >= 1.3.0, < 2",)

    def _instrument(self, **kwargs: Any) -> None:
        config = kwargs.get("config") or TraceConfig()
        if not isinstance(config, TraceConfig):
            raise TypeError("config must be a TraceConfig")
        tracer = FITracer(trace.get_tracer(__name__, __version__, kwargs.get("tracer_provider")), config)
        self._originals = []
        for module, cls_name, method, operation, is_async in _METHODS:
            cls = getattr(import_module(module), cls_name)
            original = cls.__dict__.get(method)
            if original is None:  # this SDK version does not have the method
                continue
            wrap_function_wrapper(module, f"{cls_name}.{method}", _wrap(tracer, config, operation, method, is_async))
            self._originals.append((cls, method, original))

    def _uninstrument(self, **kwargs: Any) -> None:
        for cls, name, original in getattr(self, "_originals", []):
            setattr(cls, name, original)
        self._originals = []


__all__ = ["WatsonxInstrumentor", "__version__"]
