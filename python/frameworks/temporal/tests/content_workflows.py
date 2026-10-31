"""Real sandboxed workflows exercising decoded content and deterministic replay."""

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow


@activity.defn
async def echo_content(value: Any) -> Any:
    return value


@workflow.defn
class ContentWorkflow:
    @workflow.run
    async def run(self, value: Any) -> Any:
        return await workflow.execute_activity(
            echo_content, value, start_to_close_timeout=timedelta(seconds=10),
        )


@workflow.defn
class UnserializableResultWorkflow:
    @workflow.run
    async def run(self) -> Any:
        return object()


@workflow.defn
class DeterministicContentWorkflow:
    @workflow.run
    async def run(self, value: dict) -> dict:
        # The app owns this seed. Capture must not advance its random sequence.
        workflow.random().seed(315)
        value["random"] = workflow.random().random()
        value["mutated"] = True
        await workflow.sleep(timedelta(milliseconds=1))
        return await workflow.execute_activity(
            echo_content, value, start_to_close_timeout=timedelta(seconds=10),
        )
