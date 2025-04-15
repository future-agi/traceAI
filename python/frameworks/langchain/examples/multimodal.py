import base64
from io import BytesIO

import httpx
from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ProjectType,
)
from langchain.chains import LLMChain
from langchain.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from PIL import Image
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
    project_type=ProjectType.EXPERIMENT,
    eval_tags=eval_tags,
    project_name="FUTURE_AGI",
    project_version_name="v1",
)

# Initialize the LangChain instrumentor
LangChainInstrumentor().instrument(tracer_provider=trace_provider)


# Function to fetch and encode image data
def fetch_and_encode_image(url):
    response = httpx.get(url)
    if response.status_code == 200:
        image_size_kb = len(response.content) / 1024  # Convert bytes to KB
        print(f"Image size for {url}: {image_size_kb:.2f} KB")
        return base64.b64encode(response.content).decode("utf-8")
    else:
        raise Exception(f"Failed to fetch image from {url}")


# URLs for the images
image_url1 = "https://upload.wikimedia.org/wikipedia/commons/thumb/d/dd/Gfp-wisconsin-madison-the-nature-boardwalk.jpg/2560px-Gfp-wisconsin-madison-the-nature-boardwalk.jpg"
image_url2 = "https://upload.wikimedia.org/wikipedia/commons/a/a7/Camponotus_flavomarginatus_ant.jpg"
# Fetch and encode images
# image_data1 = fetch_and_encode_image(image_url1)
# image_data2 = fetch_and_encode_image(image_url2)

# Initialize the model
model = ChatOpenAI(model="gpt-4o")

# Create a prompt template
prompt = ChatPromptTemplate.from_messages(
    [
        ("system", "Compare the two pictures provided."),
        (
            "user",
            [
                {
                    "type": "text",
                    "text": "Here are two images. Please analyze their differences and similarities.",
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"{image_url2}"},
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"{image_url1}"},
                },
            ],
        ),
    ]
)

# Create a chain with the model and prompt
chain = LLMChain(llm=model, prompt=prompt)

# Invoke the chain with the image data
response = chain.invoke({"image_url1": image_url1}, {"image_url2": image_url2})

# Print the response content
print(response["text"])
