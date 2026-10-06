"""Trace Qwen Chat Completions through the official OpenAI SDK."""

import argparse
import os
import sys
from urllib.parse import unquote, urlsplit

import openai
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_openai import OpenAIInstrumentor

DEFAULT_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
API_KEY_ENV = "DASHSCOPE_API_KEY"
BASE_URL_ENV = "DASHSCOPE_BASE_URL"
MODEL_ENV = "DASHSCOPE_MODEL"


def check_base_url(url: str) -> str:
    if "{WorkspaceId}" in unquote(url):
        raise ValueError("Replace {WorkspaceId} with your workspace id from the console.")
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if (host == "aliyuncs.com" or host.endswith(".aliyuncs.com")) and not (
        parsed.path.removesuffix("/").endswith("/compatible-mode/v1")
    ):
        raise ValueError("The OpenAI SDK base must end with /compatible-mode/v1.")
    return url


def make_client(
    base_url: str | None = None, api_key: str | None = None, http_client=None
) -> openai.OpenAI:
    url = check_base_url(
        base_url if base_url is not None else os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL
    )
    key = api_key if api_key is not None else os.environ[API_KEY_ENV]
    return openai.OpenAI(base_url=url, api_key=key, http_client=http_client)


def setup_tracing(project_name: str | None = None):
    provider = register(
        project_name=project_name or "qwen-openai-recipe",
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
    parser.add_argument("--model", help=f"Model id; otherwise use {MODEL_ENV}.")
    args = parser.parse_args(argv)
    try:
        url = check_base_url(os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    model = args.model or os.environ.get(MODEL_ENV)
    if not model:
        parser.error(f"Set {MODEL_ENV} or pass --model.")

    provider = setup_tracing()
    try:
        with make_client(base_url=url) as client:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": args.prompt}],
                stream=args.stream,
                **({"stream_options": {"include_usage": True}} if args.stream else {}),
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
