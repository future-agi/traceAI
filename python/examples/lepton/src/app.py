"""Trace OpenAI-compatible NVIDIA DGX Cloud Lepton Chat Completions."""

import argparse
import os
import sys
from urllib.parse import unquote, urlsplit

import openai
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_openai import OpenAIInstrumentor

DEFAULT_BASE_URL = None
DOCUMENTED_BASE_URL_FORMS = ("<ENDPOINT_URL from the API tab>",)
API_KEY_ENV = "LEPTON_API_TOKEN"
BASE_URL_ENV = "LEPTON_ENDPOINT_URL"
MODEL_ENV = "LEPTON_MODEL"


def check_base_url(url: str) -> str:
    """Check scope without rewriting or including the secret URL in errors."""
    if not url:
        raise ValueError(f"Set {BASE_URL_ENV} from the endpoint's API tab.")
    decoded = url
    while (next_value := unquote(decoded)) != decoded:
        decoded = next_value
    if any(marker in decoded for marker in ("<", ">", "ENDPOINT_URL")):
        raise ValueError(f"Replace the {BASE_URL_ENV} placeholder using the API tab.")
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        raise ValueError(f"Set {BASE_URL_ENV} to a valid HTTP endpoint base URL.") from None
    if parsed.scheme not in ("http", "https") or not host:
        raise ValueError(f"Set {BASE_URL_ENV} to a valid HTTP endpoint base URL.")
    if host == "api.lepton.ai" or host.endswith(".lepton.run"):
        raise ValueError("Legacy Lepton AI hosts are unsupported; use the endpoint's API tab.")
    if host in ("dashboard.dgxc-lepton.nvidia.com", "dashboard.lepton.ai"):
        raise ValueError("Console hosts are not inference endpoints; use the endpoint's API tab.")
    return url


def make_client(
    base_url: str | None = None,
    api_key: str | None = None,
    http_client=None,
) -> openai.OpenAI:
    if base_url is None:
        base_url = os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL
    base_url = check_base_url(base_url or "")
    api_key = api_key if api_key is not None else os.environ[API_KEY_ENV]
    if not api_key.strip():
        raise ValueError(f"Set {API_KEY_ENV} to the non-empty token shown in the API tab.")
    return openai.OpenAI(base_url=base_url, api_key=api_key, http_client=http_client)


def setup_tracing(project_name: str | None = None):
    provider = register(
        project_name=project_name or "lepton-openai-recipe",
        project_type=ProjectType.OBSERVE,
        set_global_tracer_provider=False,
        verbose=False,
    )
    OpenAIInstrumentor().instrument(tracer_provider=provider)
    return provider


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--prompt", default="What is a comet? Answer in one sentence.")
    parser.add_argument("--model")
    args = parser.parse_args(argv)
    try:
        base_url = check_base_url(os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL or "")
        model = args.model or os.environ[MODEL_ENV]
        if not model.strip():
            raise ValueError(f"Set {MODEL_ENV} to the model served by your endpoint.")
        api_key = os.environ[API_KEY_ENV]
        if not api_key.strip():
            raise ValueError(f"Set {API_KEY_ENV} to the non-empty token shown in the API tab.")
    except KeyError as error:
        print(f"Set {error.args[0]} before running this recipe.", file=sys.stderr)
        return 2
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    provider = setup_tracing()
    try:
        with make_client(base_url=base_url, api_key=api_key) as client:
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
    except (openai.OpenAIError, ValueError):
        # SDK error messages can contain the secret-equivalent endpoint URL.
        print("Endpoint request failed; check the URL, token and model in the API tab.", file=sys.stderr)
        return 1
    finally:
        provider.force_flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
