"""Trace the customer's local PrismML Bonsai Chat Completions server."""

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

DEFAULT_BASE_URL = "http://localhost:8080/v1"
API_KEY_ENV = "PRISMML_API_KEY"
BASE_URL_ENV = "PRISMML_BASE_URL"
MODEL_ENV = "PRISMML_MODEL"
HTTP_WARNING = (
    "WARNING: PRISMML_BASE_URL uses HTTP on a non-loopback host; the Bonsai "
    "server is unauthenticated. Keep it on loopback or a trusted LAN."
)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_base_url(url: str) -> str:
    """Validate without rewriting; warn once for HTTP outside loopback."""
    def refuse(reason):
        raise ValueError(f"{BASE_URL_ENV}: {reason}")

    if any(char.isspace() or not char.isprintable() for char in url):
        refuse("remove whitespace and control characters from the URL")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        refuse("use a valid URL with a valid host and port")
    if parsed.username is not None or parsed.password is not None:
        refuse("remove embedded credentials; use PRISMML_API_KEY instead")
    if parsed.scheme not in ("http", "https"):
        refuse("use an http or https URL")
    if not host or not host.isascii():
        refuse("use a plain ASCII host")
    host = host.lower()
    host = host[:-1] if host.endswith(".") else host  # One trailing DNS dot only.
    try:
        if ":" in host:
            ipaddress.IPv6Address(host)
            if "%" in host:
                raise ValueError
        else:
            # Letters, digits and hyphens, plus underscores inside a label for
            # container and Compose service names such as bonsai_server.
            if len(host) > 253 or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?", label)
                for label in host.split(".")
            ):
                raise ValueError
            host.encode("ascii").decode("idna")
    except (ValueError, UnicodeError):
        refuse("use a valid ASCII host name (letters, digits, hyphens, inner underscores) "
               "or IP address, without malformed IDNA")
    if port == 0 or (parsed.netloc.endswith(":") and port is None):
        refuse("use a valid non-zero port")
    if "?" in url or "#" in url:
        refuse("remove the query or fragment, including an empty delimiter")
    if parsed.path not in ("/v1", "/v1/"):
        refuse(f"use the API path /v1 or /v1/, for example {DEFAULT_BASE_URL}")
    if parsed.scheme == "http" and not _is_loopback(host):
        print(HTTP_WARNING, file=sys.stderr)
    return url


def make_client(base_url: str | None = None, api_key: str | None = None,
                http_client=None) -> OpenAI:
    url = check_base_url(base_url or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL)
    key = api_key if api_key is not None else os.environ.get(API_KEY_ENV) or "not-needed"
    if not key:
        raise ValueError(f"{API_KEY_ENV}: use a non-empty placeholder such as not-needed")
    return OpenAI(base_url=url, api_key=key, http_client=http_client)


def setup_tracing(project_name: str | None = None):
    provider = register(
        project_name=project_name or "prismml-bonsai",
        project_type=ProjectType.OBSERVE,
        set_global_tracer_provider=False,
        verbose=False,
    )
    OpenAIInstrumentor().instrument(tracer_provider=provider)
    return provider


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--prompt", default="Say hello.")
    parser.add_argument("--model", default=os.environ.get(MODEL_ENV) or "bonsai")
    args = parser.parse_args(argv)
    try:
        url = check_base_url(os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL)
        if not args.model.strip():
            raise ValueError(f"{MODEL_ENV}: use a non-empty model name or bonsai")
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    key = os.environ.get(API_KEY_ENV) or "not-needed"
    provider = setup_tracing()
    try:
        # Validation above runs before tracing and emits the LAN warning only once.
        with OpenAI(base_url=url, api_key=key) as client:
            response = client.chat.completions.create(
                model=args.model,
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
