"""Real sandboxed workflow definitions. No tracing API calls inside workflows."""

from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError


@activity.defn
async def llm_activity(url: str) -> str:
    import httpx
    async with httpx.AsyncClient() as client:
        response = await client.post(url + "/v1/chat/completions", json={
            "model": "test-model", "messages": [{"role": "user", "content": "ping"}],
        })
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


@activity.defn
def sync_activity(value: str) -> str:
    from opentelemetry import trace
    with trace.get_tracer("test.sync").start_as_current_span("sync.inner"):
        return value


@activity.defn
async def flaky_activity() -> str:
    if activity.info().attempt == 1:
        raise ApplicationError("private failure message")
    return "retried"


@workflow.defn
class ChildWorkflow:
    @workflow.run
    async def run(self, args: dict) -> str:
        return await workflow.execute_activity(
            llm_activity, args["url"], task_queue=args["activities"],
            start_to_close_timeout=timedelta(seconds=20),
        )


@workflow.defn
class ParentWorkflow:
    @workflow.run
    async def run(self, args: dict) -> list[str]:
        # Advancing the virtual clock must not wait in real time or use wall time.
        await workflow.sleep(timedelta(hours=2))
        direct = await workflow.execute_activity(
            llm_activity, args["url"], task_queue=args["activities"],
            start_to_close_timeout=timedelta(seconds=20),
        )
        child = await workflow.execute_child_workflow(
            ChildWorkflow.run, args, id=workflow.info().workflow_id + "-child",
            task_queue=args.get("children", workflow.info().task_queue),
        )
        return [direct, child]


@workflow.defn
class LocalWorkflow:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_local_activity(
            sync_activity, "local-result", start_to_close_timeout=timedelta(seconds=10),
        )


@workflow.defn
class RetryWorkflow:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_activity(
            flaky_activity, start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(initial_interval=timedelta(milliseconds=10), maximum_attempts=2),
        )


@workflow.defn
class FailedWorkflow:
    @workflow.run
    async def run(self) -> None:
        raise ApplicationError("sensitive workflow failure", non_retryable=True)


@workflow.defn
class ContinueWorkflow:
    @workflow.run
    async def run(self, count: int) -> int:
        if count:
            workflow.continue_as_new(count - 1)
        return count


@workflow.defn
class TimeoutWorkflow:
    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False, timeout=timedelta(seconds=1))


@workflow.defn
class SignalFailureWorkflow:
    def __init__(self):
        self.ready = False

    @workflow.query
    def is_ready(self) -> bool:
        return self.ready

    @workflow.signal
    def fail(self):
        raise ApplicationError("private signal failure", non_retryable=True)

    @workflow.run
    async def run(self, args: dict) -> None:
        await workflow.execute_activity(
            llm_activity, args["url"], task_queue=args["activities"],
            start_to_close_timeout=timedelta(seconds=20),
        )
        self.ready = True
        await workflow.wait_condition(lambda: False)


@workflow.defn
class UpdateStartWorkflow:
    def __init__(self):
        self.done = False

    @workflow.update
    async def finish(self) -> str:
        self.done = True
        return "updated"

    @workflow.run
    async def run(self, args: dict) -> str:
        value = await workflow.execute_activity(
            llm_activity, args["url"], task_queue=args["activities"],
            start_to_close_timeout=timedelta(seconds=20),
        )
        await workflow.wait_condition(lambda: self.done)
        return value


@workflow.defn
class WaitingWorkflow:
    def __init__(self):
        self.done = False

    @workflow.signal
    def finish(self):
        self.done = True

    @workflow.run
    async def run(self, queue: str) -> str:
        # Replay and workflow eviction must not consume workflow.random().
        before = workflow.random().random()
        await workflow.wait_condition(lambda: self.done)
        value = await workflow.execute_activity(
            sync_activity, str(before), task_queue=queue,
            start_to_close_timeout=timedelta(seconds=10),
        )
        return value
