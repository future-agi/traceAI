# Context Managers

Context managers let you add metadata and control tracing behavior for specific code blocks.

## Overview

Import from `fi_instrumentation`:

```python
from fi_instrumentation import (
    using_attributes,
    using_session,
    using_user,
    using_metadata,
    using_tags,
    using_prompt_template,
    suppress_tracing,
)
```

## using_attributes()

The combined context manager for all attributes:

```python
from fi_instrumentation import using_attributes

with using_attributes(
    session_id="session-123",
    user_id="user-456",
    metadata={"feature": "chat", "version": "1.0"},
    tags=["production", "chatbot"],
    prompt_template="You are a {role}. {task}",
    prompt_template_version="v2.0",
    prompt_template_variables={"role": "assistant", "task": "Help users"}
):
    response = client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": "Hello!"}]
    )
```

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | str | Session identifier |
| `user_id` | str | User identifier |
| `metadata` | dict | Key-value metadata |
| `tags` | list[str] | Categorical tags |
| `prompt_template` | str | Prompt template string |
| `prompt_template_version` | str | Template version |
| `prompt_template_variables` | dict | Template variables |

## Individual Context Managers

### using_session()

Track conversation sessions:

```python
from fi_instrumentation import using_session

with using_session(session_id="conversation-abc"):
    # All spans in this block share the session
    response1 = client.chat.completions.create(...)
    response2 = client.chat.completions.create(...)
```

### using_user()

Track user identity:

```python
from fi_instrumentation import using_user

with using_user(user_id="user-123"):
    # Associate spans with this user
    response = client.chat.completions.create(...)
```

### using_metadata()

Add custom key-value data:

```python
from fi_instrumentation import using_metadata

with using_metadata({
    "environment": "production",
    "feature_flag": "new_model",
    "experiment_id": "exp-001",
    "customer_tier": "enterprise"
}):
    response = client.chat.completions.create(...)
```

### using_tags()

Add categorical labels:

```python
from fi_instrumentation import using_tags

with using_tags(["chatbot", "support", "english"]):
    response = client.chat.completions.create(...)
```

### using_prompt_template()

Track prompt engineering:

```python
from fi_instrumentation import using_prompt_template

template = """You are a {role} assistant.

Your task: {task}

User question: {question}"""

with using_prompt_template(
    template=template,
    version="v2.1",
    variables={
        "role": "helpful",
        "task": "Answer questions accurately",
        "question": user_input
    }
):
    response = client.chat.completions.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": template.format(
                role="helpful",
                task="Answer questions accurately",
                question=user_input
            )},
            {"role": "user", "content": user_input}
        ]
    )
```

## Skills and agent contexts

A runtime **load** means verified skill bytes were given to application code
inside a trace. It does not mean a model followed the skill. Fetched, linked,
installed and loaded are different facts; no loads recorded does not mean unused.

### With the Future AGI SDK

For SDK versions that provide `fi.telemetry`, high-level skill/context snapshot
reads emit `skill.load` / `context.load`. The first read of any skill file records
the load; a context `read_all()` records only the context, not its linked skills.
The SDK records evidence (`delivered`, `agent_read` or `declared`) separately.

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from fi.telemetry import agent_scope

provider = register(project_name="support", project_type=ProjectType.OBSERVE,
                    skills_processor=True)
tracer = provider.get_tracer(__name__)
try:
    # ctx_snapshot is an already resolved, verified agent-context snapshot.
    with agent_scope(agent="refund-agent", context=ctx_snapshot):
        with tracer.start_as_current_span("request"):
            run_agent()
finally:
    provider.shutdown()
```

`skills_processor=True` is an **opt-in** to early installation of the SDK's
processor, not a new exporter. Without it, the SDK can self-install when a load
occurs. Both installers share a lock and the `_futureagi_skills_processor` marker.
`register()` defaults to non-global; the SDK discovers it through
`fi_instrumentation.otel.get_registered_tracer_providers()`. That function returns
an immutable snapshot ordered oldest to newest (most recently registered last).
The registry uses weak references. Existing provider exit hooks still apply.

traceAI does not require the SDK: absent or older SDKs leave ordinary tracing
unchanged even if early installation was requested. traceAI ships no Python
skills processor of its own. `register()` still does **not** set `service.name`.
Name each agent with `agent_scope(agent=...)`; a service name must come from the
application's resource configuration. With neither, several agents in one project
may appear as one unnamed agent.

The SDK's `FI_SKILLS_TRACING=off` disables loads and stamping; `load_only` records
load spans without stamping. `fi.telemetry.configure(skills_tracing=...)` takes
precedence over that environment variable. The optional early installer honors
both controls.

### Low-level carrier, without the SDK

```python
from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.instrumentation.runtime_attributes import (
    RUNTIME_ATTRIBUTES_CONTEXT_KEY,
    using_runtime_attributes,
)
from opentelemetry.context import get_value

# provider is your existing tracer provider. No SDK import is needed here.
# FITracer applies TraceConfig masking (FI_HIDE_INPUTS, FI_PII_REDACTION, ...)
# to every attribute written through its spans, including copied carrier values.
tracer = FITracer(provider.get_tracer(__name__), config=TraceConfig())
with using_runtime_attributes({"gen_ai.agent.name": "refund-agent"}):
    with tracer.start_as_current_span("request") as span:
        # Explicit copying for a provider without a compatible processor.
        span.set_attributes(get_value(RUNTIME_ATTRIBUTES_CONTEXT_KEY)["attrs"])
```

Copy carrier values only through an `FITracer` span, as above. A plain
OpenTelemetry tracer's span does not apply `TraceConfig`, so hidden inputs or PII
in the carrier would be written unmasked.

The helper attaches an immutable carrier under the **literal** OTel Context key
`futureagi.runtime.span_attributes.v1`, with shape
`{v: 1, attrs: {...}, reset_skills: bool}`. A compatible span processor can read it
from `parent_context` and copy attributes only when the span has no explicit
value. The helper itself emits **no spans**, installs nothing and does not prove
that a skill was loaded. Without a compatible processor, subsequent spans need
explicit attribute copying as in the example.

Only `fi.skills.*`, `fi.context.*` and `gen_ai.agent.*` keys are accepted. Scalar
`fi.skill.*` identities belong only on `skill.load`, and
`fi.context.linked_skill_version_ids` only on `context.load`; neither propagates
through this helper. Never put contents, local paths, credentials or personal
data in these values. Use canonical lowercase UUIDs and `sha256:<64 hex>` digests.
The helper copies flat values, drops invalid or over-256-byte strings rather than
rewriting identities, caps skill lists at 32 (setting count/truncation), and
keeps at most 64 attributes.

Ordinary nested blocks inherit attributes; supplied keys replace their values.
This is a snapshot interface: supply **complete**, de-duplicated, first-load-order
parallel lists (`fi.skills.version_ids`, `fi.skills.ids`, `fi.skills.refs`), not
list deltas. The SDK's higher-level scope helper accumulates loads for you.
Supplying a new context replaces all inherited context and skill keys;
`reset_skills=True` explicitly clears just the skill keys. Exiting the block
restores the previous OTel context, including on an exception or cancellation.
Use a fresh `with` block inside async functions and generator bodies. Thread-pool
handoffs require `contextvars.copy_context().run(...)`; subprocesses require
explicit propagation and cannot inherit this in-process carrier.

### What later-span stamping cannot cover

- Already-started ancestor spans cannot be retroactively changed. Put the explicit
  scope **outside** root span creation if the root needs attribution.
- Head-sampled-out spans and providers without the processor are not stamped.
- In automatic mode, interleaved agents share a trace's skill list. Use explicit
  SDK agent scopes for separation; a nested context starts a fresh list.
- Explicit span attributes, including a framework's `gen_ai.agent.name`, win.
- Trace-level load queries use `skill.load` evidence, not root attributes.

Java, C#, Go and Ruby: **manual — set attributes on your spans**. Automatic SDK
load attribution and the carrier above are not implemented for those languages.

## suppress_tracing()

Temporarily disable tracing:

```python
from fi_instrumentation import suppress_tracing

# Normal tracing
response1 = client.chat.completions.create(...)  # Traced

with suppress_tracing():
    # No tracing in this block
    response2 = client.chat.completions.create(...)  # Not traced

# Tracing resumes
response3 = client.chat.completions.create(...)  # Traced
```

Use cases:
- Skip tracing for health checks
- Avoid tracing internal/debug calls
- Reduce trace volume for high-frequency operations

## Nesting Context Managers

Context managers can be nested:

```python
with using_session(session_id="session-123"):
    with using_user(user_id="user-456"):
        with using_tags(["production"]):
            # All attributes are combined
            response = client.chat.completions.create(...)
```

Or use multiple in one `with` statement:

```python
with (
    using_session(session_id="session-123"),
    using_user(user_id="user-456"),
    using_tags(["production"])
):
    response = client.chat.completions.create(...)
```

## Async Support

All context managers work with async code:

```python
import asyncio
from openai import AsyncOpenAI

async def chat():
    client = AsyncOpenAI()

    with using_attributes(session_id="async-session"):
        response = await client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": "Hello!"}]
        )

asyncio.run(chat())
```

## Framework Integration

### FastAPI

```python
from fastapi import FastAPI, Request
from fi_instrumentation import using_attributes

app = FastAPI()

@app.post("/chat")
async def chat(request: Request):
    session_id = request.headers.get("X-Session-ID", "unknown")
    user_id = request.headers.get("X-User-ID", "anonymous")

    with using_attributes(
        session_id=session_id,
        user_id=user_id,
        metadata={"endpoint": "/chat"}
    ):
        response = await client.chat.completions.create(...)
        return {"response": response.choices[0].message.content}
```

### Flask

```python
from flask import Flask, request
from fi_instrumentation import using_attributes

app = Flask(__name__)

@app.route("/chat", methods=["POST"])
def chat():
    with using_attributes(
        session_id=request.headers.get("X-Session-ID"),
        user_id=request.headers.get("X-User-ID"),
    ):
        response = client.chat.completions.create(...)
        return {"response": response.choices[0].message.content}
```

### Django

```python
from django.http import JsonResponse
from fi_instrumentation import using_attributes

def chat_view(request):
    with using_attributes(
        session_id=request.session.session_key,
        user_id=str(request.user.id) if request.user.is_authenticated else None,
    ):
        response = client.chat.completions.create(...)
        return JsonResponse({"response": response.choices[0].message.content})
```

## Best Practices

### 1. Set Session Early

```python
# At request start
with using_session(session_id=request_id):
    # All operations share the session
    handle_request()
```

### 2. Use Meaningful Tags

```python
# Good - specific and filterable
tags = ["production", "chatbot", "english", "enterprise"]

# Avoid - too generic
tags = ["request", "llm"]
```

### 3. Structure Metadata

```python
# Good - organized and typed
metadata = {
    "request_id": "req-123",
    "feature": "summarization",
    "model_version": "v2",
    "user_tier": "premium"
}

# Avoid - flat and unstructured
metadata = {"data": "some value"}
```

### 4. Track Experiments

```python
with using_metadata({
    "experiment_id": "exp-001",
    "variant": "A",
    "hypothesis": "shorter_prompts"
}):
    response = client.chat.completions.create(...)
```

## Accessing Context Values

Get current context values:

```python
from fi_instrumentation.instrumentation.context_attributes import (
    get_attributes_from_context
)

# Inside a traced function
attrs = get_attributes_from_context()
session_id = attrs.get("session_id")
user_id = attrs.get("user_id")
```

## Related

- [Core Concepts](core-concepts.md) - Understanding traceAI
- [TraceConfig](../configuration/trace-config.md) - Privacy settings
