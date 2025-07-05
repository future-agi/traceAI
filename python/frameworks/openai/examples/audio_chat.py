from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    ProjectType,
)
from traceai_openai import OpenAIInstrumentor

# Configure trace provider with custom evaluation tags
eval_tags = []

# Configure trace provider with custom evaluation tags
trace_provider = register(
    project_type=ProjectType.EXPERIMENT,
    eval_tags=eval_tags,
    project_name="FUTURE_AGI",
    project_version_name="v1",
)

# Initialize the OpenAI instrumentor
OpenAIInstrumentor().instrument(tracer_provider=trace_provider)

import base64

import requests
from openai import OpenAI

client = OpenAI()

# Fetch the audio file and convert it to a base64 encoded string
url = "https://cdn.openai.com/API/docs/audio/alloy.wav"
response = requests.get(url)
response.raise_for_status()
wav_data = response.content
encoded_string = base64.b64encode(wav_data).decode("utf-8")

completion = client.chat.completions.create(
    model="gpt-4o-audio-preview",
    modalities=["text", "audio"],
    audio={"voice": "alloy", "format": "wav"},
    messages=[
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is in this recording?"},
                {
                    "type": "input_audio",
                    "input_audio": {"data": encoded_string, "format": "wav"},
                },
            ],
        },
    ],
)

# print(completion.choices[0].message)
