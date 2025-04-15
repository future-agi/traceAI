"""
Requires a Cohere API request set with the `COHERE_API_KEY` environment variable.
"""

from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ProjectType,
)
from haystack import Document, Pipeline
from haystack_integrations.components.rankers.cohere import CohereRanker
from traceai_haystack import HaystackInstrumentor

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

# Initialize the Haystack instrumentor
HaystackInstrumentor().instrument(tracer_provider=trace_provider)

ranker = CohereRanker()
pipe = Pipeline()
pipe.add_component("ranker", ranker)
response = pipe.run(
    {
        "ranker": {
            "query": "Who won the World Cup in 2022?",
            "documents": [
                Document(
                    content="Paul Graham is the founder of Y Combinator.",
                ),
                Document(
                    content=(
                        "Lionel Messi, captain of the Argentinian national team, "
                        " won his first World Cup in 2022."
                    ),
                ),
                Document(
                    content="France lost the 2022 World Cup.",
                ),  # Cohere consistently ranks this document last
            ],
            "top_k": 2,
        }
    }
)
print(f"{response=}")
