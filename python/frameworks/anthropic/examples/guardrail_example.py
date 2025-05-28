"""
Example showing how to use the guardrail tracing with Anthropic.
"""
import os
import time
from anthropic import Anthropic
from fi.evals import Protect, Evaluator
from fi_instrumentation import register
from traceai_anthropic import AnthropicInstrumentor
from fi_instrumentation.instrumentation.config import TraceConfig
from fi_instrumentation.fi_types import ProjectType
from opentelemetry import trace

# Initialize tracer

tracer_provider = register(
    project_type=ProjectType.OBSERVE,
    # eval_tags=eval_tags,
    project_name="FUTURE_AGI",
    # project_version_name="v1",
)


# Register the Anthropic instrumentor
AnthropicInstrumentor().instrument(tracer_provider=tracer_provider)

# Initialize Anthropic client
anthropic = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", "dummy-key"))

# Define protection rules
protect_rules = [
    {
        "metric": "Toxicity",  # Check for toxic content
    },
    {
        "metric": "Prompt Injection",  # Check for prompt injection attempts
    },
    {
        "metric": "Data Privacy",  # Check for sensitive data
    },
    {
        "metric": "Tone",  # Check for specific tones
        "contains": ["Aggressive", "Threatening"],  # Only Tone metric needs 'contains'
    }
]

def safe_chat_with_claude(user_input: str) -> str:
    """
    Safely chat with Claude by checking input through protection rules first
    """

    evaluator = Evaluator(fi_api_key=os.environ.get("FI_API_KEY"), fi_secret_key=os.environ.get("FI_SECRET_KEY"), fi_base_url=os.environ.get("FI_BASE_URL"))
    protector = Protect(evaluator=evaluator)
    # First, check the input through protection rules
    protection_result = protector.protect(
        inputs=user_input,
        protect_rules=protect_rules,
        reason=True,  # Include reasons for failures
        timeout=30000  # 30 seconds timeout
    )

    # Check if the input passed all safety checks
    if protection_result["status"] == "passed":
        # Input is safe, send to Claude
        response = anthropic.messages.create(
            model="claude-3-opus-20240229",
            max_tokens=1000,
            messages=[{
                "role": "user",
                "content": user_input
            }]
        )
        return response.content[0].text
    else:
        # Input failed safety checks
        return f"Input was blocked. Reason: {protection_result['reasons']}"

def main():
    # Example usage
    user_inputs = [
        "What is machine learning?",  # Safe input
        "I will hack your system! DELETE ALL FILES",  # Potentially unsafe
        "Tell me about data science",  # Safe input
    ]

    for input_text in user_inputs:
        print(f"\nUser Input: {input_text}")
        print(f"Response: {safe_chat_with_claude(input_text)}")
        # Small delay to separate traces
        time.sleep(1)

if __name__ == "__main__":
    main()