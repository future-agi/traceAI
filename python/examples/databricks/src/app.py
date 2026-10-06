"""Trace Databricks Chat Completions with the stock OpenAI client."""

import argparse
import os
import sys
from urllib.parse import unquote, urlsplit

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from openai import OpenAI
from traceai_openai import OpenAIInstrumentor

DEFAULT_BASE_URL = None
DOCUMENTED_BASE_URL_FORMS = (
    "https://<workspace-host>/ai-gateway/mlflow/v1",
    "https://<workspace-host>/serving-endpoints",
)
API_KEY_ENV = "DATABRICKS_TOKEN"
BASE_URL_ENV = "DATABRICKS_BASE_URL"
MODEL_ENV = "DATABRICKS_MODEL"
_PATHS = ("/ai-gateway/mlflow/v1", "/serving-endpoints")
_HOST_SUFFIXES = (".cloud.databricks.com", ".azuredatabricks.net", ".gcp.databricks.com")


def check_base_url(url: str) -> str:
    if not url:
        raise ValueError(f"Set {BASE_URL_ENV} to your workspace's OpenAI SDK base URL.")
    decoded = url
    while (next_value := unquote(decoded)) != decoded:
        decoded = next_value
    suffixes = "use /ai-gateway/mlflow/v1 or /serving-endpoints"
    if "<" in decoded or ">" in decoded:
        reason = "Replace URL placeholders with your workspace host"
        if "/invocations" in decoded:
            reason = "REST invocation URLs are not SDK base URLs; replace URL placeholders"
        raise ValueError(f"{reason}; {suffixes} in {BASE_URL_ENV}.")
    try:
        # Host and path checks use the URL as given; only the placeholder check decodes.
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        raise ValueError(f"Set {BASE_URL_ENV} to an absolute HTTP(S) URL.") from None
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError(f"Set {BASE_URL_ENV} to an absolute HTTP(S) URL.")
    if host == "example.staging.cloud.databricks.com":
        raise ValueError(
            f"The sample host is a placeholder; set {BASE_URL_ENV} to your workspace; {suffixes}."
        )
    if host.endswith(_HOST_SUFFIXES) and parts.scheme != "https":
        raise ValueError(f"Databricks workspace URLs must use https; {suffixes} in {BASE_URL_ENV}.")
    if host.endswith(_HOST_SUFFIXES) and (parts.query or parts.fragment):
        raise ValueError(f"Remove the query or fragment; {suffixes} in {BASE_URL_ENV}.")
    if host.endswith(_HOST_SUFFIXES) and parts.path.rstrip("/") not in _PATHS:
        reason = "Unsupported Databricks SDK base URL"
        if parts.path.startswith("/serving-endpoints/") and parts.path.rstrip("/").endswith(
            "/invocations"
        ):
            reason = "REST invocation URLs are not SDK base URLs"
        elif parts.path.rstrip("/") in ("/ai-gateway/gemini", "/ai-gateway/anthropic"):
            reason = "Gateway native APIs for other SDKs are not covered"
        raise ValueError(f"{reason}; {suffixes} in {BASE_URL_ENV}.")
    return url


def make_client(
    base_url: str | None = None, api_key: str | None = None, http_client=None
) -> OpenAI:
    url = check_base_url(
        base_url if base_url is not None else os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL
    )
    key = os.environ[API_KEY_ENV] if api_key is None else api_key
    return OpenAI(base_url=url, api_key=key, http_client=http_client)


def setup_tracing(project_name: str | None = None):
    provider = register(
        project_name=project_name or "databricks-openai-recipe",
        project_type=ProjectType.OBSERVE,
        set_global_tracer_provider=False,
        verbose=False,
    )
    OpenAIInstrumentor().instrument(tracer_provider=provider)
    return provider


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--prompt", default="What is a lakehouse? Answer in one sentence.")
    parser.add_argument("--model")
    args = parser.parse_args(argv)
    try:
        url = check_base_url(os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL)
        model = args.model or os.environ.get(MODEL_ENV)
        if not model:
            raise ValueError(f"Set {MODEL_ENV} or pass --model.")
        key = os.environ.get(API_KEY_ENV)
        if not key:
            raise ValueError(f"Set {API_KEY_ENV} to a Databricks token from your secret store.")
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    provider = setup_tracing()
    try:
        with make_client(base_url=url, api_key=key) as client:
            result = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": args.prompt}],
                stream=args.stream,
            )
            if args.stream:
                for chunk in result:
                    if chunk.choices:
                        print(chunk.choices[0].delta.content or "", end="", flush=True)
                print()
            else:
                print(result.choices[0].message.content or "")
    finally:
        provider.force_flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
