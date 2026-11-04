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


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


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
    input_tokens = _first(usage, "prompt_tokens", "input_tokens")
    # Anthropic's native input excludes both cache reads and cache creation.
    # OpenAI prompt/input totals already include their nested cached_tokens.
    # Do not invent a base count when it is missing or invalid.
    if _field(usage, "prompt_tokens") is None and _is_count(input_tokens):
        input_tokens += sum(
            count for count in (
                _field(usage, "cache_read_input_tokens"),
                _field(usage, "cache_creation_input_tokens"),
            ) if _is_count(count)
        )
    for name, count in (
        ("input_tokens", input_tokens),
        ("output_tokens", _first(usage, "completion_tokens", "output_tokens")),
        ("total_tokens", _field(usage, "total_tokens")),
        ("cache_read_tokens", cache_read),
        ("cache_write_tokens", cache_write),
        ("output_tokens.reasoning", reasoning),
    ):
        if _is_count(count):
            yield f"gen_ai.usage.{name}", count
