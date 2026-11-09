"""Pin coding-agent semantic convention wire values."""
import pytest
from fi_instrumentation.fi_types import SpanAttributes


@pytest.mark.parametrize("name, expected", [
    ("GEN_AI_COST_TOTAL", "gen_ai.cost.total"),
    ("GEN_AI_COST_INPUT", "gen_ai.cost.input"),
    ("GEN_AI_COST_OUTPUT", "gen_ai.cost.output"),
    ("GEN_AI_SERVER_TIME_TO_FIRST_TOKEN", "gen_ai.server.time_to_first_token"),
    ("TOOL_NAME", "tool.name"),
    ("TOOL_CALL_ID", "tool_call.id"),
    ("FI_USAGE_SCOPE", "fi.usage.scope"),
    ("FI_USAGE_CONTRIBUTION", "fi.usage.contribution"),
    ("FI_COST_KIND", "fi.cost.kind"),
    ("FI_AGENT_FIDELITY", "fi.agent.fidelity"),
    ("FI_AGENT_TIMING", "fi.agent.timing"),
    ("FI_AGENT_OUTCOME", "fi.agent.outcome"),
    ("FI_AGENT_USAGE_ADDITIVE", "fi.agent.usage.additive"),
    ("FI_AGENT_USAGE_PLAN", "fi.agent.usage.plan"),
    ("FI_AGENT_USAGE_CACHE_IN_INPUT", "fi.agent.usage.cache_in_input"),
    ("FI_AGENT_USAGE_REASONING_IN_OUTPUT", "fi.agent.usage.reasoning_in_output"),
    ("FI_AGENT_COST_SOURCE", "fi.agent.cost.source"),
    ("FI_AGENT_COST_ESTIMATOR", "fi.agent.cost.estimator"),
    ("FI_AGENT_GAP", "fi.agent.gap"),
    ("FI_AGENT_TRANSPORT", "fi.agent.transport"),
    ("FI_AGENT_SOURCE_CALL_ID", "fi.agent.source_call_id"),
    ("FI_AGENT_SUMMARY_INPUT", "fi.agent.summary.input"),
    ("FI_AGENT_SUMMARY_OUTPUT", "fi.agent.summary.output"),
    ("FI_AGENT_SUMMARY_TOTAL", "fi.agent.summary.total"),
    ("FI_AGENT_SUMMARY_CACHE_READ", "fi.agent.summary.cache_read"),
    ("FI_AGENT_SUMMARY_CACHE_WRITE", "fi.agent.summary.cache_write"),
    ("FI_AGENT_SUMMARY_REASONING", "fi.agent.summary.reasoning"),
    ("FI_AGENT_SUMMARY_COST_USD", "fi.agent.summary.cost_usd"),
    ("FI_AGENT_TAGS", "fi.agent.tags"),
    ("FI_AGENT_EVENT", "fi.agent.event"),
    ("FI_AGENT_INPUT_COMPLETENESS", "fi.agent.input.completeness"),
    ("FI_AGENT_CONTENT_SOURCE", "fi.agent.content.source"),
])
def test_agent_wire_contract(name, expected):
    assert getattr(SpanAttributes, name) == expected
