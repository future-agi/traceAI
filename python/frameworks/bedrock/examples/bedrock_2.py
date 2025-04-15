import json
import os

import boto3
from botocore.exceptions import ClientError
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

trace_provider = register(
    project_type=ProjectType.EXPERIMENT,
    # eval_tags=eval_tags,
    project_name="FUTURE_AGI",
    project_version_name="v1",
)

# Configure trace provider with custom evaluation tags
trace_provider = register(
    project_type=ProjectType.EXPERIMENT,
    # eval_tags=eval_tags,
    project_name="FUTURE_AGI",
    project_version_name="v1",
)

# Instrument Bedrock client
BedrockInstrumentor().instrument(tracer_provider=trace_provider)

client = boto3.client(
    service_name="bedrock-runtime",
    region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
)
model_id = "mistral.mistral-large-2402-v1:0"


def converse_with_model(prompt, model_id=model_id):
    conversation = [
        {
            "role": "user",
            "content": [{"text": prompt}, {"text": prompt}],
        }
    ]

    try:
        response = client.converse(
            modelId=model_id,
            messages=conversation,
            inferenceConfig={"maxTokens": 512, "temperature": 0.5, "topP": 0.9},
        )
        response_text = response["output"]["message"]["content"][0]["text"]
        # print(response_text)
        return response_text
    except (ClientError, Exception) as e:
        print(f"ERROR: Can't invoke '{model_id}'. Reason: {e}")
        exit(1)


def converse_stream_with_model(prompt, model_id=model_id):
    conversation = [
        {
            "role": "user",
            "content": [{"text": prompt}, {"text": prompt}],
        }
    ]

    try:
        streaming_response = client.converse_stream(
            modelId=model_id,
            messages=conversation,
            inferenceConfig={"maxTokens": 512, "temperature": 0.5, "topP": 0.9},
        )

        response_text = ""
        for chunk in streaming_response["stream"]:
            if "contentBlockDelta" in chunk:
                text = chunk["contentBlockDelta"]["delta"]["text"]
                print(text, end="")
                response_text += text
        return response_text
    except (ClientError, Exception) as e:
        print(f"ERROR: Can't invoke '{model_id}'. Reason: {e}")
        exit(1)


def invoke_model_sync(prompt, model_id=model_id):
    formatted_prompt = f"{prompt}"
    native_request = {
        "prompt": formatted_prompt,
        "max_tokens": 512,
        "temperature": 0.5,
    }
    request = json.dumps(native_request)

    try:
        response = client.invoke_model(modelId=model_id, body=request)
        model_response = json.loads(response["body"].read())
        response_text = model_response["outputs"][0]["text"]
        print(response_text)
        return response_text
    except (ClientError, Exception) as e:
        print(f"ERROR: Can't invoke '{model_id}'. Reason: {e}")
        exit(1)


def invoke_model_stream(prompt, model_id=model_id):
    formatted_prompt = f"<s>[INST] {prompt} [/INST]"
    native_request = {
        "prompt": formatted_prompt,
        "max_tokens": 512,
        "temperature": 0.5,
    }
    # Convert dictionary to JSON string for span attributes
    request = json.dumps(native_request)

    try:
        streaming_response = client.invoke_model_with_response_stream(
            modelId=model_id,
            body=request,
            # _llm_invocation_params=json.dumps(native_request),
            # _input_value=formatted_prompt
        )

        response_text = ""
        for event in streaming_response["body"]:
            chunk = json.loads(event["chunk"]["bytes"])
            if "outputs" in chunk:
                text = chunk["outputs"][0].get("text", "")
                print(text, end="")
                response_text += text
        return response_text
    except (ClientError, Exception) as e:
        print(f"ERROR: Can't invoke '{model_id}'. Reason: {e}")
        exit(1)


if __name__ == "__main__":
    test_prompt = "Describe the purpose of a 'hello world' program in one line."
    converse_with_model(test_prompt)
    converse_stream_with_model(test_prompt)
    invoke_model_sync(test_prompt)
    invoke_model_stream(test_prompt)
