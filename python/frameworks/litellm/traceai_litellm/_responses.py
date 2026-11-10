"""Normalize Responses items/events without importing version-specific SDK types."""

from copy import deepcopy
from typing import Mapping


def as_dict(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    elif hasattr(value, "to_dict"):
        value = value.to_dict()
    return value if isinstance(value, Mapping) else {}


def response_messages(items):
    """Translate Responses messages/tool linkage to the shared message schema."""
    if isinstance(items, str):
        return [{"role": "user", "content": items}]
    messages = []
    for item in items or []:
        item = as_dict(item)
        kind = item.get("type", "message")
        if kind == "function_call":
            messages.append(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": item.get("call_id") or item.get("id"),
                            "function": {
                                "name": item.get("name"),
                                "arguments": item.get("arguments"),
                            },
                        }
                    ],
                }
            )
        elif kind == "function_call_output":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id"),
                    "content": item.get("output"),
                }
            )
        elif kind == "message":
            content = item.get("content")
            if isinstance(content, list):
                blocks = []
                for block in content:
                    block = as_dict(block)
                    if block.get("type") in ("input_text", "output_text"):
                        blocks.append({"type": "text", "text": block.get("text")})
                    elif block.get("type") == "input_image":
                        blocks.append(
                            {
                                "type": "image_url",
                                "image_url": {"url": block.get("image_url")},
                            }
                        )
                    else:
                        blocks.append(dict(block))
                content = blocks
            messages.append({"role": item.get("role", "user"), "content": content})
    return messages


class ResponseEvents:
    """Keep deltas by output/content index; done snapshots replace, never append."""

    def __init__(self):
        self.items = {}
        self.failed = False

    def add(self, event):
        kind = event.get("type", "")
        index = event.get("output_index", 0)
        if kind in ("response.output_item.added", "response.output_item.done"):
            self.items[index] = deepcopy(dict(as_dict(event.get("item"))))
        elif kind in (
            "response.content_part.added",
            "response.content_part.done",
            "response.output_text.delta",
            "response.output_text.done",
        ):
            item = self.items.setdefault(
                index, {"type": "message", "role": "assistant", "content": []}
            )
            parts = item.setdefault("content", [])
            content_index = event.get("content_index", 0)
            while len(parts) <= content_index:
                parts.append({"type": "output_text", "text": ""})
            if kind.startswith("response.content_part."):
                parts[content_index] = deepcopy(dict(as_dict(event.get("part"))))
            elif kind.endswith(".delta"):
                parts[content_index]["text"] = parts[content_index].get(
                    "text", ""
                ) + event.get("delta", "")
            else:
                parts[content_index]["text"] = event.get("text", "")
        elif kind in (
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
        ):
            item = self.items.setdefault(
                index, {"type": "function_call", "arguments": ""}
            )
            if kind.endswith(".delta"):
                item["arguments"] = item.get("arguments", "") + event.get("delta", "")
            else:
                item["arguments"] = event.get("arguments", "")
            if event.get("name"):
                item["name"] = event["name"]
        response = as_dict(event.get("response"))
        if kind in ("response.completed", "response.incomplete", "response.failed"):
            if response.get("output") is not None:
                self.items = {
                    i: deepcopy(dict(as_dict(item)))
                    for i, item in enumerate(response["output"])
                }
        if kind in ("error", "response.failed") or response.get("status") == "failed":
            self.failed = True
        return response

    def messages(self):
        return response_messages([self.items[i] for i in sorted(self.items)])
