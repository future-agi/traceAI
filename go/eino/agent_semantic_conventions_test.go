package traceaieino

import "testing"

func TestCodingAgentWireContract(t *testing.T) {
	for name, pair := range map[string][2]string{
		"GenAICostTotal":                {GenAICostTotal, "gen_ai.cost.total"},
		"GenAICostInput":                {GenAICostInput, "gen_ai.cost.input"},
		"GenAICostOutput":               {GenAICostOutput, "gen_ai.cost.output"},
		"GenAIServerTimeToFirstToken":   {GenAIServerTimeToFirstToken, "gen_ai.server.time_to_first_token"},
		"ToolName":                      {ToolName, "tool.name"},
		"ToolCallID":                    {ToolCallID, "tool_call.id"},
		"FIUsageScope":                  {FIUsageScope, "fi.usage.scope"},
		"FIUsageContribution":           {FIUsageContribution, "fi.usage.contribution"},
		"FICostKind":                    {FICostKind, "fi.cost.kind"},
		"FIAgentFidelity":               {FIAgentFidelity, "fi.agent.fidelity"},
		"FIAgentTiming":                 {FIAgentTiming, "fi.agent.timing"},
		"FIAgentOutcome":                {FIAgentOutcome, "fi.agent.outcome"},
		"FIAgentUsageAdditive":          {FIAgentUsageAdditive, "fi.agent.usage.additive"},
		"FIAgentUsagePlan":              {FIAgentUsagePlan, "fi.agent.usage.plan"},
		"FIAgentUsageCacheInInput":      {FIAgentUsageCacheInInput, "fi.agent.usage.cache_in_input"},
		"FIAgentUsageReasoningInOutput": {FIAgentUsageReasoningInOutput, "fi.agent.usage.reasoning_in_output"},
		"FIAgentCostSource":             {FIAgentCostSource, "fi.agent.cost.source"},
		"FIAgentCostEstimator":          {FIAgentCostEstimator, "fi.agent.cost.estimator"},
		"FIAgentGap":                    {FIAgentGap, "fi.agent.gap"},
		"FIAgentTransport":              {FIAgentTransport, "fi.agent.transport"},
		"FIAgentSourceCallID":           {FIAgentSourceCallID, "fi.agent.source_call_id"},
		"FIAgentSummaryInput":           {FIAgentSummaryInput, "fi.agent.summary.input"},
		"FIAgentSummaryOutput":          {FIAgentSummaryOutput, "fi.agent.summary.output"},
		"FIAgentSummaryTotal":           {FIAgentSummaryTotal, "fi.agent.summary.total"},
		"FIAgentSummaryCacheRead":       {FIAgentSummaryCacheRead, "fi.agent.summary.cache_read"},
		"FIAgentSummaryCacheWrite":      {FIAgentSummaryCacheWrite, "fi.agent.summary.cache_write"},
		"FIAgentSummaryReasoning":       {FIAgentSummaryReasoning, "fi.agent.summary.reasoning"},
		"FIAgentSummaryCostUSD":         {FIAgentSummaryCostUSD, "fi.agent.summary.cost_usd"},
		"FIAgentTags":                   {FIAgentTags, "fi.agent.tags"},
		"FIAgentEvent":                  {FIAgentEvent, "fi.agent.event"},
		"FIAgentInputCompleteness":      {FIAgentInputCompleteness, "fi.agent.input.completeness"},
		"FIAgentContentSource":          {FIAgentContentSource, "fi.agent.content.source"},
	} {
		if pair[0] != pair[1] {
			t.Errorf("%s = %q, want %q", name, pair[0], pair[1])
		}
	}
}
