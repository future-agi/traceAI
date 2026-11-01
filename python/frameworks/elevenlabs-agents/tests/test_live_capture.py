"""Maps a body captured from a live ElevenLabs workspace on 2026-10-17 (see the fixture's _note)."""
import json

from conftest import FIXTURES, hide_all
from traceai_elevenlabs_agents.map import map_otlp_traces

CAPTURED = json.loads((FIXTURES / "live_get_conversation_opentelemetry.json").read_text())


def _spans(request):
    return [s for rs in request["resourceSpans"] for ss in rs["scopeSpans"] for s in ss["spans"]]


def _attrs(span):
    return {a["key"]: list(a["value"].values())[0] for a in span["attributes"]}


def _map(**kw):
    return map_otlp_traces(
        CAPTURED["otlp_traces"], conversation_id=CAPTURED["conversation_id"], project_name="p", **kw
    )


def test_captured_body_maps_to_conversation_llm_and_tool_spans():
    request, summary = _map()
    spans = _spans(request)
    kinds = [_attrs(s).get("fi.span.kind") for s in spans]
    assert kinds.count("CONVERSATION") == 1 and kinds.count("TOOL") == 1 and kinds.count("LLM") == 3
    tool = next(s for s in spans if _attrs(s).get("fi.span.kind") == "TOOL")
    assert _attrs(tool)["tool.name"] == "get_zen"
    llm = [_attrs(s) for s in spans if _attrs(s).get("fi.span.kind") == "LLM"]
    assert {a["gen_ai.request.model"] for a in llm} == {"gpt-4o-mini"}
    # real attribute names: elevenlabs.llm_usage.<model>.input / .output_total, JSON {"tokens", "price"}
    assert sorted(int(a["gen_ai.usage.input_tokens"]) for a in llm if "gen_ai.usage.input_tokens" in a) == [717, 745]
    assert sorted(int(a["gen_ai.usage.output_tokens"]) for a in llm if "gen_ai.usage.output_tokens" in a) == [9, 11]


def test_captured_body_hides_content_with_the_standard_switches():
    request, _ = _map(trace_config=hide_all())
    blob = json.dumps(request)
    assert "fully shipped" not in blob and "Give me a quote" not in blob
