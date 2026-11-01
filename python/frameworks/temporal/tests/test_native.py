import asyncio
import os
import shutil
import uuid

import pytest
from fi_instrumentation import using_session
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from temporalio import activity
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from workflow_case import OrderWorkflow


@activity.defn
async def price_order() -> str:
    # Synthetic model/tool work; no model endpoint or key is used.
    with trace.get_tracer("synthetic-activity").start_as_current_span(
        "price-tool",
        attributes={
            "gen_ai.span.kind": "tool",
            "input.value": "PRIVATE-ORDER",
        },
    ):
        with trace.get_tracer("synthetic-activity").start_as_current_span(
            "model",
            attributes={
                "gen_ai.span.kind": "llm",
                "gen_ai.request.model": "synthetic-model",
                "gen_ai.usage.input_tokens": 5,
                "gen_ai.usage.output_tokens": 2,
                "output.value": "PRIVATE-ANSWER",
            },
        ):
            if activity.info().attempt == 1:
                raise ValueError("PRIVATE-ERROR")
    return "EUR 42"


@pytest.fixture
def temporal_binary():
    binary = os.getenv("TEMPORAL_CLI") or shutil.which("temporal")
    if not binary:
        if os.getenv("REQUIRE_TEMPORAL_INTEGRATION") == "1":
            pytest.fail("TEMPORAL_CLI is required for native integration qualification")
        pytest.skip(
            "Install Temporal CLI or set TEMPORAL_CLI to run native integration"
        )
    return binary


@pytest.mark.integration
def test_native_workflow_retry_signal_and_replay(tracing, temporal_binary):
    handle, exporter = tracing()

    async def run():
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=temporal_binary,
            dev_server_extra_args=["--ip", "127.0.0.1"],
            ui=False,
        ) as env:
            client = await Client.connect(
                env.client.service_client.config.target_host, plugins=[handle.plugin]
            )
            task_queue = "synthetic-" + uuid.uuid4().hex
            workflow_id = "order-" + uuid.uuid4().hex
            async with Worker(
                client,
                task_queue=task_queue,
                workflows=[OrderWorkflow],
                activities=[price_order],
            ):
                execution = await client.start_workflow(
                    OrderWorkflow.run, id=workflow_id, task_queue=task_queue
                )
                # env.client has no tracing plugin: this is an external signal with
                # no incoming trace context, so its handler starts another trace.
                await env.client.get_workflow_handle(workflow_id).signal(
                    OrderWorkflow.complete
                )
                assert await execution.result() == "EUR 42"
                history = await execution.fetch_history()
            assert handle.force_flush()
            before = exporter.get_finished_spans()
            names = [s.name for s in before]
            assert names.count("RunWorkflow:OrderWorkflow") == 1
            assert names.count("RunActivity:price_order") == 2
            assert names.count("StartActivity:price_order") == 1
            assert names.count("model") == 2
            assert names.count("price-tool") == 2
            assert names.count("HandleSignal:complete") == 1
            assert all(s.attributes.get("session.id") == workflow_id for s in before)
            model_spans = [s for s in before if s.name == "model"]
            assert (
                sum(s.attributes["gen_ai.usage.input_tokens"] for s in model_spans)
                == 10
            )
            activities = [s for s in before if s.name == "RunActivity:price_order"]
            assert (
                sum(s.status.status_code == StatusCode.ERROR for s in activities) == 1
            )
            by_id = {s.context.span_id: s for s in before}
            for model in model_spans:
                tool = by_id[model.parent.span_id]
                assert tool.name == "price-tool"
                assert by_id[tool.parent.span_id].name == "RunActivity:price_order"
            root = next(s for s in before if s.name == "StartWorkflow:OrderWorkflow")
            signal = next(s for s in before if s.name == "HandleSignal:complete")
            workflow_span = next(
                s for s in before if s.name == "RunWorkflow:OrderWorkflow"
            )
            assert signal.context.trace_id != root.context.trace_id
            assert workflow_span.context.trace_id == root.context.trace_id
            assert all(s.context.trace_id == root.context.trace_id for s in model_spans)
            assert "PRIVATE-" not in "".join(s.to_json() for s in before)
            # Replay the actual persisted history with the actual native plugin.
            # No custom replay hook or ID generator is used by the adapter.
            await Replayer(
                workflows=[OrderWorkflow], plugins=[handle.plugin]
            ).replay_workflow(history)
            assert handle.force_flush()
            assert len(exporter.get_finished_spans()) == len(before)

    asyncio.run(asyncio.wait_for(run(), timeout=60))


@activity.defn(name="price_order")
async def price_order_with_sessions() -> str:
    tracer = trace.get_tracer("synthetic-activity")
    with tracer.start_as_current_span(
        "explicit-parent", attributes={"session.id": "application-session"}
    ):
        with tracer.start_as_current_span("inherited-child"):
            with tracer.start_as_current_span("inherited-grandchild"):
                pass
        with using_session("context-session"):
            with tracer.start_as_current_span("context-child"):
                with tracer.start_as_current_span(
                    "explicit-child", attributes={"session.id": "span-session"}
                ):
                    pass
    with tracer.start_as_current_span("after-explicit-parent"):
        pass
    return "EUR 42"


@pytest.mark.integration
def test_native_activity_inherits_local_session_without_crossing_workflow_boundary(
    tracing, temporal_binary
):
    handle, exporter = tracing()

    async def run():
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=temporal_binary,
            dev_server_extra_args=["--ip", "127.0.0.1"],
            ui=False,
        ) as env:
            client = await Client.connect(
                env.client.service_client.config.target_host, plugins=[handle.plugin]
            )
            task_queue = "synthetic-" + uuid.uuid4().hex
            workflow_id = "order-" + uuid.uuid4().hex
            async with Worker(
                client,
                task_queue=task_queue,
                workflows=[OrderWorkflow],
                activities=[price_order_with_sessions],
            ):
                with trace.get_tracer("starter").start_as_current_span(
                    "workflow-starter", attributes={"session.id": "starter-session"}
                ):
                    execution = await client.start_workflow(
                        OrderWorkflow.run, id=workflow_id, task_queue=task_queue
                    )
                await env.client.get_workflow_handle(workflow_id).signal(
                    OrderWorkflow.complete
                )
                assert await execution.result() == "EUR 42"
            assert handle.force_flush()
            spans = {s.name: s for s in exporter.get_finished_spans()}
            expected_sessions = {
                "workflow-starter": "starter-session",
                "explicit-parent": "application-session",
                "inherited-child": "application-session",
                "inherited-grandchild": "application-session",
                "context-child": "context-session",
                "explicit-child": "span-session",
                "after-explicit-parent": workflow_id,
                "StartWorkflow:OrderWorkflow": workflow_id,
                "RunWorkflow:OrderWorkflow": workflow_id,
                "StartActivity:price_order": workflow_id,
                "RunActivity:price_order": workflow_id,
                "HandleSignal:complete": workflow_id,
            }
            assert {name: s.attributes["session.id"] for name, s in spans.items()} == (
                expected_sessions
            )
            parentage = {
                "StartWorkflow:OrderWorkflow": "workflow-starter",
                "explicit-parent": "RunActivity:price_order",
                "inherited-child": "explicit-parent",
                "inherited-grandchild": "inherited-child",
                "context-child": "explicit-parent",
                "explicit-child": "context-child",
                "after-explicit-parent": "RunActivity:price_order",
            }
            for child_name, parent_name in parentage.items():
                child, parent = spans[child_name], spans[parent_name]
                assert child.parent.span_id == parent.context.span_id
                assert child.context.trace_id == parent.context.trace_id

    asyncio.run(asyncio.wait_for(run(), timeout=60))
