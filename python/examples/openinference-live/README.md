# OpenInference live run (TH-8336 re-verification)

`live_e2e.py` runs real OpenInference instrumentation (`openinference-instrumentation-openai` plus the `OITracer` tool/agent/retriever helpers) against a Future AGI collector. It is the real-instrumentor round trip that the fixture in `../openinference/` deliberately does not do. CI does not run it; `tests/` only checks the script statically.

It makes one agent trace (retriever, two OpenAI chat calls with a tool call, one tool) and one failure trace (a tool that raises, and a chat call with an unknown model). A unique marker string is put in every piece of content so privacy can be checked.

```bash
pip install -r requirements.txt
export FI_BASE_URL=http://localhost:14338 FI_API_KEY=... FI_SECRET_KEY=...
export OPENAI_API_KEY=...           # OPENAI_BASE_URL optional (any OpenAI-compatible endpoint)
python live_e2e.py my-project MARKER123
# content off:
OPENINFERENCE_HIDE_INPUTS=true OPENINFERENCE_HIDE_OUTPUTS=true \
OPENINFERENCE_HIDE_INPUT_MESSAGES=true OPENINFERENCE_HIDE_OUTPUT_MESSAGES=true \
OPENINFERENCE_HIDE_INPUT_TEXT=true OPENINFERENCE_HIDE_OUTPUT_TEXT=true \
python live_e2e.py my-project-hidden MARKER456
```

Result on 2026-10-16 (local Future AGI stack, openinference-instrumentation 0.1.71 and 0.1.70): agent, retriever, llm (model and tokens), tool, and two ERROR spans stored; with the hide variables the marker is absent from every stored column except the span status message of the two errors (the exception text is exported unmasked). See the docs page for the full table.
