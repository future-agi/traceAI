import anthropic
from anthropic import Anthropic
from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ProjectType,
)
from traceai_anthropic import AnthropicInstrumentor

# Configure trace provider with custom evaluation tags
eval_tags = [
    EvalTag(
        eval_name=EvalName.DETERMINISTIC_EVALS,
        value=EvalSpanKind.TOOL,
        type=EvalTagType.OBSERVATION_SPAN,
        config={
            "multi_choice": False,
            "choices": ["Yes", "No"],
            "rule_prompt": "Evaluate if the response is correct",
        },
        custom_eval_name="<custom_eval_name>",
    )
]

# Configure trace provider with custom evaluation tags
trace_provider = register(
    project_type=ProjectType.EXPERIMENT,
    eval_tags=eval_tags,
    project_name="FUTURE_AGI",
    project_version_name="v1",
)

# Initialize the Anthropic instrumentation
AnthropicInstrumentor().instrument(tracer_provider=trace_provider)

# Initialize Anthropic client
client = Anthropic()

prompt = (
    f"{anthropic.HUMAN_PROMPT}"
    f" how does a court case get to the Supreme Court?"
    f" {anthropic.AI_PROMPT}"
)

resp = client.completions.create(
    model="claude-2.1",
    prompt=prompt,
    max_tokens_to_sample=1000,
)

print(resp)
