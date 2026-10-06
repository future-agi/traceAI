"""Trace Azure OpenAI v1 Chat Completions with the OpenAI client."""

import argparse
import os
import sys
from urllib.parse import unquote, urlsplit

import openai
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_openai import OpenAIInstrumentor

DEFAULT_BASE_URL = None
DOCUMENTED_BASE_URL_FORMS = (
    "https://YOUR-RESOURCE-NAME.openai.azure.com/openai/v1/",
    "https://YOUR-RESOURCE-NAME.services.ai.azure.com/openai/v1/",
)
API_KEY_ENV = "AZURE_OPENAI_API_KEY"
BASE_URL_ENV = "AZURE_OPENAI_BASE_URL"
MODEL_ENV = "AZURE_OPENAI_DEPLOYMENT"


def check_base_url(url: str) -> str:
    if not url:
        raise ValueError(f"Set {BASE_URL_ENV} to your resource's /openai/v1/ base URL.")
    decoded = url
    while (next_value := unquote(decoded)) != decoded:
        decoded = next_value
    if "your-resource-name" in decoded.lower() or "<" in decoded or ">" in decoded:
        raise ValueError(f"Replace placeholders in {BASE_URL_ENV} with your resource name.")
    try:
        parsed = urlsplit(decoded)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        raise ValueError(f"Set {BASE_URL_ENV} to a valid HTTP(S) base URL.") from None
    if parsed.scheme not in ("http", "https") or not host:
        raise ValueError(f"Set {BASE_URL_ENV} to a valid HTTP(S) base URL.")
    if host.endswith((".openai.azure.com", ".services.ai.azure.com", ".cognitiveservices.azure.com")):
        if parsed.path not in ("/openai/v1", "/openai/v1/"):
            if parsed.path.startswith("/api/projects"):
                reason = "The project endpoint is for agents, not model inference."
            elif parsed.path.startswith("/openai"):
                reason = "Older AzureOpenAI deployment/api-version URLs are not covered."
            elif parsed.path.startswith("/models"):
                reason = "The azure-ai-inference SDK /models endpoint is not covered."
            else:
                reason = "A bare resource root or other Azure path is not an inference base URL."
            raise ValueError(f"{reason} Set {BASE_URL_ENV} to your resource's /openai/v1/ URL.")
    return url


def make_client(base_url: str | None = None, api_key: str | None = None, http_client=None) -> openai.OpenAI:
    resolved_url = check_base_url(base_url if base_url is not None else os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL)
    key = api_key if api_key is not None else os.environ[API_KEY_ENV]
    return openai.OpenAI(base_url=resolved_url, api_key=key, http_client=http_client)


def setup_tracing(project_name: str | None = None):
    provider = register(
        project_name=project_name or "azure-ai-foundry",
        project_type=ProjectType.OBSERVE,
        set_global_tracer_provider=False,
        verbose=False,
    )
    OpenAIInstrumentor().instrument(tracer_provider=provider)
    return provider


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--prompt", default="What is a rainbow?")
    parser.add_argument("--model", help="Your Azure model deployment name")
    args = parser.parse_args(argv)
    try:
        base_url = check_base_url(os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL)
        model = args.model or os.environ[MODEL_ENV]
        api_key = os.environ[API_KEY_ENV]
        if not model:
            raise ValueError(f"Set {MODEL_ENV} to your deployment name or use --model.")
        if not api_key:
            raise ValueError(f"Set {API_KEY_ENV} to your Azure API key.")
    except KeyError as error:
        print(f"Set {error.args[0]} before running this example.", file=sys.stderr)
        return 2
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    provider = setup_tracing()
    try:
        with make_client(base_url=base_url, api_key=api_key) as client:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": args.prompt}],
                stream=args.stream,
            )
            if args.stream:
                for chunk in response:
                    if chunk.choices:
                        print(chunk.choices[0].delta.content or "", end="", flush=True)
                print()
            else:
                print(response.choices[0].message.content or "")
    finally:
        provider.force_flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
