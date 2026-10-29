"""Real README demo: local source lookup, one LLM call, deterministic validation.

No token/cost/duration values are set: the SDK records provider usage and the
Cloud backend computes cost. Set FI_API_KEY, FI_SECRET_KEY, OPENAI_API_KEY and OPENAI_MODEL privately.
One real OpenAI-compatible model call is made. Optional OPENAI_BASE_URL is
supported by the OpenAI client; no credentials are bundled.
"""
import json
import os
from pathlib import Path
from openai import OpenAI
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType, SpanAttributes as A
from traceai_openai import OpenAIInstrumentor
from opentelemetry.trace import Status, StatusCode

provider = register(project_name="traceai-quickstart", project_type=ProjectType.OBSERVE)
OpenAIInstrumentor().instrument(tracer_provider=provider)
tracer = provider.get_tracer("traceai.readme.demo")
question = "How do I trace OpenAI calls while keeping my existing OpenTelemetry pipeline?"
repo = Path(__file__).resolve().parents[2]
try:
    with tracer.start_as_current_span("SDK guide assistant", attributes={
        A.GEN_AI_SPAN_KIND: "AGENT", A.GEN_AI_AGENT_NAME: "SDK guide assistant",
        A.INPUT_VALUE: question,
    }) as root:
        trace_id = format(root.get_span_context().trace_id, "032x")
        with tracer.start_as_current_span("Search SDK guides", attributes={
            A.GEN_AI_SPAN_KIND: "TOOL", A.GEN_AI_TOOL_NAME: "search_sdk_guides",
            A.INPUT_VALUE: "OpenAI instrumentor and tracer_provider",
        }) as tool:
            text = (repo / "README.md").read_text()
            quickstart = text.split("### Python\n", 1)[1].split("### TypeScript", 1)[0]
            pipeline = text.split("## OpenTelemetry, without a new tracing stack", 1)[1].split("## Privacy controls", 1)[0]
            instrument_line = next(line for line in quickstart.splitlines() if "instrument(tracer_provider=provider)" in line)
            provider_line = next(line for line in pipeline.splitlines() if line.startswith("- **Use your existing provider."))
            context = instrument_line + "\n" + provider_line.replace("- **Use your existing provider.** ", "")
            tool.set_attribute(A.OUTPUT_VALUE, context)
            tool.set_status(Status(StatusCode.OK))
        with tracer.start_as_current_span("Write a grounded answer", attributes={
            A.GEN_AI_SPAN_KIND: "CHAIN", A.INPUT_VALUE: question,
        }) as chain:
            response = OpenAI().chat.completions.create(
                model=os.environ["OPENAI_MODEL"],
                messages=[
                    {"role": "system", "content": "Answer using only the retrieved SDK guide. Give three short steps, under 60 words."},
                    {"role": "user", "content": question + "\n\nSDK guide excerpt:\n" + context},
                ],
            )
            answer = response.choices[0].message.content
            chain.set_attribute(A.OUTPUT_VALUE, answer)
            chain.set_status(Status(StatusCode.OK))
        with tracer.start_as_current_span("Check the answer", attributes={
            A.GEN_AI_SPAN_KIND: "TOOL", A.GEN_AI_TOOL_NAME: "validate_answer",
            A.INPUT_VALUE: answer,
        }) as check:
            checks = {"mentions_instrumentation": "instrument" in answer.lower(),
                      "mentions_provider": "provider" in answer.lower(),
                      "has_content": bool(answer.strip())}
            check.set_attribute(A.OUTPUT_VALUE, json.dumps(checks))
            assert all(checks.values()), checks
            check.set_status(Status(StatusCode.OK))
        root.set_attribute(A.OUTPUT_VALUE, answer)
        root.set_status(Status(StatusCode.OK))
        print(json.dumps({"trace_id_hex": trace_id, "answer": answer,
                          "usage": response.usage.model_dump(),
                          "checks": checks}, indent=2))
finally:
    provider.shutdown()
