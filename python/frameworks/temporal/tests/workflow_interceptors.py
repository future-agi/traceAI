"""Sandbox-safe outer interceptor used by the suppression-order regressions."""

from temporalio import workflow
from temporalio.worker import Interceptor, WorkflowInboundInterceptor

with workflow.unsafe.imports_passed_through():
    from fi_instrumentation import suppress_tracing


class _SuppressedWorkflow(WorkflowInboundInterceptor):
    async def execute_workflow(self, input):
        with suppress_tracing():
            return await super().execute_workflow(input)


class SuppressWorkflowInterceptor(Interceptor):
    def workflow_interceptor_class(self, input):
        return _SuppressedWorkflow
