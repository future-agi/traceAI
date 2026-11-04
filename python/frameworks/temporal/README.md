# traceAI Temporal

Send Temporal's native workflow and activity spans to Future AGI, with model and tool spans nested inside the activities that run them. The package configures Temporal's replay-safe OpenTelemetry provider; it does not replace Temporal's instrumentation or create duplicate workflow spans.

## Install

Use Python 3.10–3.13 and Temporal Python SDK 1.34 or 1.35.

```bash
pip install "traceAI-temporal==0.1.0"
```

Temporal marks its OpenTelemetry plugin and replay-safe provider as experimental. This package restricts the SDK range to the supported minor versions.

## Configure

Set the same project in the starter and worker processes. Supply your keys through your deployment's environment or secret manager.

```bash
export FI_PROJECT_NAME=temporal-orders
export FI_API_KEY='<your-api-key>'
export FI_SECRET_KEY='<your-secret-key>'
# Optional, for a self-hosted deployment:
# export FI_BASE_URL=https://your-futureagi-host
```

Call `configure()` once in each process, **before connecting the client**. Do not also call `fi_instrumentation.register()` or install another global tracer provider.

```python
from temporalio.client import Client
from traceai_temporal import configure

telemetry = configure()
client = await Client.connect("localhost:7233", plugins=[telemetry.plugin])
```

Workers created from this client inherit the native plugin. Do not add it to the worker again. The plugin enables Temporal's workflow and activity spans by default.

## Run a workflow

Start your Temporal cluster or a local [Temporal dev server](https://docs.temporal.io/cli):

```bash
temporal server start-dev
```

Keep workflow code deterministic. Save this ordinary workflow as `workflows.py`; it must not import the exporter, this package, or a model SDK.

```python
from datetime import timedelta
from temporalio import workflow


@workflow.defn
class OrderWorkflow:
    @workflow.run
    async def run(self, quantity: int) -> str:
        return await workflow.execute_activity(
            "price_order",
            quantity,
            start_to_close_timeout=timedelta(seconds=10),
        )
```

Save the worker and starter as `app.py`. This example runs them in one process; separate deployments use the same `configure()` and client setup in each process.

```python
import asyncio
import uuid
from opentelemetry import trace
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker
from traceai_temporal import configure
from workflows import OrderWorkflow


@activity.defn
async def price_order(quantity: int) -> str:
    with trace.get_tracer("orders").start_as_current_span(
        "calculate-price", attributes={"gen_ai.span.kind": "tool"}
    ):
        return f"EUR {quantity * 21}"


async def main():
    telemetry = configure()
    try:
        client = await Client.connect("localhost:7233", plugins=[telemetry.plugin])
        async with Worker(
            client,
            task_queue="orders",
            workflows=[OrderWorkflow],
            activities=[price_order],
        ):
            result = await client.execute_workflow(
                OrderWorkflow.run,
                2,
                id=f"order-{uuid.uuid4().hex}",
                task_queue="orders",
            )
            print(result)
    finally:
        # Stop workers first. These methods return False if the time budget expires.
        if not telemetry.shutdown(timeout_millis=3000):
            print("Tracing shutdown timed out; the final batch may be lost")


asyncio.run(main())
```

```bash
python app.py
```

In Future AGI, open **Observe**, select `temporal-orders`, and open the latest `StartWorkflow:OrderWorkflow` trace. It contains `RunWorkflow`, `StartActivity`, `RunActivity`, and `calculate-price`. The workflow ID is the session ID, including on nested tool/model spans and signal handlers.

## Add model tracing inside activities

Temporal's plugin does not trace model requests. Install the traceAI package for your model SDK and instrument it **once**, using `telemetry.provider`. For example, with `traceAI-openai` installed:

```python
from traceai_openai import OpenAIInstrumentor

OpenAIInstrumentor().instrument(tracer_provider=telemetry.provider)
```

Make model calls only inside activities, not replayed workflow code. Provider spans keep their model, token usage, and span kind; the adapter does not copy token totals onto workflow or activity parents. Do not instrument the same model call through two packages.

## Privacy

The default is **metadata only**: workflow/run/activity IDs, span names and timing, status, model/tool identifiers, token counts, and explicit session/user IDs. Unknown attributes, content, arbitrary events, status text, and exception messages are removed before this destination exports. Exception types remain. Exception stack attributes are always removed, including with content enabled. Resource and scope attributes are filtered, and link attributes and trace state are removed.

Use opaque workflow IDs and user IDs. Span names, instrumentation-scope names, and allowed metadata values are not secret scanners; do not put prompts, emails, paths, or credentials in them.

To include supported input/output and message attributes, opt in in both processes as needed:

```python
telemetry = configure(capture_content=True)
```

Error text needs a second explicit opt-in, `keep_error_text=True`, together with `capture_content=True`. The core `FI_HIDE_*` settings remain privacy ceilings; `FI_HIDE_ERROR_DETAILS=true` still removes error text. Content values are capped at 16,384 characters by default. The adapter cannot recover content that your instrumentor did not record, and opt-in content can contain sensitive data.

The policy applies to this exporter only. It does not redact Temporal history, application logs, or other exporters you attach. It does not propagate user identity across Temporal's process boundary: attach explicit user IDs in activities if needed. Application-supplied `session.id` takes precedence over the workflow ID.

## Configuration and lifecycle

- `project_name`, `api_key`, `secret_key`, `base_url` override the corresponding `FI_*` environment values. `base_url` is the origin (optionally with a deployment prefix), not the traces URL; the package appends `/tracer/v1/traces`.
- The destination uses upstream OTLP/HTTP **protobuf** with no compression. Set `compression="gzip"` for Future AGI Cloud or self-hosted v1.56.0 and later. Remote endpoints require HTTPS; plain HTTP is allowed only for loopback collectors.
- `exporter=` accepts an application-supplied OpenTelemetry `SpanExporter` in place of endpoint/auth settings. It still receives filtered spans and is shut down by the handle.
- The batch queue holds at most 2,048 spans, with batches of at most 512 and a 1-second schedule. Set `max_queue_size`, `max_export_batch_size`, and `schedule_delay_millis` to tune them. A full queue drops spans rather than blocking an activity.
- `export_timeout` defaults to 3 seconds per HTTP export. `telemetry.force_flush(timeout_millis=3000)` and `telemetry.shutdown(timeout_millis=3000)` bound the caller's wait and return a boolean. On timeout, an in-flight daemon export can finish later; delivery is not guaranteed. Neither method ends unfinished workflow spans.
- Normal interpreter exit has a bounded shutdown fallback. Explicitly shut down after stopping the worker. Crashes, forced exits, and container freezes can lose buffered spans. In an async server, use `await asyncio.to_thread(telemetry.shutdown)` to avoid blocking its event loop while waiting.
- Do not configure again after shutdown: OpenTelemetry does not allow replacing its process-global provider. Construct worker processes after forking, then configure each process.

## Troubleshooting

**The worker rejects the global tracer provider.** Call this package's `configure()` before `Client.connect()`. Do not use the ordinary `register()` provider with Temporal's replay-safe plugin.

**Model spans start separate traces.** Configure the plugin on both clients and pass `telemetry.provider` to the model instrumentor. Keep calls inside activities.

**A workflow fails sandbox validation.** Move tracing setup and model imports into the worker/activity module. The workflow module should import only deterministic workflow dependencies and refer to activities by name.

**No spans appear, or the trace splits across projects.** Check both key headers, the base URL, and that starter and worker use the same `FI_PROJECT_NAME`. Flush short-lived starters before exit. A running workflow's `RunWorkflow` span is exported only when it ends.

## Limits

This integration covers the Python native plugin's workflow/activity hierarchy, activity retries, signal sessions, and history replay. It does not configure Temporal metrics/logs, cluster authentication, or model instrumentation. Native Temporal spans remain runtime spans rather than being relabeled as model calls. Each activity attempt is a distinct span; no attempt number is synthesized. Temporal owns deterministic IDs and replay suppression, not exactly-once delivery to Future AGI. Child workflows, continue-as-new, Nexus, and standalone activities are not additional compatibility guarantees of this package.
