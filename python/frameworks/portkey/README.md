## TraceAI Portkey Instrumentation 

This is a simple example of how to use TraceAI's Portkey Instrumentation.

### Requirements

- `portkey_ai`
- `traceai_portkey`

```python
pip install portkey_ai traceai_portkey 
```

Register a new project with the auto instrumentor with the Portkey client.

```python
from dotenv import load_dotenv
from portkey_ai import Portkey
from traceai_portkey import PortkeyInstrumentor
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType, EvalTag, EvalTagType, EvalSpanKind, EvalName, ModelChoices

# Load API keys from .env file
load_dotenv()

# --- Configure Future AGI Tracing Once ---
tracer_provider = register(
    project_name="My-AI-App",
    eval_tags=[
        EvalTag(
            type=EvalTagType.OBSERVATION_SPAN,
            value=EvalSpanKind.LLM,
            eval_name=EvalName.CONTEXT_ADHERENCE,
            custom_eval_name="Response_Quality"
        )
    ]
)

# Instrument the Portkey client
PortkeyInstrumentor().instrument(tracer_provider=tracer_provider)


# --- Your application logic remains the same! ---
client = Portkey(virtual_key="your-portkey-virtual-key")

completion = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Write a 6-word story about a robot who discovers music."}]
)

print(completion.choices[0].message.content)
```