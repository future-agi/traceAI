# Instructor tracing

Trace structured-output requests in Future AGI with `InstructorInstrumentor`.

## Install

Use Python 3.10–3.13. For per-call validation and usage hooks, use Instructor 1.17 or later.

```bash
pip install traceAI-instructor traceAI-openai 'instructor>=1.17' openai
```

Set `FI_API_KEY`, `FI_SECRET_KEY`, and `OPENAI_API_KEY` in your application environment or secret manager.

## Trace an extraction

```python
import instructor
from openai import OpenAI
from pydantic import BaseModel

from fi_instrumentation import TraceConfig, register
from fi_instrumentation.fi_types import ProjectType
from traceai_instructor import InstructorInstrumentor
from traceai_openai import OpenAIInstrumentor

provider = register(project_type=ProjectType.OBSERVE, project_name="Instructor")
privacy = TraceConfig(hide_inputs=True, hide_outputs=True)
OpenAIInstrumentor().instrument(tracer_provider=provider, config=privacy)
InstructorInstrumentor().instrument(tracer_provider=provider, config=privacy)

class Person(BaseModel):
    name: str
    age: int

with OpenAI() as openai_client:
    client = instructor.from_openai(openai_client)
    person = client.create(
        model="gpt-4o-mini",
        response_model=Person,
        messages=[{"role": "user", "content": "Ada is 37 years old."}],
    )
    print(person.model_dump())

provider.force_flush()
```

Open the **Instructor** project in Future AGI. Each extraction is a `CHAIN` span; the OpenAI integration supplies its model-call children. The example hides prompts, structured results, and error details. Use `TraceConfig()` on both instrumentors only when your data policy permits content capture.

## Supported request paths

- Synchronous and asynchronous `create`, `create_with_completion`, `create_partial`, and `create_iterable`.
- Legacy `instructor.patch(client=...)` and `instructor.patch(create=...)`, including generated v2 callables. Instrument before constructing patched clients. Client construction does not emit spans or serialize SDK clients.
- Cache hits still emit request spans without inventing provider usage. Provider spans and extraction usage describe the same tokens; do not sum both levels.
- Where per-call hooks are supported, attempt and validation-error counts are recorded without registering listeners on shared clients. Existing application hooks keep running.
- Lazy streams finish on exhaustion, error, or explicit `close()` / `aclose()`. Consume or close them before flushing. Their output is the last parsed item, with `instructor.stream.chunks` recording the item count; telemetry does not accumulate the stream.

The SDK's own return values and exceptions are preserved. Instructor 1.0's partial-stream implementation is incompatible with the Pydantic versions required by this package; upgrade Instructor to use partial streaming. Eager SDK results remain eager.

See the [Instructor integration guide](https://docs.futureagi.com/docs/integrations/traceai/instructor) for setup details.
