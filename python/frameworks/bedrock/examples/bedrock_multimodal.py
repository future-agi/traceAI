import os
from typing import Tuple

import boto3
import requests
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

# Initialize the Bedrock instrumentor
BedrockInstrumentor().instrument(tracer_provider=trace_provider)

# Initialize Bedrock client
client = boto3.client(
    service_name="****",
    region_name="****",
    aws_access_key_id="******",
    aws_secret_access_key="******",
)


def multimodal_example():
    model_id = "model_id"
    input_text = "What's in this image?"

    img_url = "https://upload.wikimedia.org/wikipedia/commons/thumb/d/dd/Gfp-wisconsin-madison-the-nature-boardwalk.jpg/2560px-Gfp-wisconsin-madison-the-nature-boardwalk.jpg"
    img_url = "https://a1cf74336522e87f135f-2f21ace9a6cf0052456644b80fa06d4f.ssl.cf2.rackcdn.com/images/characters/large/800/Homer-Simpson.The-Simpsons.webp"
    img_bytes, format = download_img(img_url)

    message = {
        "role": "user",
        "content": [
            {
                "text": input_text,
            },
            {
                "image": {
                    "format": format,
                    "source": {
                        "bytes": img_bytes,
                    },
                }
            },
        ],
    }

    response = client.converse(
        modelId=model_id,
        messages=[message],
    )

    out = response["output"]["message"]
    print(out.get("content")[-1].get("text"))


def download_img(url: str) -> Tuple[bytes, str]:
    format = sanitize_format(os.path.splitext(url)[-1].lstrip("."))
    resp = requests.get(url)
    if resp.status_code != 200:
        raise ValueError(f"Error: Could not retrieve image from URL: {url}")
    return resp.content, format


def sanitize_format(fmt: str) -> str:
    if fmt == "jpg":
        return "jpeg"
    return fmt


if __name__ == "__main__":
    multimodal_example()
