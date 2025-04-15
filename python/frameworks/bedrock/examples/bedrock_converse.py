import json

import boto3
from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ProjectType,
)
from traceai_bedrock import BedrockInstrumentor

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

# Instrument Bedrock client
BedrockInstrumentor().instrument(tracer_provider=trace_provider)

client = boto3.client(
    service_name="****",
    region_name="****",
    aws_access_key_id="******",
    aws_secret_access_key="******",
)


def invoke_example():
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 2000,
        "messages": [{"role": "user", "content": "Hello there, how are you?"}],
    }

    response = client.invoke_model(
        modelId="model_id",
        body=json.dumps(body),
    )

    response_body = json.loads(response.get("body").read())
    print(response_body["content"][0]["text"])


def converse_example():
    system_prompt = [{"text": "You are an expert at creating music playlists"}]
    inital_message = {
        "role": "user",
        "content": [{"text": "Create a list of 3 pop songs."}],
    }
    inference_config = {"maxTokens": 1024, "temperature": 0.0}
    messages = []

    messages.append(inital_message)
    response = client.converse(
        modelId="model_id",
        system=system_prompt,
        messages=messages,
        inferenceConfig=inference_config,
    )
    out = response["output"]["message"]
    messages.append(out)
    print(out.get("content")[-1].get("text"))


if __name__ == "__main__":
    invoke_example()
    converse_example()
