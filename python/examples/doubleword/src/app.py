"""Trace Doubleword Chat Completions with the existing OpenAI instrumentor."""

import argparse
import ipaddress
import os
import re
import sys
from urllib.parse import urlsplit

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from openai import OpenAI
from traceai_openai import OpenAIInstrumentor

DEFAULT_BASE_URL = "https://api.doubleword.ai/v1"
API_KEY_ENV = "DOUBLEWORD_API_KEY"
BASE_URL_ENV = "DOUBLEWORD_BASE_URL"
MODEL_ENV = "DOUBLEWORD_MODEL"


def check_base_url(url: str) -> str:
    """Validate without rewriting; permit customer proxies and loopback servers."""
    def refuse(reason):
        raise ValueError(f"{BASE_URL_ENV}: {reason}")

    if not url or any(c.isspace() or not c.isprintable() for c in url):
        refuse("use an absolute URL without whitespace or control characters")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # Reject malformed ports before tracing.
    except ValueError:
        refuse("use a valid absolute HTTP(S) URL")
    if parts.username is not None or parts.password is not None:
        refuse("remove URL credentials; supply the key through DOUBLEWORD_API_KEY")
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
    if comparable_host == "api.doubleword.ai":
        if parts.scheme != "https":
            refuse(f"use HTTPS: {DEFAULT_BASE_URL}")
        if "?" in url or "#" in url:
            refuse(f"remove the query or fragment; use {DEFAULT_BASE_URL}")
        if parts.path.startswith(("/batch", "/v1/batch")):
            refuse(f"the Batch API is not covered; use {DEFAULT_BASE_URL}")
        if parts.path.startswith(("/messages", "/v1/messages")):
            refuse(f"the Anthropic Messages API is not covered; use {DEFAULT_BASE_URL}")
        if parts.path not in ("/v1", "/v1/"):
            refuse(f"use the base URL {DEFAULT_BASE_URL}, without an endpoint path")
    return url


def make_client(base_url: str | None = None, api_key: str | None = None, http_client=None) -> OpenAI:
    base_url = check_base_url(
        base_url if base_url is not None else os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL
    )
    return OpenAI(
        base_url=base_url,
        api_key=api_key if api_key is not None else os.environ[API_KEY_ENV],
        http_client=http_client,
    )


def setup_tracing(project_name: str | None = None):
    provider = register(
        project_name=project_name or "doubleword-example",
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
    parser.add_argument("--model")
    args = parser.parse_args(argv)
    try:
        base_url = check_base_url(os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL)
        model = args.model or os.environ.get(MODEL_ENV)
        if not model:
            raise ValueError(f"{MODEL_ENV}: set a catalog model id or pass --model")
        api_key = os.environ.get(API_KEY_ENV)
        if not api_key:
            raise ValueError(f"{API_KEY_ENV}: set your Doubleword API key")
    except ValueError as error:
        print(error, file=sys.stderr)
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
