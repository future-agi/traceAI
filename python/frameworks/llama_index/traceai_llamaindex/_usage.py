"""Normalize supplied SDK usage without inventing missing or zero counts."""

from typing import Any, Iterator, Mapping, Tuple


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def _first(value: Any, *names: str) -> Any:
    for name in names:
        found = _field(value, name)
        if found is not None:
            return found
    return None


def token_counts(usage: Any) -> Iterator[Tuple[str, int]]:
    input_details = _first(usage, "prompt_tokens_details", "input_tokens_details")
    output_details = _first(usage, "completion_tokens_details", "output_tokens_details")
    cache_read = _first(usage, "cache_read_input_tokens", "cache_read_tokens")
    if cache_read is None:
        cache_read = _first(input_details, "cached_tokens", "cache_read_tokens")
    cache_write = _first(usage, "cache_creation_input_tokens", "cache_write_tokens")
    if cache_write is None:
        cache_write = _first(input_details, "cache_creation_tokens", "cache_write_tokens")
    reasoning = _first(output_details, "reasoning_tokens")
    if reasoning is None:
        reasoning = _field(usage, "reasoning_tokens")
    for name, count in (
        ("input_tokens", _first(usage, "prompt_tokens", "input_tokens")),
        ("output_tokens", _first(usage, "completion_tokens", "output_tokens")),
        ("total_tokens", _field(usage, "total_tokens")),
        ("cache_read_tokens", cache_read),
        ("cache_write_tokens", cache_write),
        ("output_tokens.reasoning", reasoning),
    ):
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            yield f"gen_ai.usage.{name}", count
