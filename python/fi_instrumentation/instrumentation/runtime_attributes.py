"""Versioned OTel carrier for runtime identity; no provider, SDK or I/O.

This is a low-level snapshot helper, not a load emitter or skill registry.
Applications supply verified identities and complete parallel skill lists. The
SDK's agent_scope helper manages load tracking and list accumulation instead.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from types import MappingProxyType
from typing import Any

from opentelemetry.context import attach, detach, get_value, set_value
from opentelemetry.util.types import AttributeValue

RUNTIME_ATTRIBUTES_CONTEXT_KEY = "futureagi.runtime.span_attributes.v1"
_RUNTIME_PREFIXES = ("fi.skills.", "fi.context.", "gen_ai.agent.")
_SKILL_LIST_KEYS = ("fi.skills.version_ids", "fi.skills.ids", "fi.skills.refs")
_MAX_ATTRIBUTES = 64
_MAX_STRING_BYTES = 256
_MAX_SKILLS = 32
# Keys examined per mapping, as in the TypeScript consumer: a large foreign
# mapping adds bounded work. Pinned by the shared processor_v1.json vectors.
_MAX_SCAN = 256


def _short_string(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return len(value.encode("utf-8")) <= _MAX_STRING_BYTES
    except UnicodeEncodeError:
        return False


def _attributes(mapping: Mapping[str, Any]) -> dict[str, AttributeValue]:
    """Copy only flat OTel values; never stringify arbitrary application objects."""
    attrs: dict[str, AttributeValue] = {}
    truncated = False
    for scanned, (key, value) in enumerate(mapping.items(), 1):
        if scanned > _MAX_SCAN:
            break
        if not _short_string(key) or not key.startswith(_RUNTIME_PREFIXES):
            continue
        # Linked is not loaded, and belongs only on context.load, never the carrier.
        if key == "fi.context.linked_skill_version_ids":
            continue
        if isinstance(value, str):
            if not _short_string(value):
                continue
        elif type(value) in (bool, int, float):
            pass
        elif isinstance(value, (list, tuple)):
            limit = _MAX_SKILLS if key in _SKILL_LIST_KEYS else _MAX_ATTRIBUTES
            # Reject invalid elements rather than misalign parallel identity lists.
            if not all(_short_string(item) for item in value[:limit]):
                continue
            if key in _SKILL_LIST_KEYS:
                truncated = truncated or len(value) > _MAX_SKILLS
            elif len(value) > limit:
                continue
            value = tuple(value[:limit])
        else:
            continue
        attrs[key] = value
        # Leave room for normalized skill count and truncation below.
        if len(attrs) == _MAX_ATTRIBUTES - 2:
            break
    if "fi.skills.version_ids" in attrs:
        versions = attrs["fi.skills.version_ids"]
        if isinstance(versions, tuple):
            attrs["fi.skills.count"] = len(versions)
    if truncated:
        attrs["fi.skills.truncated"] = True
    elif attrs.get("fi.skills.truncated") is not True:
        attrs.pop("fi.skills.truncated", None)
    return attrs


@contextmanager
def using_runtime_attributes(
    mapping: Mapping[str, AttributeValue], *, reset_skills: bool = False,
) -> Iterator[None]:
    """Attach an immutable v1 carrier, restoring the previous context on exit.

    Only fi.skills.*, fi.context.* scalars and gen_ai.agent.* are accepted.
    Scalar fi.skill.* load identities and context linked-version lists are never
    propagated. Keep contents, paths, credentials and personal data out of values.

    Ordinary nesting inherits attributes, with supplied keys replacing their
    values (skill lists are complete snapshots, not deltas). Supplying a context
    identity replaces all inherited context and skill keys. reset_skills=True
    clears just the inherited skill keys. An inherited reset flag stays set.

    This helper emits no spans and does not install a processor. A compatible
    processor must consume the carrier for span stamping; explicit span values
    win. Use a fresh with block, also inside async functions/generator bodies.
    """
    supplied = _attributes(mapping)
    parent = get_value(RUNTIME_ATTRIBUTES_CONTEXT_KEY)
    inherited: dict[str, AttributeValue] = {}
    parent_reset = False
    if isinstance(parent, Mapping) and parent.get("v") == 1:
        parent_attrs = parent.get("attrs")
        if isinstance(parent_attrs, Mapping):
            inherited = _attributes(parent_attrs)
        parent_reset = parent.get("reset_skills") is True

    new_context = any(key.startswith("fi.context.") for key in supplied)
    if new_context or reset_skills:
        inherited = {
            key: value for key, value in inherited.items()
            if not key.startswith("fi.skills.")
            and not (new_context and key.startswith("fi.context."))
        }
    inherited.update(supplied)
    carrier = MappingProxyType({
        "v": 1,
        "attrs": MappingProxyType(_attributes(inherited)),
        "reset_skills": bool(reset_skills or new_context or parent_reset),
    })
    token = attach(set_value(RUNTIME_ATTRIBUTES_CONTEXT_KEY, carrier))
    try:
        yield
    finally:
        detach(token)
