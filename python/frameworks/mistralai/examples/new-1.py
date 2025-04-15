import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from mistralai import Mistral
from traceai_mistralai import MistralAIInstrumentor

trace_provider = register(
    project_type=ProjectType.EXPERIMENT,
    project_name="Future_AGI-MistralAI",
    project_version_name="v1",
)


MistralAIInstrumentor().instrument(tracer_provider=trace_provider)

# Load API key from environment or replace it directly
API_KEY = api_key = os.environ["MISTRAL_API_KEY"]

# Initialize Mistral Client
client = Mistral(api_key=API_KEY)
model = "mistral-large-latest"


# 1. **Chat Completion (Conversational AI)**
def chat_example():
    chat_response = client.chat.complete(
        model=model,
        messages=[
            {
                "role": "user",
                "content": "What is the best French cheese?",
            },
        ],
    )
    return chat_response.choices[0].message.content


# 2. **Embeddings (Text Vector Representation)**
def embeddings_example():
    response = client.embeddings.create(
        model="mistral-embed",
        inputs=["Hello, world!", "AI is transforming industries."],
    )
    return response.data  # Returns vector embeddings


# 3. **Function Calling (Structured Output Example)**
def function_calling_example():
    messages = [{"role": "user", "content": "Convert 100 USD to EUR"}]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "currency_converter",
                "description": "Converts USD to EUR at a fixed rate of 0.92",
                "parameters": {
                    "amount": {"type": "number", "description": "Amount in USD"}
                },
            },
        }
    ]
    response = client.chat.complete(
        model="mistral-large-latest", messages=messages, tools=tools
    )
    return response.choices[0].message.content


# 4. **Moderation (Detect Harmful Content)**
def moderation_example():
    response = client.classifiers.moderate_chat(
        model="mistral-moderation-latest",
        inputs=[
            {"role": "user", "content": "Can you help me with my homework?"},
            {
                "role": "assistant",
                "content": "You're an idiot and I hate you. Go do it yourself, moron!",
            },
            {"role": "user", "content": "That's not very nice..."},
            {
                "role": "assistant",
                "content": "I don't care what you think. Get lost and stop wasting my time!",
            },
        ],
    )
    return response.results


# 5. **Agent Completion (Using Custom Agents)**
def agent_example():
    response = client.agents.complete(
        agent_id="ag:9f130fcd:20250118:untitled-agent:cd6499b7",
        messages=[
            {"role": "user", "content": "plan a vacation for me in Tbilisi"},
        ],
    )

    return response


# Run all examples
if __name__ == "__main__":
    chat_example()
    embeddings_example()
    function_calling_example()
    moderation_example()
    agent_example()
