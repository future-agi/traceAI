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
