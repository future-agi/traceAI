package ai.traceai;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.assertEquals;

class AgentSemanticConventionsTest {
    @Test
    void codingAgentWireContract() {
        assertEquals("gen_ai.cost.total", SemanticConventions.GEN_AI_COST_TOTAL);
        assertEquals("gen_ai.cost.input", SemanticConventions.GEN_AI_COST_INPUT);
        assertEquals("gen_ai.cost.output", SemanticConventions.GEN_AI_COST_OUTPUT);
        assertEquals("gen_ai.server.time_to_first_token", SemanticConventions.GEN_AI_SERVER_TIME_TO_FIRST_TOKEN);
        assertEquals("tool.name", SemanticConventions.TOOL_NAME);
        assertEquals("tool_call.id", SemanticConventions.TOOL_CALL_ID);
        assertEquals("fi.usage.scope", SemanticConventions.FI_USAGE_SCOPE);
        assertEquals("fi.usage.contribution", SemanticConventions.FI_USAGE_CONTRIBUTION);
        assertEquals("fi.cost.kind", SemanticConventions.FI_COST_KIND);
        assertEquals("fi.agent.fidelity", SemanticConventions.FI_AGENT_FIDELITY);
        assertEquals("fi.agent.timing", SemanticConventions.FI_AGENT_TIMING);
        assertEquals("fi.agent.outcome", SemanticConventions.FI_AGENT_OUTCOME);
        assertEquals("fi.agent.usage.additive", SemanticConventions.FI_AGENT_USAGE_ADDITIVE);
        assertEquals("fi.agent.usage.plan", SemanticConventions.FI_AGENT_USAGE_PLAN);
        assertEquals("fi.agent.usage.cache_in_input", SemanticConventions.FI_AGENT_USAGE_CACHE_IN_INPUT);
        assertEquals("fi.agent.usage.reasoning_in_output", SemanticConventions.FI_AGENT_USAGE_REASONING_IN_OUTPUT);
        assertEquals("fi.agent.cost.source", SemanticConventions.FI_AGENT_COST_SOURCE);
        assertEquals("fi.agent.cost.estimator", SemanticConventions.FI_AGENT_COST_ESTIMATOR);
        assertEquals("fi.agent.gap", SemanticConventions.FI_AGENT_GAP);
        assertEquals("fi.agent.transport", SemanticConventions.FI_AGENT_TRANSPORT);
        assertEquals("fi.agent.source_call_id", SemanticConventions.FI_AGENT_SOURCE_CALL_ID);
        assertEquals("fi.agent.summary.input", SemanticConventions.FI_AGENT_SUMMARY_INPUT);
        assertEquals("fi.agent.summary.output", SemanticConventions.FI_AGENT_SUMMARY_OUTPUT);
        assertEquals("fi.agent.summary.total", SemanticConventions.FI_AGENT_SUMMARY_TOTAL);
        assertEquals("fi.agent.summary.cache_read", SemanticConventions.FI_AGENT_SUMMARY_CACHE_READ);
        assertEquals("fi.agent.summary.cache_write", SemanticConventions.FI_AGENT_SUMMARY_CACHE_WRITE);
        assertEquals("fi.agent.summary.reasoning", SemanticConventions.FI_AGENT_SUMMARY_REASONING);
        assertEquals("fi.agent.summary.cost_usd", SemanticConventions.FI_AGENT_SUMMARY_COST_USD);
        assertEquals("fi.agent.tags", SemanticConventions.FI_AGENT_TAGS);
        assertEquals("fi.agent.event", SemanticConventions.FI_AGENT_EVENT);
        assertEquals("fi.agent.input.completeness", SemanticConventions.FI_AGENT_INPUT_COMPLETENESS);
        assertEquals("fi.agent.content.source", SemanticConventions.FI_AGENT_CONTENT_SOURCE);
    }
}
