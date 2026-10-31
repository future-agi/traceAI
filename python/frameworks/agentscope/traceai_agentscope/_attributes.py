"""Bounded, text-only projections; never serialize a client, credential or media."""

import json
import math
from itertools import islice
from types import GetSetDescriptorType
from typing import Any

from fi_instrumentation.auto.wire._privacy import mask_attributes

MAX_TEXT = 16_384
MAX_ITEMS = 64
OMITTED_TEXT = "<omitted: text limit>"


def bounded_text(value: str, limit: int = MAX_TEXT) -> str:
    # A prefix can end halfway through a credential, signed URL or media value;
    # neither local scrubbing nor the export boundary could then recognize it.
    return value if len(value) <= limit else OMITTED_TEXT


def _data(value: Any) -> dict:
    """Read instance storage through a builtin descriptor, never a user getter."""
    cls = type(value)
    for base in type.__getattribute__(cls, "__mro__"):
        descriptor = type.__getattribute__(base, "__dict__").get("__dict__")
        if type(descriptor) is GetSetDescriptorType:
            data = descriptor.__get__(value, cls)
            return data if type(data) is dict else {}
    return {}


def _module(value: Any) -> str:
    module = type.__getattribute__(type(value), "__module__")
    return module if type(module) is str else ""


class Attributes:
    def __init__(self, config: Any) -> None:
        from agentscope import message, model, tool
        from agentscope.model._model_usage import ChatUsage

        self.config = config
        # Exact types only: a user subclass may implement arbitrary getters.
        self.types = tuple(
            cls
            for module in (message, model, tool)
            for cls in vars(module).values()
            if isinstance(cls, type) and cls.__module__.startswith("agentscope.")
        ) + (ChatUsage,)

    def field(self, value: Any, key: str, default: Any = None) -> Any:
        if type(value) is dict:
            return value.get(key, default)
        if any(type(value) is cls for cls in self.types):
            if isinstance(value, dict):
                # AgentScope ChatResponse / ChatUsage use DictMixin: their
                # dataclass fields live in the dict, not in __dict__.
                return dict.get(value, key, default)
            return _data(value).get(key, default)
        return default

    def label(self, value: Any) -> Any:
        if type(value) is str:
            return value
        if any(type(value) is cls for cls in self.types):
            label = _data(value).get("_value_")
            return label if type(label) is str else None
        return None

    def safe(self, value: Any, depth: int = 0, budget: Any = None) -> Any:
        if budget is None:
            budget = [256, MAX_TEXT * 4]
        budget[0] -= 1
        if budget[0] < 0 or depth > 8 or budget[1] <= 0:
            return "<omitted: capture limit>"
        if type(value) is str:
            result = bounded_text(value, min(MAX_TEXT, budget[1]))
            budget[1] -= len(result)
            return result
        if value is None or type(value) is bool or type(value) is int:
            return value
        if type(value) is float:
            return (
                value
                if math.isfinite(value)
                else (
                    "NaN"
                    if math.isnan(value)
                    else "Infinity"
                    if value > 0
                    else "-Infinity"
                )
            )
        if type(value) is dict:
            return {
                bounded_text(key, 256): self.safe(item, depth + 1, budget)
                for key, item in islice(value.items(), MAX_ITEMS)
                if type(key) is str
            }
        if type(value) is list or type(value) is tuple:
            return [self.safe(item, depth + 1, budget) for item in value[:MAX_ITEMS]]
        return "<" + type.__getattribute__(type(value), "__name__") + ">"

    def json(self, value: Any) -> str:
        return json.dumps(self.safe(value), ensure_ascii=False, separators=(",", ":"))

    def content(self, value: Any) -> str:
        if type(value) is str:
            return bounded_text(value)
        if type(value) is not list and type(value) is not tuple:
            value = self.field(value, "content", [])
        if type(value) is str:
            return bounded_text(value)
        if type(value) is not list and type(value) is not tuple:
            return ""
        texts = []
        size = 0
        for block in value[:MAX_ITEMS]:
            text = self.field(block, "text")
            if type(text) is str:
                size += len(text) + bool(texts)
                if size > MAX_TEXT:
                    return OMITTED_TEXT
                texts.append(text)
        return "\n".join(texts)

    def messages(self, value: Any, side: str) -> dict:
        if type(value) is not list and type(value) is not tuple:
            value = [value]
        attrs = {}
        for i, msg in enumerate(value[:MAX_ITEMS]):
            prefix = f"gen_ai.{side}.messages.{i}.message"
            role = self.field(msg, "role")
            if type(role) is str:
                attrs[f"{prefix}.role"] = bounded_text(role, 128)
            content = self.field(msg, "content")
            text = self.content(content)
            if text:
                attrs[f"{prefix}.content"] = text
            if type(content) is not list and type(content) is not tuple:
                continue
            k = 0
            for block in content[:MAX_ITEMS]:
                name = self.field(block, "name")
                if (
                    self.label(self.field(block, "type"))
                    not in ("tool_use", "tool_call")
                    or type(name) is not str
                ):
                    continue
                tool = f"{prefix}.tool_calls.{k}.tool_call"
                attrs[f"{tool}.function.name"] = bounded_text(name, 256)
                call_id = self.field(block, "id")
                if type(call_id) is str:
                    attrs[f"{tool}.id"] = bounded_text(call_id, 256)
                arguments = self.field(block, "input")
                attrs[f"{tool}.function.arguments"] = (
                    bounded_text(arguments)
                    if type(arguments) is str
                    else self.json(arguments)
                )
                k += 1
        return attrs

    def request(self, kind: str, instance: Any, args: tuple, kwargs: dict) -> dict:
        attrs = {"traceai.capture.layer": "library", "gen_ai.system": "agentscope"}
        if kind == "LLM":
            # These are framework-owned fields, not arbitrary model parameters.
            data = _data(instance)
            model = data.get("model", data.get("model_name"))
            if type(model) is str:
                attrs["gen_ai.request.model"] = bounded_text(model, 256)
            attrs["gen_ai.operation.name"] = "chat"
            classname = type.__getattribute__(type(instance), "__name__").lower()
            for provider in (
                "openai",
                "anthropic",
                "dashscope",
                "gemini",
                "ollama",
                "deepseek",
                "minimax",
                "moonshot",
                "xai",
                "volcengine",
            ):
                if provider in classname:
                    attrs["gen_ai.provider.name"] = provider
                    break
            stream = data.get("stream")
            if type(stream) is bool:
                attrs["gen_ai.request.stream"] = stream
            messages = kwargs.get("messages", args[0] if args else None)
            attrs.update(self.messages(messages, "input"))
            # Do not export raw requests: client_kwargs / extra_body can contain auth.
            params = data.get("generate_kwargs", {})
            params = params if type(params) is dict else {}
            parameter_object = data.get("parameters")
            if parameter_object is not None and _module(parameter_object).startswith(
                "agentscope."
            ):
                params = _data(parameter_object)
            selected = {}
            for key in (
                "temperature",
                "top_p",
                "max_tokens",
                "seed",
                "frequency_penalty",
                "presence_penalty",
            ):
                value = kwargs.get(key, params.get(key))
                if (type(value) is int or type(value) is float) and (
                    type(value) is int or math.isfinite(value)
                ):
                    attrs[f"gen_ai.request.{key}"] = value
                    selected[key] = value
            attrs["gen_ai.request.parameters"] = self.json(selected)
            tools = kwargs.get("tools")
            if type(tools) is list:
                for i, definition in enumerate(tools[:MAX_ITEMS]):
                    attrs[f"gen_ai.tool.definitions.{i}.tool.json_schema"] = self.json(
                        definition
                    )
        elif kind == "TOOL":
            call = kwargs.get("tool_call", args[0] if args else None)
            attrs["gen_ai.operation.name"] = "execute_tool"
            for field, key in (
                ("name", "gen_ai.tool.name"),
                ("id", "gen_ai.tool.call.id"),
            ):
                value = self.field(call, field)
                if type(value) is str:
                    attrs[key] = bounded_text(value, 256)
            value = self.field(call, "input")
            attrs["input.value"] = (
                bounded_text(value) if type(value) is str else self.json(value)
            )
            attrs["input.mime_type"] = "application/json"
            attrs["gen_ai.tool.call.arguments"] = attrs["input.value"]
        else:
            attrs["gen_ai.operation.name"] = (
                "invoke_agent" if kind == "AGENT" else "invoke_workflow"
            )
            if instance is not None:
                data = _data(instance)
                name = data.get("name")
                if kind == "AGENT" and type(name) is str:
                    attrs["gen_ai.agent.name"] = bounded_text(name, 256)
            value = kwargs.get(
                "inputs", kwargs.get("msg", kwargs.get("x", args[0] if args else None))
            )
            attrs.update(self.messages(value, "input"))
        return attrs

    def response(self, value: Any, kind: str) -> dict:
        attrs = {}
        text = self.content(value)
        if text:
            attrs["output.value"] = text
            attrs["output.mime_type"] = "text/plain"
            if kind in ("LLM", "AGENT"):
                attrs["gen_ai.output.messages.0.message.role"] = "assistant"
                attrs["gen_ai.output.messages.0.message.content"] = text
        if kind == "LLM":
            attrs.update(
                self.messages(
                    {"role": "assistant", "content": self.field(value, "content", [])},
                    "output",
                )
            )
            response_id = self.field(value, "id")
            if type(response_id) is str:
                attrs["gen_ai.response.id"] = bounded_text(response_id, 256)
            usage = self.field(value, "usage")
            for source, target in (
                ("input_tokens", "input_tokens"),
                ("output_tokens", "output_tokens"),
                ("cache_input_tokens", "cache_read_tokens"),
                ("cache_creation_input_tokens", "cache_write_tokens"),
            ):
                count = self.field(usage, source)
                if type(count) is int:
                    attrs[f"gen_ai.usage.{target}"] = count
            if (
                "gen_ai.usage.input_tokens" in attrs
                and "gen_ai.usage.output_tokens" in attrs
            ):
                attrs["gen_ai.usage.total_tokens"] = (
                    attrs["gen_ai.usage.input_tokens"]
                    + attrs["gen_ai.usage.output_tokens"]
                )
        if kind == "TOOL" and text:
            attrs["gen_ai.tool.call.result"] = text
        return attrs

    def clean(self, attrs: dict) -> dict:
        # Best-effort structural/media/hide masking only. Known-secret replacement
        # belongs to the core's export-time boundary, not SDK client inspection.
        return mask_attributes(attrs, self.config, ())
