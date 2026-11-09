namespace FIInstrumentation.Tests;

public class AgentSemanticConventionsTests
{
    [Fact]
    public void CodingAgentWireContract()
    {
        Assert.Equal("gen_ai.cost.total", SemanticConventions.GenAiCostTotal);
        Assert.Equal("gen_ai.cost.input", SemanticConventions.GenAiCostInput);
        Assert.Equal("gen_ai.cost.output", SemanticConventions.GenAiCostOutput);
        Assert.Equal("gen_ai.server.time_to_first_token", SemanticConventions.GenAiServerTimeToFirstToken);
        Assert.Equal("tool.name", SemanticConventions.ToolName);
        Assert.Equal("tool_call.id", SemanticConventions.ToolCallId);
        Assert.Equal("fi.usage.scope", SemanticConventions.FiUsageScope);
        Assert.Equal("fi.usage.contribution", SemanticConventions.FiUsageContribution);
        Assert.Equal("fi.cost.kind", SemanticConventions.FiCostKind);
        Assert.Equal("fi.agent.fidelity", SemanticConventions.FiAgentFidelity);
        Assert.Equal("fi.agent.timing", SemanticConventions.FiAgentTiming);
        Assert.Equal("fi.agent.outcome", SemanticConventions.FiAgentOutcome);
        Assert.Equal("fi.agent.usage.additive", SemanticConventions.FiAgentUsageAdditive);
        Assert.Equal("fi.agent.usage.plan", SemanticConventions.FiAgentUsagePlan);
        Assert.Equal("fi.agent.usage.cache_in_input", SemanticConventions.FiAgentUsageCacheInInput);
        Assert.Equal("fi.agent.usage.reasoning_in_output", SemanticConventions.FiAgentUsageReasoningInOutput);
        Assert.Equal("fi.agent.cost.source", SemanticConventions.FiAgentCostSource);
        Assert.Equal("fi.agent.cost.estimator", SemanticConventions.FiAgentCostEstimator);
        Assert.Equal("fi.agent.gap", SemanticConventions.FiAgentGap);
        Assert.Equal("fi.agent.transport", SemanticConventions.FiAgentTransport);
        Assert.Equal("fi.agent.source_call_id", SemanticConventions.FiAgentSourceCallId);
        Assert.Equal("fi.agent.summary.input", SemanticConventions.FiAgentSummaryInput);
        Assert.Equal("fi.agent.summary.output", SemanticConventions.FiAgentSummaryOutput);
        Assert.Equal("fi.agent.summary.total", SemanticConventions.FiAgentSummaryTotal);
        Assert.Equal("fi.agent.summary.cache_read", SemanticConventions.FiAgentSummaryCacheRead);
        Assert.Equal("fi.agent.summary.cache_write", SemanticConventions.FiAgentSummaryCacheWrite);
        Assert.Equal("fi.agent.summary.reasoning", SemanticConventions.FiAgentSummaryReasoning);
        Assert.Equal("fi.agent.summary.cost_usd", SemanticConventions.FiAgentSummaryCostUsd);
        Assert.Equal("fi.agent.tags", SemanticConventions.FiAgentTags);
        Assert.Equal("fi.agent.event", SemanticConventions.FiAgentEvent);
        Assert.Equal("fi.agent.input.completeness", SemanticConventions.FiAgentInputCompleteness);
        Assert.Equal("fi.agent.content.source", SemanticConventions.FiAgentContentSource);
    }
}
