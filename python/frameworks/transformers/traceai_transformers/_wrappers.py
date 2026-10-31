"""Fail-open wrappers. The original callable executes exactly once with its arguments intact."""

import logging
import time
from contextvars import ContextVar
from typing import Any

try:
    from fi_instrumentation.auto.wire._privacy import mask_attributes, secrets
except ImportError:
    # Older supported cores do not provide the agent's privacy API. Importing an
    # optional instrumentor must still work; leave hooks off rather than capture
    # content without that boundary or maintain a second secret registry.
    mask_attributes = secrets = None

from opentelemetry import context as context_api
from opentelemetry import trace as trace_api

from traceai_transformers import _attributes as extract

logger = logging.getLogger(__name__)
PRIVACY_AVAILABLE = callable(mask_attributes) and callable(secrets)
_ACTIVE: ContextVar[Any] = ContextVar("traceai_transformers_call", default=None)
_GENERATING: ContextVar[bool] = ContextVar("traceai_transformers_generating", default=False)
_STREAM: ContextVar[Any] = ContextVar("traceai_transformers_stream", default=None)
TASKS = {
    "text-generation": ("LLM", "text_generation"),
    "text2text-generation": ("LLM", "text_generation"),
    "summarization": ("LLM", "text_generation"),
    "translation": ("LLM", "text_generation"),
    "feature-extraction": ("EMBEDDING", "embeddings"),
    "automatic-speech-recognition": ("LLM", "speech_to_text"),
}


def _attempt(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except Exception:
        # Do not log exception text/locals, which can contain model inputs.
        logger.debug("Transformers telemetry extraction unavailable")
        return None


class _Call:
    def __init__(self, owner, config, operation, kind, start):
        self.owner = owner
        self.config = config
        self.operation = operation
        self.start = start
        self.generations = []
        self.first_token = None
        self.audio_input_attributes = {}
        self.identity = {"gen_ai.span.kind": kind, "gen_ai.operation.name": operation,
                         "gen_ai.provider.name": "huggingface", "traceai.capture.layer": "library"}
        self.span = owner._tracer.start_span(
            "transformers." + operation,
            kind=trace_api.SpanKind.CLIENT,
            start_time=start,
            attributes=self.identity,
        )

    def finish(self, instance, args, kwargs, result, end, pipeline, tokenizer=None, error=None):
        # Every telemetry action is isolated, including end() itself. No internal
        # exception may replace a return value, exception or cancellation.
        attrs = {**self.identity, "gen_ai.response.latency": max(0.0, (end - self.start) / 1e9)}
        if self.first_token is not None:
            ttft = max(0.0, (self.first_token - self.start) / 1e9)
            attrs["gen_ai.response.time_to_first_token"] = ttft
            attrs["gen_ai.server.time_to_first_token"] = ttft * 1000  # traceAI's existing millisecond key.
        name = _attempt(extract.model_name, instance, pipeline)
        if name:
            attrs.update({"gen_ai.request.model": name, "gen_ai.response.model": name})
        params = _attempt(extract.parameters, instance, kwargs, pipeline, args) or {}
        attrs.update(_attempt(extract.parameter_attributes, params) or {})
        for model, gen_args, gen_kwargs, generated in self.generations:
            usage = _attempt(extract.generation_attributes, model, gen_args, gen_kwargs, generated,
                             None if pipeline else tokenizer) or {}
            for key, value in usage.items():
                if key.startswith("gen_ai.usage."):
                    attrs[key] = attrs.get(key, 0) + value
                else:
                    attrs[key] = value
        if pipeline:
            source = args[0] if args else kwargs.get("inputs", kwargs.get("text_inputs", kwargs.get("sequences")))
            if self.operation == "speech_to_text":
                attrs.update(self.audio_input_attributes)
                attrs.update(_attempt(extract.audio_attributes, None, instance, result) or {})
            else:
                inputs = _attempt(extract.messages, source) or []
                attrs.update(_attempt(extract.message_attributes, inputs, "input") or {})
                if self.operation == "embeddings":
                    attrs["gen_ai.request.embedding_inputs_count"] = len(inputs)
                    attrs.update(_attempt(extract.embedding_attributes, result) or {})
                    if "gen_ai.usage.input_tokens" not in attrs:
                        count = _attempt(self._token_count, instance, inputs)
                        if count is not None:
                            attrs["gen_ai.usage.input_tokens"] = count
                            attrs["gen_ai.usage.total_tokens"] = count
                else:
                    output_kwargs = dict(kwargs)
                    postprocess = getattr(instance, "_postprocess_params", {})
                    return_type = kwargs.get("return_type", postprocess.get("return_type"))
                    if "return_full_text" not in output_kwargs and getattr(return_type, "name", None) == "NEW_TEXT":
                        output_kwargs["return_full_text"] = False
                    if getattr(instance, "task", None) != "text-generation":
                        output_kwargs["return_full_text"] = False
                    outputs = _attempt(extract.output_messages, result, source, output_kwargs) or []
                    attrs.update(_attempt(extract.message_attributes, outputs, "output") or {})
        if error is not None:
            # The class alone is enough for status, and cannot quote raw media or
            # prompt/credential material like exception messages/stack traces can.
            attrs["error.type"] = type(error).__name__
            _attempt(self.span.set_status, trace_api.Status(trace_api.StatusCode.ERROR))
        else:
            _attempt(self.span.set_status, trace_api.Status(trace_api.StatusCode.OK))
        clean = _attempt(mask_attributes, attrs, self.config, secrets({}), self.operation)
        if clean is not None:
            # OTel evicts oldest attributes at its limit. Refresh identity and
            # append metadata after content so ordinary batches cannot evict
            # model, usage, operation or timing with flattened message fields.
            content = {key: value for key, value in clean.items()
                       if key.startswith(("input.", "output.", "gen_ai.input.", "gen_ai.output."))
                       or key == "gen_ai.audio.transcript"}
            metadata = {key: value for key, value in clean.items() if key not in content}
            _attempt(self.span.set_attributes, {**content, **metadata})

    @staticmethod
    def _token_count(instance, inputs):
        tokenizer = getattr(instance, "tokenizer", None)
        if tokenizer is None or not inputs:
            return None
        return sum(len(tokenizer.encode(item["content"], add_special_tokens=True)) for item in inputs)


class _Wrapper:
    def __init__(self, owner, config):
        self.owner = owner
        self.config = config
        self.epoch = owner._epoch

    def skip(self):
        return (not self.owner._enabled or self.owner._epoch is not self.epoch
                or bool(context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY)))

    def run(self, wrapped, instance, args, kwargs, pipeline, operation, kind, tokenizer=None):
        # Timestamp precedes validation, metadata reads and instrumentation work.
        start = time.time_ns()
        call = _attempt(_Call, self.owner, self.config, operation, kind, start)
        if call is None:
            return wrapped(*args, **kwargs)
        if pipeline and operation == "speech_to_text":
            # HF preprocessing pops array/raw and sampling_rate from caller-owned
            # dictionaries. Snapshot only scalar metadata before inference.
            source = args[0] if args else kwargs.get("inputs")
            call.audio_input_attributes = _attempt(extract.audio_attributes, source, instance, None) or {}
        context_token = _attempt(context_api.attach, trace_api.set_span_in_context(call.span))
        active_token = _ACTIVE.set(call)
        try:
            try:
                result = wrapped(*args, **kwargs)
            except BaseException as error:
                end = time.time_ns()
                _attempt(call.finish, instance, args, kwargs, None, end, pipeline, tokenizer, error)
                _attempt(call.span.end, end_time=end)
                raise
            end = time.time_ns()
            _attempt(call.finish, instance, args, kwargs, result, end, pipeline, tokenizer)
            _attempt(call.span.end, end_time=end)
            return result
        finally:
            call.generations.clear()
            _attempt(_ACTIVE.reset, active_token)
            if context_token is not None:
                _attempt(context_api.detach, context_token)


class PipelineWrapper(_Wrapper):
    def __call__(self, wrapped, instance, args, kwargs):
        if self.skip() or _ACTIVE.get() is not None:
            return wrapped(*args, **kwargs)
        task = _attempt(getattr, instance, "task", None)
        operation = TASKS.get(task) if type(task) is str else None
        if operation is None and type(task) is str and task.startswith("translation_"):
            operation = TASKS["translation"]
        if operation is None:
            return wrapped(*args, **kwargs)
        source = args[0] if args else kwargs.get("inputs", kwargs.get("text_inputs", kwargs.get("sequences")))
        # A lazy iterator must remain lazy and retain its identity. Its eventual
        # model.generate calls are traced independently when the user consumes it.
        if operation[1] != "speech_to_text" and not (isinstance(source, str) or type(source) is list):
            return wrapped(*args, **kwargs)
        return self.run(wrapped, instance, args, kwargs, True, operation[1], operation[0])


class GenerateWrapper(_Wrapper):
    def __init__(self, owner, config, tokenizer=None):
        super().__init__(owner, config)
        self.tokenizer = tokenizer

    def __call__(self, wrapped, instance, args, kwargs):
        if self.skip() or _GENERATING.get():
            return wrapped(*args, **kwargs)
        if _ACTIVE.get() is None:
            tokenizer = kwargs.get("tokenizer", self.tokenizer)
            return self.run(self._generation(wrapped, instance), instance, args, kwargs,
                            False, "text_generation", "LLM", tokenizer)
        return self._generation(wrapped, instance)(*args, **kwargs)

    @staticmethod
    def _generation(wrapped, instance):
        def invoke(*args, **kwargs):
            call = _ACTIVE.get()
            if call is None:
                # Span creation failed. Do not install generation/streamer state
                # without an owning call: inference must be an exact pass-through.
                return wrapped(*args, **kwargs)
            streamer = kwargs.get("streamer")
            if streamer is None and len(args) > 7:
                streamer = args[7]  # GenerationMixin's positional streamer (4.x).
            stream = {"streamer": streamer, "puts": 0, "call": call}
            stream_token = _STREAM.set(stream)
            generating_token = _GENERATING.set(True)
            try:
                result = wrapped(*args, **kwargs)
                if len(call.generations) < extract.MAX_ITEMS:
                    # Keep only sequence IDs for deferred extraction, not the
                    # ModelOutput's KV caches, logits or attention tensors.
                    generated = _attempt(extract.generation_sequences, result)
                    call.generations.append((instance, args, kwargs, generated))
                return result
            finally:
                _attempt(_GENERATING.reset, generating_token)
                _attempt(_STREAM.reset, stream_token)
        return invoke


class StreamerWrapper:
    def __init__(self, owner):
        self.owner = owner
        self.epoch = owner._epoch

    def __call__(self, wrapped, instance, args, kwargs):
        if not self.owner._enabled or self.owner._epoch is not self.epoch:
            return wrapped(*args, **kwargs)
        state = _STREAM.get()
        if state is not None and state["streamer"] is instance:
            now = time.time_ns()
            # Transformers first calls put(prompt_ids), including for a streamer
            # with skip_prompt=True. Only subsequent puts are generated tokens.
            if state["puts"] > 0 and state["call"].first_token is None:
                state["call"].first_token = now
            state["puts"] += 1
        return wrapped(*args, **kwargs)
