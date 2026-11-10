"""Workflow sandbox module: no exporters, application tracing, or model SDKs."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class OrderWorkflow:
    def __init__(self):
        self.finished = False

    @workflow.run
    async def run(self) -> str:
        result = await workflow.execute_activity(
            "price_order",
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(milliseconds=10), maximum_attempts=2
            ),
        )
        await workflow.wait_condition(lambda: self.finished)
        return result

    @workflow.signal
    def complete(self):
        self.finished = True
