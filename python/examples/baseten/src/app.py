"""Trace Baseten Model APIs Chat Completions with the OpenAI SDK."""

import argparse
import os
import re
import sys
from urllib.parse import urlsplit

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from openai import OpenAI
from traceai_openai import OpenAIInstrumentor

DEFAULT_BASE_URL = "https://inference.baseten.co/v1"
API_KEY_ENV = "BASETEN_API_KEY"
BASE_URL_ENV = "BASETEN_BASE_URL"
MODEL_ENV = "BASETEN_MODEL"


def check_base_url(url: str) -> str:
    """Reject the two Baseten surfaces that need a different recipe."""
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if host == "inference.baseten.co" and parsed.path in ("", "/"):
        raise ValueError(
            "Baseten's root is the Anthropic Messages beta endpoint; "
            "use https://inference.baseten.co/v1 for the OpenAI SDK."
        )
    if re.fullmatch(r"model-[^.]+\.api\.baseten\.co", host):
        raise ValueError(
            "Baseten dedicated deployments use a per-deployment URL and are "
            "out of scope for this Model APIs recipe."
        )
    return url


def make_client(base_url: str | None = None, api_key: str | None = None,
                http_client=None) -> OpenAI:
    base_url = check_base_url(
        base_url if base_url is not None
        else os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL
    )
    api_key = api_key if api_key is not None else os.environ[API_KEY_ENV]
    return OpenAI(base_url=base_url, api_key=api_key, http_client=http_client)


def setup_tracing(project_name: str | None = None):
    provider = register(
        project_name=project_name or "baseten-openai",
        project_type=ProjectType.OBSERVE,
        set_global_tracer_provider=False,
        verbose=False,
    )
    OpenAIInstrumentor().instrument(tracer_provider=provider)
    return provider


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--prompt", default="What is the capital of France?")
    parser.add_argument("--model", help=f"Model slug; otherwise set {MODEL_ENV}")
    args = parser.parse_args(argv)
    try:
        base_url = check_base_url(os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL)
        model = args.model or os.environ[MODEL_ENV]
        api_key = os.environ[API_KEY_ENV]
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    except KeyError as error:
        print(f"Set {error.args[0]} before running this example.", file=sys.stderr)
        return 2

    provider = setup_tracing()
    try:
        with make_client(base_url=base_url, api_key=api_key) as client:
            response = client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": args.prompt}],
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
