import * as exported from "../index";
import { SemanticConventions as SC } from "../index";

describe("coding-agent wire contract", () => {
  test("GEN_AI_COST_TOTAL", () => {
    expect(exported.GEN_AI_COST_TOTAL).toBe("gen_ai.cost.total");
    expect(SC.GEN_AI_COST_TOTAL).toBe("gen_ai.cost.total");
  });
  test("GEN_AI_COST_INPUT", () => {
    expect(exported.GEN_AI_COST_INPUT).toBe("gen_ai.cost.input");
    expect(SC.GEN_AI_COST_INPUT).toBe("gen_ai.cost.input");
  });
  test("GEN_AI_COST_OUTPUT", () => {
    expect(exported.GEN_AI_COST_OUTPUT).toBe("gen_ai.cost.output");
    expect(SC.GEN_AI_COST_OUTPUT).toBe("gen_ai.cost.output");
  });
  test("GEN_AI_SERVER_TIME_TO_FIRST_TOKEN", () => {
    expect(exported.GEN_AI_SERVER_TIME_TO_FIRST_TOKEN).toBe("gen_ai.server.time_to_first_token");
    expect(SC.GEN_AI_SERVER_TIME_TO_FIRST_TOKEN).toBe("gen_ai.server.time_to_first_token");
  });
  test("TOOL_NAME", () => {
    expect(exported.TOOL_NAME).toBe("tool.name");
    expect(SC.TOOL_NAME).toBe("tool.name");
  });
  test("TOOL_CALL_ID", () => {
    expect(exported.TOOL_CALL_ID).toBe("tool_call.id");
    expect(SC.TOOL_CALL_ID).toBe("tool_call.id");
  });
  test("FI_USAGE_SCOPE", () => {
    expect(exported.FI_USAGE_SCOPE).toBe("fi.usage.scope");
    expect(SC.FI_USAGE_SCOPE).toBe("fi.usage.scope");
  });
  test("FI_USAGE_CONTRIBUTION", () => {
    expect(exported.FI_USAGE_CONTRIBUTION).toBe("fi.usage.contribution");
    expect(SC.FI_USAGE_CONTRIBUTION).toBe("fi.usage.contribution");
  });
  test("FI_COST_KIND", () => {
    expect(exported.FI_COST_KIND).toBe("fi.cost.kind");
    expect(SC.FI_COST_KIND).toBe("fi.cost.kind");
  });
  test("FI_AGENT_FIDELITY", () => {
    expect(exported.FI_AGENT_FIDELITY).toBe("fi.agent.fidelity");
    expect(SC.FI_AGENT_FIDELITY).toBe("fi.agent.fidelity");
  });
  test("FI_AGENT_TIMING", () => {
    expect(exported.FI_AGENT_TIMING).toBe("fi.agent.timing");
    expect(SC.FI_AGENT_TIMING).toBe("fi.agent.timing");
  });
  test("FI_AGENT_OUTCOME", () => {
    expect(exported.FI_AGENT_OUTCOME).toBe("fi.agent.outcome");
    expect(SC.FI_AGENT_OUTCOME).toBe("fi.agent.outcome");
  });
  test("FI_AGENT_USAGE_ADDITIVE", () => {
    expect(exported.FI_AGENT_USAGE_ADDITIVE).toBe("fi.agent.usage.additive");
    expect(SC.FI_AGENT_USAGE_ADDITIVE).toBe("fi.agent.usage.additive");
  });
  test("FI_AGENT_USAGE_PLAN", () => {
    expect(exported.FI_AGENT_USAGE_PLAN).toBe("fi.agent.usage.plan");
    expect(SC.FI_AGENT_USAGE_PLAN).toBe("fi.agent.usage.plan");
  });
  test("FI_AGENT_USAGE_CACHE_IN_INPUT", () => {
    expect(exported.FI_AGENT_USAGE_CACHE_IN_INPUT).toBe("fi.agent.usage.cache_in_input");
    expect(SC.FI_AGENT_USAGE_CACHE_IN_INPUT).toBe("fi.agent.usage.cache_in_input");
  });
  test("FI_AGENT_USAGE_REASONING_IN_OUTPUT", () => {
    expect(exported.FI_AGENT_USAGE_REASONING_IN_OUTPUT).toBe("fi.agent.usage.reasoning_in_output");
    expect(SC.FI_AGENT_USAGE_REASONING_IN_OUTPUT).toBe("fi.agent.usage.reasoning_in_output");
  });
  test("FI_AGENT_COST_SOURCE", () => {
    expect(exported.FI_AGENT_COST_SOURCE).toBe("fi.agent.cost.source");
    expect(SC.FI_AGENT_COST_SOURCE).toBe("fi.agent.cost.source");
  });
  test("FI_AGENT_COST_ESTIMATOR", () => {
    expect(exported.FI_AGENT_COST_ESTIMATOR).toBe("fi.agent.cost.estimator");
    expect(SC.FI_AGENT_COST_ESTIMATOR).toBe("fi.agent.cost.estimator");
  });
  test("FI_AGENT_GAP", () => {
    expect(exported.FI_AGENT_GAP).toBe("fi.agent.gap");
    expect(SC.FI_AGENT_GAP).toBe("fi.agent.gap");
  });
  test("FI_AGENT_TRANSPORT", () => {
    expect(exported.FI_AGENT_TRANSPORT).toBe("fi.agent.transport");
    expect(SC.FI_AGENT_TRANSPORT).toBe("fi.agent.transport");
  });
  test("FI_AGENT_SOURCE_CALL_ID", () => {
    expect(exported.FI_AGENT_SOURCE_CALL_ID).toBe("fi.agent.source_call_id");
    expect(SC.FI_AGENT_SOURCE_CALL_ID).toBe("fi.agent.source_call_id");
  });
  test("FI_AGENT_SUMMARY_INPUT", () => {
    expect(exported.FI_AGENT_SUMMARY_INPUT).toBe("fi.agent.summary.input");
    expect(SC.FI_AGENT_SUMMARY_INPUT).toBe("fi.agent.summary.input");
  });
  test("FI_AGENT_SUMMARY_OUTPUT", () => {
    expect(exported.FI_AGENT_SUMMARY_OUTPUT).toBe("fi.agent.summary.output");
    expect(SC.FI_AGENT_SUMMARY_OUTPUT).toBe("fi.agent.summary.output");
  });
  test("FI_AGENT_SUMMARY_TOTAL", () => {
    expect(exported.FI_AGENT_SUMMARY_TOTAL).toBe("fi.agent.summary.total");
    expect(SC.FI_AGENT_SUMMARY_TOTAL).toBe("fi.agent.summary.total");
  });
  test("FI_AGENT_SUMMARY_CACHE_READ", () => {
    expect(exported.FI_AGENT_SUMMARY_CACHE_READ).toBe("fi.agent.summary.cache_read");
    expect(SC.FI_AGENT_SUMMARY_CACHE_READ).toBe("fi.agent.summary.cache_read");
  });
  test("FI_AGENT_SUMMARY_CACHE_WRITE", () => {
    expect(exported.FI_AGENT_SUMMARY_CACHE_WRITE).toBe("fi.agent.summary.cache_write");
    expect(SC.FI_AGENT_SUMMARY_CACHE_WRITE).toBe("fi.agent.summary.cache_write");
  });
  test("FI_AGENT_SUMMARY_REASONING", () => {
    expect(exported.FI_AGENT_SUMMARY_REASONING).toBe("fi.agent.summary.reasoning");
    expect(SC.FI_AGENT_SUMMARY_REASONING).toBe("fi.agent.summary.reasoning");
  });
  test("FI_AGENT_SUMMARY_COST_USD", () => {
    expect(exported.FI_AGENT_SUMMARY_COST_USD).toBe("fi.agent.summary.cost_usd");
    expect(SC.FI_AGENT_SUMMARY_COST_USD).toBe("fi.agent.summary.cost_usd");
  });
  test("FI_AGENT_TAGS", () => {
    expect(exported.FI_AGENT_TAGS).toBe("fi.agent.tags");
    expect(SC.FI_AGENT_TAGS).toBe("fi.agent.tags");
  });
  test("FI_AGENT_EVENT", () => {
    expect(exported.FI_AGENT_EVENT).toBe("fi.agent.event");
    expect(SC.FI_AGENT_EVENT).toBe("fi.agent.event");
  });
  test("FI_AGENT_INPUT_COMPLETENESS", () => {
    expect(exported.FI_AGENT_INPUT_COMPLETENESS).toBe("fi.agent.input.completeness");
    expect(SC.FI_AGENT_INPUT_COMPLETENESS).toBe("fi.agent.input.completeness");
  });
  test("FI_AGENT_CONTENT_SOURCE", () => {
    expect(exported.FI_AGENT_CONTENT_SOURCE).toBe("fi.agent.content.source");
    expect(SC.FI_AGENT_CONTENT_SOURCE).toBe("fi.agent.content.source");
  });
});
