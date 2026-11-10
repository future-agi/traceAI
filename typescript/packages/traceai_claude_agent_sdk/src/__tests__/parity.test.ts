/**
 * AC-01 / AC-02 parity with the Python package.
 *
 * Reads `python/frameworks/claude-agent-sdk/traceai_claude_agent_sdk/_attributes.py`
 * from this repository and fails when a span kind, attribute name or built-in
 * tool from the Python list is missing or has a different value here.
 */
import { readFileSync } from "fs";
import { join } from "path";
import { SpanStatusCode } from "@opentelemetry/api";
import {
  BUILTIN_TOOLS,
  ClaudeAgentAttributes,
  ClaudeAgentSpanKind,
  FI_SPAN_KIND_BY_CLAUDE_KIND,
  wrapQuery,
} from "../index";
import { FISpanKind } from "@traceai/fi-semantic-conventions";
import { makeFakeQuery } from "./fixtures/fakeQuery";
import { PROMPT, mcpErrorJourney, simpleToolJourney, subagentJourney } from "./fixtures/messages";
import { drain, memoryProvider } from "./helpers";

const PYTHON_ATTRIBUTES = join(
  __dirname,
  "../../../../../python/frameworks/claude-agent-sdk/traceai_claude_agent_sdk/_attributes.py",
);

function pythonClassBody(source: string, className: string): string {
  const start = source.indexOf(`class ${className}`);
  if (start < 0) throw new Error(`class ${className} not found in ${PYTHON_ATTRIBUTES}`);
  const rest = source.slice(start + 1);
  const next = rest.search(/\n(class |# Built-in tool names|def )/);
  return next < 0 ? rest : rest.slice(0, next);
}

function pythonAssignments(body: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const match of body.matchAll(/^\s+([A-Z_]+)\s*=\s*"([^"]+)"/gm)) {
    out[match[1]] = match[2];
  }
  return out;
}

describe("Python parity", () => {
  const source = readFileSync(PYTHON_ATTRIBUTES, "utf8");

  it("has every Python span kind with the same string", () => {
    const python = pythonAssignments(pythonClassBody(source, "ClaudeAgentSpanKind"));
    expect(Object.keys(python).length).toBe(5);
    expect(ClaudeAgentSpanKind).toEqual(python);
  });

  it("has every Python attribute name with the same value (AC-02)", () => {
    const python = pythonAssignments(pythonClassBody(source, "ClaudeAgentAttributes"));
    expect(Object.keys(python).length).toBeGreaterThan(60);
    expect(ClaudeAgentAttributes).toEqual(python);
  });

  it("includes the Python built-in tool list", () => {
    const block = source.slice(source.indexOf("BUILTIN_TOOLS = frozenset(["));
    const list = block.slice(0, block.indexOf("])"));
    const python = [...list.matchAll(/"([A-Za-z]+)"/g)].map((m) => m[1]);
    expect(python.length).toBe(13);
    for (const name of python) expect(BUILTIN_TOOLS.has(name)).toBe(true);
  });

  it("maps each kind onto a Future AGI span kind that exists in fi-semantic-conventions", () => {
    const known = new Set(Object.values(FISpanKind) as string[]);
    for (const value of Object.values(FI_SPAN_KIND_BY_CLAUDE_KIND)) {
      expect(known.has(value)).toBe(true);
    }
    // TS FISpanKind has no CONVERSATION: the conversation span falls back to CHAIN.
    expect((FISpanKind as Record<string, string>)["CONVERSATION"]).toBeUndefined();
    expect(FI_SPAN_KIND_BY_CLAUDE_KIND.conversation).toBe("CHAIN");
    expect(FI_SPAN_KIND_BY_CLAUDE_KIND.assistant_turn).toBe("LLM");
    expect(FI_SPAN_KIND_BY_CLAUDE_KIND.tool_execution).toBe("TOOL");
    expect(FI_SPAN_KIND_BY_CLAUDE_KIND.mcp_tool).toBe("TOOL");
    expect(FI_SPAN_KIND_BY_CLAUDE_KIND.subagent).toBe("AGENT");
  });

  it("emits all five span kinds across J1-J3 and each span carries the names it should (AC-01, AC-02)", async () => {
    const { provider, exporter } = memoryProvider();
    for (const journey of [simpleToolJourney(), subagentJourney(), mcpErrorJourney()]) {
      await drain(wrapQuery(makeFakeQuery(journey).query, { tracerProvider: provider })({ prompt: PROMPT }));
    }
    const spans = exporter.getFinishedSpans();
    const kinds = new Set(spans.map((s) => s.attributes[ClaudeAgentAttributes.SPAN_KIND]));
    expect([...kinds].sort()).toEqual(Object.values(ClaudeAgentSpanKind).sort());

    const required: Record<string, string[]> = {
      conversation: [
        ClaudeAgentAttributes.SPAN_KIND,
        ClaudeAgentAttributes.AGENT_MODEL,
        ClaudeAgentAttributes.AGENT_SESSION_ID,
        ClaudeAgentAttributes.GEN_AI_CONVERSATION_ID,
        ClaudeAgentAttributes.AGENT_NUM_TURNS,
        ClaudeAgentAttributes.USAGE_INPUT_TOKENS,
        ClaudeAgentAttributes.USAGE_OUTPUT_TOKENS,
        ClaudeAgentAttributes.USAGE_TOTAL_TOKENS,
        ClaudeAgentAttributes.COST_TOTAL_USD,
        ClaudeAgentAttributes.DURATION_MS,
        ClaudeAgentAttributes.DURATION_API_MS,
        ClaudeAgentAttributes.IS_ERROR,
        "gen_ai.span.kind",
        "gen_ai.cost.total",
        "gen_ai.request.model",
        "session.id",
      ],
      assistant_turn: [
        ClaudeAgentAttributes.SPAN_KIND,
        ClaudeAgentAttributes.AGENT_NUM_TURNS,
        ClaudeAgentAttributes.AGENT_MODEL,
        ClaudeAgentAttributes.MESSAGE_HAS_TOOL_USE,
        ClaudeAgentAttributes.MESSAGE_TOOL_USE_COUNT,
        "gen_ai.span.kind",
        "gen_ai.request.model",
        "gen_ai.provider.name",
      ],
      tool_execution: [
        ClaudeAgentAttributes.SPAN_KIND,
        ClaudeAgentAttributes.GEN_AI_TOOL_NAME,
        ClaudeAgentAttributes.TOOL_USE_ID,
        ClaudeAgentAttributes.TOOL_SOURCE,
        ClaudeAgentAttributes.TOOL_IS_ERROR,
        ClaudeAgentAttributes.TOOL_DURATION_MS,
        "gen_ai.span.kind",
        "gen_ai.tool.name",
      ],
      mcp_tool: [
        ClaudeAgentAttributes.SPAN_KIND,
        ClaudeAgentAttributes.GEN_AI_TOOL_NAME,
        ClaudeAgentAttributes.TOOL_SOURCE,
        ClaudeAgentAttributes.MCP_SERVER_NAME,
        ClaudeAgentAttributes.TOOL_IS_ERROR,
        "gen_ai.span.kind",
      ],
      subagent: [
        ClaudeAgentAttributes.SPAN_KIND,
        ClaudeAgentAttributes.SUBAGENT_TYPE,
        ClaudeAgentAttributes.TOOL_USE_ID,
        ClaudeAgentAttributes.TOOL_DURATION_MS,
        "gen_ai.span.kind",
      ],
    };
    for (const span of spans) {
      const kind = span.attributes[ClaudeAgentAttributes.SPAN_KIND] as string;
      for (const key of required[kind]) {
        expect([span.name, key, span.attributes[key] !== undefined]).toEqual([span.name, key, true]);
      }
      expect(span.status.code).not.toBe(SpanStatusCode.UNSET);
    }
  });
});
