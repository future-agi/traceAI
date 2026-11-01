"""Trace Qwen Chat Completions through the official OpenAI SDK."""

import argparse
import ipaddress
import os
import re
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


def _fully_unquoted(value: str) -> str:
    while (decoded := unquote(value)) != value:
        value = decoded
    return value


def check_base_url(url: str) -> str:
    """Validate without rewriting; permit customer proxies and loopback servers."""
    def refuse(reason):
        raise ValueError(f"{BASE_URL_ENV}: {reason}")

    if not url or any(c.isspace() or not c.isprintable() for c in url):
        refuse("use an absolute URL without whitespace or control characters")
    if "{WorkspaceId}" in _fully_unquoted(url):
        refuse("Replace {WorkspaceId} with your workspace id from the console.")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # Reject malformed ports before tracing.
    except ValueError:
        refuse("use a valid absolute HTTP(S) URL")
    if parts.username is not None or parts.password is not None:
        refuse("remove URL credentials; supply the key through DASHSCOPE_API_KEY")
    if parts.scheme not in ("http", "https") or not host:
        refuse("use an absolute HTTP(S) URL with a host")
    if not host.isascii():
        refuse("use a plain ASCII host with valid IDNA spelling")
    comparable_host = host.lower().rstrip(".")
    structural_host = host.lower().removesuffix(".")
    try:
        ipaddress.ip_address(structural_host)
    except ValueError:
        try:
            structural_host.encode("ascii").decode("idna")
        except UnicodeError:
            refuse("use a plain ASCII host with valid IDNA spelling")
        if len(structural_host) > 253 or not all(
            re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9_-]{0,61}[a-zA-Z0-9])?", label)
            for label in structural_host.split(".")
        ):
            refuse("use a valid ASCII host")
    if comparable_host == "aliyuncs.com" or comparable_host.endswith(".aliyuncs.com"):
        if parts.scheme != "https":
            refuse("use HTTPS for the Model Studio endpoint")
        if "?" in url or "#" in url:
            refuse("remove the query or fragment from the SDK base URL")
        if parts.path not in ("/compatible-mode/v1", "/compatible-mode/v1/"):
            refuse("use exactly /compatible-mode/v1 with at most one trailing slash")
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
        project_name=project_name or os.environ.get("FI_PROJECT_NAME") or "qwen-openai-recipe",
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
    if not os.environ.get(API_KEY_ENV):
        parser.error(f"Set {API_KEY_ENV}.")

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
                with response:
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
