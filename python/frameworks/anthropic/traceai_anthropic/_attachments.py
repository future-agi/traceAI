"""Anthropic Messages media capture through the shared, provider-owned attachment manager.

Mirrors ``@traceai/anthropic`` (``captureAnthropicAttachments`` /
``projectAnthropicMedia``): typed base64 image and PDF document blocks, and one
level of ``tool_result`` content. A tool-result image is a ``computer_screenshot``
only when the request declares a typed computer tool (``computer_YYYYMMDD``) AND
an assistant ``tool_use`` of that tool has the result's ``tool_use_id``; message
and block indices preserve causal order for the screenshot filmstrip. Nothing
here fetches URL sources or resolves file IDs.

This module imports ``fi_instrumentation.attachments``; the instrumentor imports
it only when an attachment manager is passed.
"""

import re
from types import MappingProxyType
from typing import Any, Mapping

from fi_instrumentation.attachments.media_capture import (
    EMPTY,
    IMAGE_MIMES,
    MAX_INLINE_BYTES,
    TOOL_ID,
    Observation,
    Occurrence,
    attach_inline,
    get,
    items,
    local_capture_policy,
)

__all__ = ("capture_anthropic_attachments", "project_anthropic_media", "anthropic_attachment_capabilities")

#: Declarative handoff; native SDK span ownership is not overridden by this adapter.
anthropic_attachment_capabilities = MappingProxyType({
    "schema_version": 1,
    "operations": ("messages.create.input", "tool_result.input"),
    "sources": ("inline_base64",),
    # Fail closed: no capture is attempted for these.
    "unsupported": ("remote_url", "provider_file", "generated_image", "beta_messages_hookup", "streamed_output"),
    "max_inline_bytes": MAX_INLINE_BYTES,
})

_COMPUTER_TOOL = re.compile(r"computer_\d{8}")
_DOCUMENT_MIMES = ("application/pdf",)


def _position(message_index: int, position: str) -> str:
    return "input/%d/%s" % (message_index, position)


def capture_anthropic_attachments(manager, span, payload: Any, *, trace_config=None,
                                  capture=None) -> Mapping[str, Occurrence]:
    """Capture typed media of one Messages request on the span the instrumentor emitted.

    ``payload`` holds the call's keyword arguments (``messages``, ``tools``).
    Returns admitted positions (position key -> occurrence); empty when no
    manager, disabled, unsampled or denied. Never raises or changes ``payload``.
    """
    observation = Observation(manager, span, "anthropic")
    if not observation.usable:
        return EMPTY
    try:
        messages = items(get(payload, "messages"))
        computer_names = set()
        for tool in items(get(payload, "tools")):
            kind, name = get(tool, "type"), get(tool, "name")
            if isinstance(kind, str) and _COMPUTER_TOOL.fullmatch(kind) and isinstance(name, str):
                computer_names.add(name)
        computer_calls = set()
        for message in messages:
            if get(message, "role") != "assistant":
                continue
            for block in items(get(message, "content")):
                block_id, name = get(block, "id"), get(block, "name")
                if get(block, "type") == "tool_use" and isinstance(block_id, str) and name in computer_names:
                    computer_calls.add(block_id)

        def visit(block, message_index, index, position, role, tool_call_id=None, error_detail=False):
            kind = get(block, "type")
            if kind not in ("image", "document"):
                return
            source = get(block, "source")
            if get(source, "type") != "base64":
                return
            mime = get(source, "media_type")
            if mime not in (IMAGE_MIMES if kind == "image" else _DOCUMENT_MIMES):
                return
            if kind == "document":
                purpose = "document"
            elif tool_call_id is not None:
                purpose = "computer_screenshot" if tool_call_id in computer_calls else "tool_result"
            else:
                purpose = "model_input"
            policy = local_capture_policy(trace_config, direction="input", base=capture, image=kind == "image",
                                          error_detail=error_detail)
            observation.visit(_position(message_index, position), lambda: attach_inline(
                manager, span, lambda: get(source, "data"), (mime,), raw=True, direction="input",
                purpose=purpose, capture=policy, message_index=message_index, part_index=index, role=role,
                tool_call_id=tool_call_id))

        for message_index, message in enumerate(messages):
            role = "assistant" if get(message, "role") == "assistant" else "user"
            for index, block in enumerate(items(get(message, "content"))):
                if get(block, "type") == "tool_result" and isinstance(get(block, "content"), (list, tuple)):
                    tool_use_id = get(block, "tool_use_id")
                    # Invalid tool IDs fail closed instead of losing causal identity.
                    if not isinstance(tool_use_id, str) or not TOOL_ID.fullmatch(tool_use_id):
                        continue
                    for child_index, child in enumerate(items(get(block, "content"))):
                        visit(child, message_index, child_index, "%d/%d" % (index, child_index), "tool",
                              tool_use_id, get(block, "is_error") is True)
                else:
                    visit(block, message_index, index, str(index), role)
    except Exception:
        pass  # No source-bearing exceptions or changes to the application's call.
    return MappingProxyType(dict(observation.captured)) if observation.captured else EMPTY


def project_anthropic_media(payload: Any, captured: Mapping[str, Occurrence]) -> Any:
    """Telemetry-only copy: ONLY admitted blocks get a ``fi-attachment:`` URL source.

    With nothing admitted the SAME object is returned; the call's arguments are
    never mutated and only plain mappings are copied.
    """
    if not captured or not isinstance(payload, Mapping) or not isinstance(payload.get("messages"), list):
        return payload
    try:
        def replace(value, position):
            occurrence = captured.get(position)
            if occurrence is None or not isinstance(value, Mapping):
                return value
            return {**value, "source": {"type": "url", "url": "fi-attachment:" + occurrence.occurrence_id}}

        def message(value, message_index):
            if not isinstance(value, Mapping) or not isinstance(value.get("content"), list):
                return value
            content = []
            for index, block in enumerate(value["content"]):
                if isinstance(block, Mapping) and block.get("type") == "tool_result" \
                        and isinstance(block.get("content"), list):
                    block = {**block, "content": [
                        replace(child, _position(message_index, "%d/%d" % (index, child_index)))
                        for child_index, child in enumerate(block["content"])]}
                    content.append(block)
                else:
                    content.append(replace(block, _position(message_index, str(index))))
            return {**value, "content": content}

        return {**payload, "messages": [message(value, index) for index, value in enumerate(payload["messages"])]}
    except Exception:
        return payload
