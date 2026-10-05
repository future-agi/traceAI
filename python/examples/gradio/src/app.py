"""Trace a Gradio chat app with traceAI's OpenAI instrumentor.

Recipe example, not a package. Gradio emits no GenAI spans. The one span per
chat turn comes from ``traceai_openai`` instrumenting the OpenAI client that
``predict`` calls. This file calls ``register()`` once at startup, instruments
the OpenAI client with content capture off, and passes a keyed digest of
Gradio's session hash to ``using_session`` so the turns of one browser session
share a session id.

    python src/app.py

FI_API_KEY, FI_SECRET_KEY, FI_PROJECT_NAME and (optionally) FI_BASE_URL are
read from the environment; so are OPENAI_API_KEY, OPENAI_BASE_URL and
OPENAI_MODEL. See README.md.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from contextlib import nullcontext

import gradio as gr
from fi_instrumentation import TraceConfig, register, using_session
from fi_instrumentation.fi_types import ProjectType
from openai import OpenAI
from traceai_openai import OpenAIInstrumentor

MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")


def init_tracing(trace_content: bool = False):
    """Register the Future AGI provider and instrument OpenAI. Call once, before the first turn."""
    provider = register(
        project_name=os.environ["FI_PROJECT_NAME"],
        project_type=ProjectType.OBSERVE,
        verbose=False,
    )
    # traceAI records prompts and answers unless told not to: TraceConfig's
    # defaults are hide_inputs=False and hide_outputs=False.
    if trace_content:
        config = TraceConfig()
    else:
        config = TraceConfig(hide_inputs=True, hide_outputs=True)
    OpenAIInstrumentor().instrument(tracer_provider=provider, config=config)
    return provider


# OpenAI() reads OPENAI_API_KEY and OPENAI_BASE_URL.
client = OpenAI()


def _text(content) -> str:
    # Gradio 6 passes each history message's content as a list of parts.
    if isinstance(content, str):
        return content
    return "".join(part["text"] for part in content if part.get("type") == "text")


# Gradio's session hash identifies one browser session (one page load), not a
# user. It is also the key to that session's live state in the running app,
# so it is not exported. The span gets a digest keyed with a random
# per-process key that never leaves the process. Gradio's reload mode re-runs
# this file in the same namespace; keeping the key keeps the ids of open
# sessions.
_SESSION_KEY = globals().get("_SESSION_KEY") or secrets.token_bytes(32)


def _session_id(request: gr.Request | None) -> str | None:
    if request is None or not request.session_hash:
        return None
    digest = hmac.new(_SESSION_KEY, request.session_hash.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:32]


def predict(message: str, history: list, request: gr.Request = None) -> str:
    messages = [{"role": m["role"], "content": _text(m["content"])} for m in history]
    messages.append({"role": "user", "content": message})
    # With a Gradio session, every turn of it carries the same session.id;
    # without one, each turn is an unrelated trace.
    session_id = _session_id(request)
    with using_session(session_id) if session_id else nullcontext():
        response = client.chat.completions.create(model=MODEL, messages=messages)
    return response.choices[0].message.content or ""


# analytics_enabled=False turns off Gradio's own usage analytics, which
# otherwise contact api.gradio.app when the app is built. It is unrelated to
# tracing.
demo = gr.ChatInterface(predict, analytics_enabled=False)

if __name__ == "__main__":
    init_tracing()
    # register() flushes the batch on normal exit and on SIGINT/SIGTERM.
    demo.launch()
