import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ProjectType,
)
from mistralai import Mistral
from traceai_mistralai import MistralAIInstrumentor

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

# Initialize the Mistral AI instrumentor
MistralAIInstrumentor().instrument(tracer_provider=trace_provider)

if __name__ == "__main__":
    client = Mistral(api_key=os.environ["MISTRAL_API_KEY"])
    response = client.agents.complete(
        agent_id="ag:9f130fcd:20250118:untitled-agent:cd6499b7",
        messages=[
            {"role": "user", "content": "plan a vacation for me in Tbilisi"},
        ],
    )
    print(response)
