"""The runtime carrier is immutable, task-local and separate from load spans."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import pytest
from fi_instrumentation.fi_types import (
    CONTEXT_LOAD_SPAN_NAME,
    SKILL_LOAD_SPAN_NAME,
    SpanAttributes,
)
from fi_instrumentation.instrumentation.context_attributes import CONTEXT_ATTRIBUTES
from fi_instrumentation.instrumentation.runtime_attributes import (
    RUNTIME_ATTRIBUTES_CONTEXT_KEY,
    using_runtime_attributes,
)
from opentelemetry.context import attach, detach, get_current, get_value, set_value


def _carrier():
    return get_value("futureagi.runtime.span_attributes.v1")


@pytest.fixture(autouse=True)
def empty_carrier():
    token = attach(set_value(RUNTIME_ATTRIBUTES_CONTEXT_KEY, None))
    try:
        yield
    finally:
        detach(token)


def test_carrier_uses_shared_literal_and_only_runtime_prefixes():
    assert RUNTIME_ATTRIBUTES_CONTEXT_KEY == "futureagi.runtime.span_attributes.v1"
    with using_runtime_attributes({
        "gen_ai.agent.name": "refund-agent",
        "gen_ai.agent.id": "agent-1",
        "fi.context.name": "refund-context",
        "fi.skills.version_ids": ["version-1"],
        "fi.skills.ids": ["skill-1"],
        "fi.skills.refs": ["refund@v1"],
        "fi.skill.version_id": "not-a-load",
        "fi.context.linked_skill_version_ids": ["linked-is-not-loaded"],
        "fi.agent.name": "wrong-namespace",
        "user.id": "private",
        "service.name": "not-a-resource-helper",
        "session.id": "existing-helper",
    }):
        assert _carrier() == {
            "v": 1,
            "attrs": {
                "gen_ai.agent.name": "refund-agent",
                "gen_ai.agent.id": "agent-1",
                "fi.context.name": "refund-context",
                "fi.skills.version_ids": ("version-1",),
                "fi.skills.ids": ("skill-1",),
                "fi.skills.refs": ("refund@v1",),
                "fi.skills.count": 1,
            },
            "reset_skills": True,
        }
    assert _carrier() is None
    assert not any(key.startswith(("fi.skill.", "fi.skills.", "fi.context."))
                   for key in CONTEXT_ATTRIBUTES)


def test_input_and_carrier_cannot_be_mutated_through_aliases():
    versions = ["v1"]
    attrs = {"fi.skills.version_ids": versions, "gen_ai.agent.name": "first"}
    with using_runtime_attributes(attrs):
        versions.append("v2")
        attrs["gen_ai.agent.name"] = "changed"
        assert _carrier()["attrs"]["fi.skills.version_ids"] == ("v1",)
        assert _carrier()["attrs"]["gen_ai.agent.name"] == "first"
        with pytest.raises(TypeError):
            _carrier()["v"] = 2
        with pytest.raises(TypeError):
            _carrier()["attrs"]["gen_ai.agent.name"] = "changed"


def test_plain_nesting_inherits_and_restores_on_exception():
    with using_runtime_attributes({"gen_ai.agent.name": "parent", "fi.skills.version_ids": ["v1"]}):
        outer = _carrier()
        with pytest.raises(RuntimeError):
            with using_runtime_attributes({"gen_ai.agent.name": "child"}):
                assert _carrier()["attrs"]["gen_ai.agent.name"] == "child"
                assert _carrier()["attrs"]["fi.skills.version_ids"] == ("v1",)
                assert _carrier()["reset_skills"] is False
                raise RuntimeError("restore")
        assert _carrier() is outer
    assert _carrier() is None


def test_new_context_replaces_all_context_and_skill_keys():
    with using_runtime_attributes({
        "fi.context.id": "parent-context",
        "fi.context.binding_id": "parent-binding",
        "fi.context.session_id": "parent-session",
        "fi.skills.version_ids": ["v1"],
        "fi.skills.truncated": True,
    }):
        outer = _carrier()
        with using_runtime_attributes({"fi.context.id": "child-context"}):
            assert _carrier()["attrs"] == {"fi.context.id": "child-context"}
            assert _carrier()["reset_skills"] is True
            with using_runtime_attributes({"gen_ai.agent.name": "child"}):
                assert _carrier()["reset_skills"] is True
                assert "fi.context.binding_id" not in _carrier()["attrs"]
        assert _carrier() is outer


def test_explicit_skill_reset_keeps_context():
    with using_runtime_attributes({"fi.context.id": "context", "fi.skills.version_ids": ["v1"]}):
        with using_runtime_attributes({}, reset_skills=True):
            assert _carrier()["attrs"] == {"fi.context.id": "context"}
            assert _carrier()["reset_skills"] is True


@pytest.mark.parametrize("parent", [None, "not-a-carrier", {"v": 2, "attrs": {"fi.context.id": "future"}}, {"v": 1, "attrs": []}])
def test_unknown_or_malformed_parent_is_ignored_and_restored(parent):
    token = attach(set_value(RUNTIME_ATTRIBUTES_CONTEXT_KEY, parent))
    try:
        with using_runtime_attributes({"gen_ai.agent.name": "current"}):
            assert _carrier()["attrs"] == {"gen_ai.agent.name": "current"}
        assert _carrier() is parent
    finally:
        detach(token)


def test_values_are_flat_and_bounded_by_utf8_bytes():
    with using_runtime_attributes({
        "gen_ai.agent.name": "é" * 128,
        "gen_ai.agent.description": "é" * 129,
        "fi.context.digest": "\ud800",
        "fi.context.nested": {"secret": "not-flat"},
        "fi.context.object": object(),
        "fi.context.null": None,
        "fi.skills.refs": ["valid", object()],
        "fi.skills.count": 0,
        "fi.skills.truncated": False,
    }):
        assert _carrier()["attrs"] == {"gen_ai.agent.name": "é" * 128, "fi.skills.count": 0}


def test_skill_lists_are_capped_with_matching_count_and_truncation():
    with using_runtime_attributes({
        "fi.skills.version_ids": [f"v{i}" for i in range(40)],
        "fi.skills.ids": [f"s{i}" for i in range(40)],
        "fi.skills.refs": [f"skill@v{i}" for i in range(40)],
        "fi.skills.count": 40,
    }):
        attrs = _carrier()["attrs"]
        assert attrs["fi.skills.version_ids"] == tuple(f"v{i}" for i in range(32))
        assert len(attrs["fi.skills.ids"]) == len(attrs["fi.skills.refs"]) == 32
        assert attrs["fi.skills.count"] == 32
        assert attrs["fi.skills.truncated"] is True


def test_at_most_64_attributes_across_nested_scopes():
    with using_runtime_attributes({f"fi.context.field{i}": "x" for i in range(100)}):
        with using_runtime_attributes({f"gen_ai.agent.field{i}": "x" for i in range(100)}):
            assert len(_carrier()["attrs"]) <= 64


def test_unrelated_otel_context_is_preserved():
    token = attach(set_value("unrelated", "keep"))
    try:
        before = get_current()
        with using_runtime_attributes({"gen_ai.agent.name": "agent"}):
            assert get_value("unrelated") == "keep"
        assert get_current() is before
    finally:
        detach(token)


@pytest.mark.asyncio
async def test_concurrent_tasks_and_cancellation_restore_context():
    entered = asyncio.Event()
    cancelled_scope = []

    async def cancelled():
        try:
            with using_runtime_attributes({"gen_ai.agent.name": "cancelled"}):
                entered.set()
                await asyncio.Event().wait()
        finally:
            cancelled_scope.append(_carrier())

    async def sibling():
        with using_runtime_attributes({"gen_ai.agent.name": "sibling"}):
            await entered.wait()
            await asyncio.sleep(0)
            return _carrier()["attrs"]["gen_ai.agent.name"]

    task = asyncio.create_task(cancelled())
    sibling_task = asyncio.create_task(sibling())
    await entered.wait()
    assert _carrier() is None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await sibling_task == "sibling"
    assert cancelled_scope == [None]
    assert _carrier() is None


def test_thread_handoff_requires_explicit_context_copy():
    with ThreadPoolExecutor(max_workers=1) as pool:
        with using_runtime_attributes({"gen_ai.agent.name": "captured"}):
            assert pool.submit(_carrier).result() is None
            ctx = copy_context()
            assert pool.submit(ctx.run, _carrier).result()["attrs"]["gen_ai.agent.name"] == "captured"
        assert pool.submit(_carrier).result() is None


def test_load_names_and_every_new_schema_constant():
    assert SKILL_LOAD_SPAN_NAME == "skill.load"
    assert CONTEXT_LOAD_SPAN_NAME == "context.load"
    keys = (
        "fi.skill.id", "fi.skill.name", "fi.skill.version_id", "fi.skill.version",
        "fi.skill.digest", "fi.skill.requested_label", "fi.skill.requested_version",
        "fi.skill.load.via", "fi.skill.load.evidence", "fi.skill.load.cached",
        "fi.skill.load.root_file", "fi.skill.files", "fi.skill.bytes",
        "fi.skill.via_context.version_id", "fi.skills.version_ids", "fi.skills.ids",
        "fi.skills.refs", "fi.skills.count", "fi.skills.truncated", "fi.context.id",
        "fi.context.version_id", "fi.context.digest", "fi.context.name",
        "fi.context.version", "fi.context.requested_label", "fi.context.binding_id",
        "fi.context.session_id", "fi.context.linked_skill_version_ids",
        "gen_ai.agent.name", "gen_ai.agent.id", "error.type",
    )
    for key in keys:
        assert getattr(SpanAttributes, key.upper().replace(".", "_")) == key
