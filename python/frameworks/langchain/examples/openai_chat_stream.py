from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ProjectType,
)
from fi_instrumentation.instrumentation.context_attributes import using_attributes
from langchain_openai import ChatOpenAI
from traceai_langchain import LangChainInstrumentor

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
    project_type=ProjectType.OBSERVE,
    project_name="LANGCHAIN_TEST_OBSERVE",
    project_version_name="V8",
    eval_tags=eval_tags,
)

# Initialize the LangChain instrumentor
LangChainInstrumentor().instrument(tracer_provider=trace_provider)


if __name__ == "__main__":
    with using_attributes(
        session_id="my-test-session",
        user_id="my-test-user",
        metadata={
            "test-int": 1,
            "test-str": "string",
            "test-list": [1, 2, 3],
            "test-dict": {
                "key-1": "val-1",
                "key-2": "val-2",
            },
        },
        tags=["tag-1", "tag-2"],
        prompt_template="Who won the soccer match in {city} on {date}",
        prompt_template_version="v1.0",
        prompt_template_variables={
            "city": "Johannesburg",
            "date": "July 11th",
        },
    ):
        for chunk in ChatOpenAI(model_name="gpt-3.5-turbo").stream("Write a haiku."):
            print(chunk.content, end="", flush=True)
