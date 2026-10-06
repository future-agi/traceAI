"""Trace Anannas Chat Completions with the existing OpenAI instrumentor."""

import argparse
import os
import sys
from urllib.parse import urlsplit

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from openai import OpenAI
from traceai_openai import OpenAIInstrumentor

DEFAULT_BASE_URL = "https://api.anannas.ai/v1"
API_KEY_ENV = "ANANNAS_API_KEY"
BASE_URL_ENV = "ANANNAS_BASE_URL"
MODEL_ENV = "ANANNAS_MODEL"


def check_base_url(url: str) -> str:
    """Validate the base URL without changing the customer's spelling."""
    if any(char.isspace() or not char.isprintable() for char in url):
        raise ValueError(f"{BASE_URL_ENV} must contain no whitespace or control characters.")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # Check malformed ports before tracing starts.
    except ValueError:
        raise ValueError(f"{BASE_URL_ENV} must be a valid HTTP(S) base URL.") from None
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{BASE_URL_ENV} must omit URL credentials; use {API_KEY_ENV}.")
    if not host or parts.scheme not in ("http", "https"):
        raise ValueError(f"{BASE_URL_ENV} must be an absolute HTTP(S) base URL.")
    try:
        host.lower().encode("ascii").decode("idna")
    except UnicodeError:
        raise ValueError(f"{BASE_URL_ENV} must have a plain ASCII host with valid IDNA.") from None
    if host.lower().rstrip(".") in {"api.anannas.ai", "anannas.ai"}:
        if parts.scheme != "https":
            raise ValueError(f"{BASE_URL_ENV} requires HTTPS on Anannas hosts; use {DEFAULT_BASE_URL}.")
        if "?" in url or "#" in url:
            raise ValueError(f"{BASE_URL_ENV} must omit query and fragment delimiters; use {DEFAULT_BASE_URL}.")
        if parts.path not in ("/v1", "/v1/"):
            raise ValueError(f"{BASE_URL_ENV} must use the base path /v1 or /v1/; use {DEFAULT_BASE_URL}.")
    return url


def make_client(base_url: str | None = None, api_key: str | None = None, http_client=None) -> OpenAI:
    url = check_base_url(base_url if base_url is not None else os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL)
    key = os.environ[API_KEY_ENV] if api_key is None else api_key
    return OpenAI(base_url=url, api_key=key, http_client=http_client)


def setup_tracing(project_name: str | None = None):
    provider = register(
        project_name=project_name,
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
    parser.add_argument("--model", help=f"Overrides {MODEL_ENV}.")
    args = parser.parse_args(argv)
    try:
        url = check_base_url(os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL)
        model = args.model or os.environ.get(MODEL_ENV)
        if not model:
            raise ValueError(f"Set {MODEL_ENV} or pass --model.")
        key = os.environ.get(API_KEY_ENV)
        if not key:
            raise ValueError(f"Set {API_KEY_ENV} to your Anannas API key.")
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    provider = setup_tracing()  # Instrument before constructing the client.
    try:
        with make_client(base_url=url, api_key=key) as client:
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
