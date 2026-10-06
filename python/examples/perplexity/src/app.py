"""Trace Perplexity's Agent API through the OpenAI Responses interface."""

import argparse
import os
import sys
from urllib.parse import urlsplit

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from openai import OpenAI
from traceai_openai import OpenAIInstrumentor

DEFAULT_BASE_URL = "https://api.perplexity.ai/v1"
API_KEY_ENV = "PERPLEXITY_API_KEY"
BASE_URL_ENV = "PERPLEXITY_BASE_URL"
MODEL_ENV = "PERPLEXITY_MODEL"


def check_base_url(url: str) -> str:
    """Reject documented endpoint mix-ups without changing an allowed URL."""
    parsed = urlsplit(url)
    if (parsed.hostname or "").lower().rstrip(".") == "api.perplexity.ai":
        path = parsed.path.rstrip("/")
        if not path:
            raise ValueError(
                "That root was the Sonar Chat Completions base URL, whose support "
                "ended on 27 September 2026; use https://api.perplexity.ai/v1 "
                "with client.responses.create."
            )
        if path in ("/v1/sonar", "/v1/agent"):
            raise ValueError(
                "Endpoint paths are not SDK base URLs: the SDK appends /responses; "
                "use https://api.perplexity.ai/v1 with client.responses.create."
            )
        if path in ("/router", "/router/v1"):
            raise ValueError(
                "The Router API is a separate private-preview product, not covered "
                "here; use https://api.perplexity.ai/v1 with client.responses.create."
            )
    return url


def make_client(
    base_url: str | None = None, api_key: str | None = None, http_client=None
) -> OpenAI:
    if base_url is None:
        base_url = os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL
    return OpenAI(
        base_url=check_base_url(base_url),
        api_key=os.environ[API_KEY_ENV] if api_key is None else api_key,
        http_client=http_client,
    )


def setup_tracing(project_name: str | None = None):
    provider = register(
        project_name=project_name or "perplexity-agent-api",
        project_type=ProjectType.OBSERVE,
        set_global_tracer_provider=False,
        verbose=False,
    )
    OpenAIInstrumentor().instrument(tracer_provider=provider)
    return provider


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--prompt", default="What is a solar eclipse?")
    parser.add_argument("--model")
    args = parser.parse_args(argv)
    try:
        base_url = check_base_url(os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL)
        model = args.model or os.environ[MODEL_ENV]
        api_key = os.environ[API_KEY_ENV]
        if not model:
            raise ValueError(f"Set {MODEL_ENV} or pass --model.")
        if not api_key:
            raise ValueError(f"Set {API_KEY_ENV}.")
    except (KeyError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2

    provider = setup_tracing()
    try:
        with make_client(base_url=base_url, api_key=api_key) as client:
            if args.stream:
                stream = client.responses.create(model=model, input=args.prompt, stream=True)
                for event in stream:
                    if event.type == "response.output_text.delta":
                        print(event.delta, end="", flush=True)
                print()
            else:
                response = client.responses.create(model=model, input=args.prompt)
                print(response.output_text)
    finally:
        provider.force_flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
