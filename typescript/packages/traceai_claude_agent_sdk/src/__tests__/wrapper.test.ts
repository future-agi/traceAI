import type { SDKMessage } from "@anthropic-ai/claude-agent-sdk";
import { SpanStatusCode, context, trace } from "@opentelemetry/api";
import {
  BasicTracerProvider,
  BatchSpanProcessor,
  InMemorySpanExporter,
  ReadableSpan,
  SpanExporter,
} from "@opentelemetry/sdk-trace-base";
import { setSession, setUser } from "@traceai/fi-core";
import {
  ClaudeAgentSDKInstrumentation,
  isWrappedQuery,
  shutdown,
  wrapQuery,
} from "../index";
import { makeFakeQuery } from "./fixtures/fakeQuery";
import {
  AGENT_TOOL_ID,
  ASSISTANT_TEXT_MARKER,
  MCP_TOOL_ID,
  MODEL,
  PROMPT,
  SESSION_ID,
  SUBAGENT_GREP_ID,
  TOOL_INPUT_MARKER,
  TOOL_OUTPUT_MARKER,
  TOTAL_COST_USD,
  assistant,
  backgroundedSubagentJourney,
  init,
  mcpErrorJourney,
  resultSuccess,
  simpleToolJourney,
  streamingInputJourney,
  subagentJourney,
  taskNotification,
  text,
  toolResult,
  toolUse,
} from "./fixtures/messages";
import { StackContextManager, allAttributeText, byName, drain, memoryProvider, one, parentId } from "./helpers";

const CONTENT_ENV = ["FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS"] as const;

describe("wrapQuery", () => {
  let saved: Record<string, string | undefined>;

  beforeEach(() => {
    saved = {};
    for (const key of [...CONTENT_ENV, "ANTHROPIC_BASE_URL"]) {
      saved[key] = process.env[key];
      delete process.env[key];
    }
  });

  afterEach(() => {
    for (const [key, value] of Object.entries(saved)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  });

  async function run(
    messages: ReturnType<typeof simpleToolJourney>,
    options: Record<string, unknown> = {},
    traceConfig?: { hideInputs?: boolean; hideOutputs?: boolean },
  ) {
    const { provider, exporter } = memoryProvider();
    const fake = makeFakeQuery(messages);
    const traced = wrapQuery(fake.query, { tracerProvider: provider, traceConfig });
    const params = { prompt: PROMPT, options };
    const yielded = await drain(traced(params));
    return { spans: exporter.getFinishedSpans(), yielded, fake, params };
  }

  describe("J1: built-in tool (AC-01, AC-03, AC-04, AC-05, AC-08)", () => {
    it("emits conversation, assistant_turn and tool_execution spans with both span-kind keys", async () => {
      const { spans } = await run(simpleToolJourney());

      expect(spans.map((s) => s.name).sort()).toEqual(
        ["claude_agent.assistant_turn", "claude_agent.assistant_turn", "claude_agent.conversation", "tool.Read"].sort(),
      );
      const conversation = one(spans, "claude_agent.conversation");
      const [turn1, turn2] = byName(spans, "claude_agent.assistant_turn").sort(
        (a, b) => (a.attributes["claude_agent.num_turns"] as number) - (b.attributes["claude_agent.num_turns"] as number),
      );
      const tool = one(spans, "tool.Read");

      expect(conversation.attributes["claude_agent.span_kind"]).toBe("conversation");
      expect(conversation.attributes["gen_ai.span.kind"]).toBe("CHAIN");
      expect(conversation.attributes["fi.span.kind"]).toBe("CHAIN");
      expect(turn1.attributes["claude_agent.span_kind"]).toBe("assistant_turn");
      expect(turn1.attributes["gen_ai.span.kind"]).toBe("LLM");
      expect(tool.attributes["claude_agent.span_kind"]).toBe("tool_execution");
      expect(tool.attributes["gen_ai.span.kind"]).toBe("TOOL");
      expect(tool.attributes["fi.span.kind"]).toBe("TOOL");

      // AC-04: one trace, tool parent is the assistant turn that issued it.
      expect(new Set(spans.map((s) => s.spanContext().traceId)).size).toBe(1);
      expect(parentId(turn1)).toBe(conversation.spanContext().spanId);
      expect(parentId(turn2)).toBe(conversation.spanContext().spanId);
      expect(parentId(tool)).toBe(turn1.spanContext().spanId);

      // The split API response (two SDK messages, same message.id) is one turn.
      expect(turn1.attributes["claude_agent.message.tool_use_count"]).toBe(1);
      expect(turn1.attributes["claude_agent.message.has_tool_use"]).toBe(true);
      expect(turn2.attributes["claude_agent.message.has_tool_use"]).toBe(false);

      // Model, provider, tool metadata.
      expect(conversation.attributes["gen_ai.request.model"]).toBe(MODEL);
      expect(conversation.attributes["claude_agent.model"]).toBe(MODEL);
      expect(turn1.attributes["gen_ai.request.model"]).toBe(MODEL);
      expect(turn1.attributes["gen_ai.provider.name"]).toBe("anthropic");
      expect(tool.attributes["claude_agent.tool.name"]).toBe("Read");
      expect(tool.attributes["gen_ai.tool.name"]).toBe("Read");
      expect(tool.attributes["claude_agent.tool.use_id"]).toBe("toolu_read_1");
      expect(tool.attributes["claude_agent.tool.source"]).toBe("builtin");
      expect(tool.attributes["claude_agent.tool.is_error"]).toBe(false);
      expect(typeof tool.attributes["claude_agent.tool.duration_ms"]).toBe("number");

      // AC-05: session id from the init message on every span.
      for (const span of spans) {
        expect(span.attributes["session.id"]).toBe(SESSION_ID);
        expect(span.attributes["claude_agent.session.id"]).toBe(SESSION_ID);
      }
      expect(conversation.attributes["claude_agent.session_id"]).toBe(SESSION_ID);
      expect(conversation.attributes["claude_agent.session.is_new"]).toBe(true);

      // AC-03: usage and cost from the result message.
      expect(conversation.attributes["gen_ai.usage.input_tokens"]).toBe(120);
      expect(conversation.attributes["gen_ai.usage.output_tokens"]).toBe(45);
      expect(conversation.attributes["gen_ai.usage.total_tokens"]).toBe(165);
      expect(conversation.attributes["gen_ai.usage.cache_read_tokens"]).toBe(11);
      expect(conversation.attributes["gen_ai.usage.cache_creation_tokens"]).toBe(7);
      expect(conversation.attributes["claude_agent.cost.total_usd"]).toBe(TOTAL_COST_USD);
      expect(conversation.attributes["gen_ai.cost.total"]).toBe(TOTAL_COST_USD);
      expect(conversation.attributes["claude_agent.duration_ms"]).toBe(1500);
      expect(conversation.attributes["claude_agent.duration_api_ms"]).toBe(1200);
      expect(conversation.attributes["claude_agent.time_to_first_token_ms"]).toBe(300);
      expect(conversation.attributes["claude_agent.num_turns"]).toBe(2);
      expect(conversation.attributes["claude_agent.is_error"]).toBe(false);

      for (const span of spans) {
        expect(span.status.code).toBe(SpanStatusCode.OK);
        expect(span.ended).toBe(true);
      }
    });

    it("AC-07: keeps prompt, tool input/output and assistant text off spans by default", async () => {
      const { spans } = await run(simpleToolJourney(), { systemPrompt: "SECRET_SYSTEM_PROMPT_MARKER" });
      const text = allAttributeText(spans);
      for (const marker of [
        "SECRET_PROMPT_MARKER",
        "SECRET_SYSTEM_PROMPT_MARKER",
        TOOL_INPUT_MARKER,
        TOOL_OUTPUT_MARKER,
        ASSISTANT_TEXT_MARKER,
      ]) {
        expect(text).not.toContain(marker);
      }
      for (const span of spans) {
        for (const key of [
          "input.value",
          "output.value",
          "claude_agent.prompt",
          "claude_agent.system_prompt",
          "claude_agent.tool.input",
          "claude_agent.tool.output",
          "claude_agent.message.content",
          "claude_agent.tool.file_path",
        ]) {
          expect(span.attributes[key]).toBeUndefined();
        }
      }
    });

    it("writes content only when hideInputs/hideOutputs are false", async () => {
      const { spans } = await run(
        simpleToolJourney(),
        { systemPrompt: "be brief" },
        { hideInputs: false, hideOutputs: false },
      );
      const conversation = one(spans, "claude_agent.conversation");
      const tool = one(spans, "tool.Read");
      expect(conversation.attributes["input.value"]).toBe(PROMPT);
      expect(conversation.attributes["claude_agent.prompt"]).toBe(PROMPT);
      expect(conversation.attributes["claude_agent.system_prompt"]).toBe("be brief");
      expect(conversation.attributes["output.value"]).toContain(ASSISTANT_TEXT_MARKER);
      expect(tool.attributes["claude_agent.tool.input"]).toBe(JSON.stringify({ file_path: TOOL_INPUT_MARKER }));
      expect(tool.attributes["input.value"]).toBe(JSON.stringify({ file_path: TOOL_INPUT_MARKER }));
      expect(tool.attributes["claude_agent.tool.file_path"]).toBe(TOOL_INPUT_MARKER);
      expect(tool.attributes["claude_agent.tool.output"]).toContain(TOOL_OUTPUT_MARKER);
      expect(tool.attributes["output.value"]).toContain(TOOL_OUTPUT_MARKER);
      const firstTurn = byName(spans, "claude_agent.assistant_turn").find(
        (s) => s.attributes["claude_agent.num_turns"] === 1,
      )!;
      expect(firstTurn.attributes["claude_agent.message.content"]).toBe(
        `Reading the file. ${ASSISTANT_TEXT_MARKER} [Tool: Read]`,
      );
    });

    it("hides inputs but keeps outputs when only hideInputs is true", async () => {
      const { spans } = await run(simpleToolJourney(), {}, { hideInputs: true, hideOutputs: false });
      const text = allAttributeText(spans);
      expect(text).not.toContain("SECRET_PROMPT_MARKER");
      expect(text).not.toContain(TOOL_INPUT_MARKER);
      expect(text).toContain(TOOL_OUTPUT_MARKER);
    });

    it("honours FI_HIDE_INPUTS=false / FI_HIDE_OUTPUTS=false as an opt-in", async () => {
      process.env.FI_HIDE_INPUTS = "false";
      process.env.FI_HIDE_OUTPUTS = "false";
      const { spans } = await run(simpleToolJourney());
      const text = allAttributeText(spans);
      expect(text).toContain("SECRET_PROMPT_MARKER");
      expect(text).toContain(TOOL_OUTPUT_MARKER);
    });

    it.each([" False ", "FALSE"])("treats FI_HIDE_*=%j (trimmed, any case) as the opt-in", async (value) => {
      process.env.FI_HIDE_INPUTS = value;
      process.env.FI_HIDE_OUTPUTS = value;
      const { spans } = await run(simpleToolJourney());
      const text = allAttributeText(spans);
      expect(text).toContain("SECRET_PROMPT_MARKER");
      expect(text).toContain(TOOL_OUTPUT_MARKER);
    });

    it.each(["1", "yes", " true", "TRUE", "", "0", "no", "off"])(
      "keeps content hidden for FI_HIDE_*=%j: only an explicit 'false' opts in",
      async (value) => {
        process.env.FI_HIDE_INPUTS = value;
        process.env.FI_HIDE_OUTPUTS = value;
        const { spans } = await run(simpleToolJourney());
        const text = allAttributeText(spans);
        for (const marker of ["SECRET_PROMPT_MARKER", TOOL_INPUT_MARKER, TOOL_OUTPUT_MARKER, ASSISTANT_TEXT_MARKER]) {
          expect(text).not.toContain(marker);
        }
      },
    );

    it("AC-08: yields the same message objects, in order, and passes params through unchanged", async () => {
      const messages = simpleToolJourney();
      const expected = JSON.parse(JSON.stringify(messages));
      const abortController = new AbortController();
      const options = {
        resume: undefined,
        allowedTools: ["Read"],
        permissionMode: "default",
        maxTurns: 3,
        hooks: {},
        mcpServers: {},
        agents: {},
        abortController,
        env: { PATH: "/usr/bin" },
      };
      const optionsBefore = { ...options, env: { ...options.env } };
      const { yielded, fake, params } = await run(messages, options);

      expect(yielded).toHaveLength(messages.length);
      yielded.forEach((message, index) => expect(message).toBe(messages[index]));
      expect(JSON.parse(JSON.stringify(yielded))).toEqual(expected);

      expect(fake.control.calls).toHaveLength(1);
      expect(fake.control.calls[0]).toBe(params);
      expect(fake.control.calls[0].options).toBe(options);
      expect(options).toEqual(optionsBefore);
      expect(options.env).not.toHaveProperty("ANTHROPIC_BASE_URL");
    });

    it("forwards Query control methods to the original object", async () => {
      const { provider } = memoryProvider();
      const fake = makeFakeQuery(simpleToolJourney());
      const traced = wrapQuery(fake.query, { tracerProvider: provider });
      const q = traced({ prompt: PROMPT });
      await q.interrupt();
      expect(fake.control.interruptCalls).toBe(1);
      await drain(q);
    });
  });

  describe("J2: subagent (AC-01, AC-04)", () => {
    it("nests the subagent under the Agent tool span and the subagent's turns and tools under the subagent", async () => {
      const { spans } = await run(subagentJourney());
      const conversation = one(spans, "claude_agent.conversation");
      const agentTool = one(spans, "tool.Agent");
      const subagent = one(spans, "claude_agent.subagent.code-reviewer");
      const grep = one(spans, "tool.Grep");
      const turns = byName(spans, "claude_agent.assistant_turn");
      const mainTurns = turns.filter((t) => t.attributes["claude_agent.parent_tool_use_id"] === undefined);
      const subTurns = turns.filter((t) => t.attributes["claude_agent.parent_tool_use_id"] === AGENT_TOOL_ID);

      expect(mainTurns).toHaveLength(2);
      expect(subTurns).toHaveLength(2);
      expect(new Set(spans.map((s) => s.spanContext().traceId)).size).toBe(1);

      const issuingTurn = mainTurns.find((t) => t.attributes["claude_agent.message.has_tool_use"] === true)!;
      expect(parentId(agentTool)).toBe(issuingTurn.spanContext().spanId);
      expect(parentId(issuingTurn)).toBe(conversation.spanContext().spanId);
      expect(parentId(subagent)).toBe(agentTool.spanContext().spanId);
      for (const turn of subTurns) {
        expect(parentId(turn)).toBe(subagent.spanContext().spanId);
      }
      const grepTurn = subTurns.find((t) => t.attributes["claude_agent.message.has_tool_use"] === true)!;
      expect(parentId(grep)).toBe(grepTurn.spanContext().spanId);

      expect(agentTool.attributes["claude_agent.span_kind"]).toBe("tool_execution");
      expect(agentTool.attributes["claude_agent.tool.source"]).toBe("builtin");
      expect(subagent.attributes["claude_agent.span_kind"]).toBe("subagent");
      expect(subagent.attributes["gen_ai.span.kind"]).toBe("AGENT");
      expect(subagent.attributes["fi.span.kind"]).toBe("AGENT");
      expect(subagent.attributes["claude_agent.subagent.type"]).toBe("code-reviewer");
      expect(subagent.attributes["claude_agent.tool.use_id"]).toBe(AGENT_TOOL_ID);
      expect(subagent.attributes["claude_agent.subagent.task_id"]).toBe("task-1");
      expect(subagent.attributes["claude_agent.subagent.status"]).toBe("completed");
      expect(grep.attributes["claude_agent.parent_tool_use_id"]).toBe(AGENT_TOOL_ID);
      expect(grep.attributes["claude_agent.tool.use_id"]).toBe(SUBAGENT_GREP_ID);
      // Content off by default: the subagent prompt and description are inputs.
      expect(subagent.attributes["claude_agent.subagent.prompt"]).toBeUndefined();
      expect(subagent.attributes["claude_agent.subagent.description"]).toBeUndefined();
      expect(allAttributeText(spans)).not.toContain("SECRET_SUBAGENT_PROMPT_MARKER");

      // No child outlives its parent.
      const endMs = (s: ReadableSpan) => s.endTime[0] * 1e3 + s.endTime[1] / 1e6;
      expect(endMs(subagent)).toBeLessThanOrEqual(endMs(agentTool));
      expect(endMs(grep)).toBeLessThanOrEqual(endMs(subagent));
      for (const span of spans) expect(span.status.code).toBe(SpanStatusCode.OK);
    });

    it("keeps a backgrounded subagent open after its tool_result until task_notification", async () => {
      const journey = subagentJourney();
      // Move the tool_result for the Agent tool before the subagent's work and mark it backgrounded.
      const agentResultIndex = journey.findIndex(
        (m) => m.type === "user" && JSON.stringify(m).includes(`"tool_use_id":"${AGENT_TOOL_ID}"`),
      );
      const [agentResult] = journey.splice(agentResultIndex, 1);
      const startedIndex = journey.findIndex((m) => m.type === "system" && m.subtype === "task_started");
      (journey[startedIndex] as { is_backgrounded?: boolean }).is_backgrounded = true;
      journey.splice(startedIndex + 1, 0, agentResult);

      const { spans } = await run(journey);
      const agentTool = one(spans, "tool.Agent");
      const subagent = one(spans, "claude_agent.subagent.code-reviewer");
      const endMs = (s: ReadableSpan) => s.endTime[0] * 1e3 + s.endTime[1] / 1e6;
      expect(endMs(subagent)).toBeGreaterThanOrEqual(endMs(agentTool));
      for (const turn of byName(spans, "claude_agent.assistant_turn").filter(
        (t) => t.attributes["claude_agent.parent_tool_use_id"] === AGENT_TOOL_ID,
      )) {
        expect(parentId(turn)).toBe(subagent.spanContext().spanId);
      }
      expect(subagent.status.code).toBe(SpanStatusCode.OK);
    });

    const endMsOf = (s: ReadableSpan) => s.endTime[0] * 1e3 + s.endTime[1] / 1e6;

    /** Every subagent turn and tool sits under the subagent span and ends before it. */
    function expectSubagentOutlivesItsWork(spans: ReadableSpan[]) {
      const agentTool = one(spans, "tool.Agent");
      const subagent = one(spans, "claude_agent.subagent.code-reviewer");
      const grep = one(spans, "tool.Grep");
      const subTurns = byName(spans, "claude_agent.assistant_turn").filter(
        (t) => t.attributes["claude_agent.parent_tool_use_id"] === AGENT_TOOL_ID,
      );
      expect(subTurns).toHaveLength(2);
      expect(endMsOf(subagent)).toBeGreaterThan(endMsOf(agentTool));
      for (const turn of subTurns) {
        expect(parentId(turn)).toBe(subagent.spanContext().spanId);
        expect(endMsOf(turn)).toBeLessThanOrEqual(endMsOf(subagent));
      }
      expect(endMsOf(grep)).toBeLessThanOrEqual(endMsOf(subagent));
      return subagent;
    }

    it("R6: keeps a subagent moved to the background (task_updated patch.is_backgrounded) open until task_notification", async () => {
      const { provider, exporter } = memoryProvider();
      const fake = makeFakeQuery(backgroundedSubagentJourney(), { delayMs: 2 });
      await drain(wrapQuery(fake.query, { tracerProvider: provider })({ prompt: PROMPT }));
      const subagent = expectSubagentOutlivesItsWork(exporter.getFinishedSpans());
      expect(subagent.status.code).toBe(SpanStatusCode.OK);
      expect(subagent.attributes["claude_agent.subagent.status"]).toBe("completed");
    });

    it("R6: keeps subagents open after the app calls Query.backgroundTasks()", async () => {
      const { provider, exporter } = memoryProvider();
      const fake = makeFakeQuery(backgroundedSubagentJourney({ taskUpdated: false }), { delayMs: 2 });
      const q = wrapQuery(fake.query, { tracerProvider: provider })({ prompt: PROMPT });
      for (let i = 0; i < 4; i += 1) await q.next(); // init, Agent tool_use, task_started, subagent Grep turn
      await expect(q.backgroundTasks()).resolves.toBe(true);
      expect(fake.control.backgroundTasksCalls).toEqual([undefined]);
      await drain(q);
      const subagent = expectSubagentOutlivesItsWork(exporter.getFinishedSpans());
      expect(subagent.status.code).toBe(SpanStatusCode.OK);
    });

    it("R6: treats a subagent as foreground again when backgroundTasks() rejects", async () => {
      const { provider, exporter } = memoryProvider();
      const disabled = new Error("background tasks are disabled");
      const fake = makeFakeQuery(subagentJourney(), { backgroundTasksResult: disabled });
      const q = wrapQuery(fake.query, { tracerProvider: provider })({ prompt: PROMPT });
      for (let i = 0; i < 3; i += 1) await q.next();
      await expect(q.backgroundTasks(AGENT_TOOL_ID)).rejects.toBe(disabled);
      await drain(q);
      const spans = exporter.getFinishedSpans();
      expect(endMsOf(one(spans, "claude_agent.subagent.code-reviewer"))).toBeLessThanOrEqual(
        endMsOf(one(spans, "tool.Agent")),
      );
      expect(one(spans, "claude_agent.subagent.code-reviewer").status.code).toBe(SpanStatusCode.OK);
    });

    it.each(["failed", "stopped"] as const)(
      "marks a foreground subagent ERROR when task_notification reports %s",
      async (status) => {
        const journey = subagentJourney().map((m) =>
          m.type === "system" && m.subtype === "task_notification" ? taskNotification(AGENT_TOOL_ID, status) : m,
        );
        const { spans } = await run(journey);
        const subagent = one(spans, "claude_agent.subagent.code-reviewer");
        expect(subagent.attributes["claude_agent.subagent.status"]).toBe(status);
        expect(subagent.status.code).toBe(SpanStatusCode.ERROR);
        expect(subagent.status.message).toBe(`subagent ${status}`);
      },
    );

    it.each(["failed", "stopped"] as const)(
      "marks a backgrounded subagent ERROR when task_notification reports %s",
      async (status) => {
        const { spans } = await run(backgroundedSubagentJourney({ status }));
        const subagent = one(spans, "claude_agent.subagent.code-reviewer");
        expect(subagent.attributes["claude_agent.subagent.status"]).toBe(status);
        expect(subagent.status.code).toBe(SpanStatusCode.ERROR);
      },
    );

    it("N2: matches a task_notification without tool_use_id (optional, sdk.d.ts:5997) by its task_id", async () => {
      const journey = backgroundedSubagentJourney().map((m) => {
        if (m.type === "system" && m.subtype === "task_notification") {
          const { tool_use_id: _dropped, ...rest } = m as typeof m & { tool_use_id?: string };
          return rest as SDKMessage;
        }
        return m;
      });
      const { spans } = await run(journey);
      const subagent = one(spans, "claude_agent.subagent.code-reviewer");
      expect(subagent.attributes["claude_agent.subagent.status"]).toBe("completed");
      expect(subagent.status.code).toBe(SpanStatusCode.OK);
    });
  });

  describe("J3: MCP tool error and error result", () => {
    it("marks the MCP tool span mcp_tool/TOOL with ERROR and the conversation ERROR", async () => {
      const { spans } = await run(mcpErrorJourney());
      const tool = one(spans, "tool.mcp__docs__search");
      const conversation = one(spans, "claude_agent.conversation");

      expect(tool.attributes["claude_agent.span_kind"]).toBe("mcp_tool");
      expect(tool.attributes["gen_ai.span.kind"]).toBe("TOOL");
      expect(tool.attributes["claude_agent.tool.source"]).toBe("mcp");
      expect(tool.attributes["claude_agent.mcp.server_name"]).toBe("docs");
      expect(tool.attributes["claude_agent.mcp.tool_name"]).toBe("search");
      expect(tool.attributes["claude_agent.tool.is_error"]).toBe(true);
      expect(tool.status.code).toBe(SpanStatusCode.ERROR);
      // Error text is tool output: hidden by default.
      expect(tool.attributes["claude_agent.tool.error_message"]).toBeUndefined();
      expect(tool.status.message).toBe("tool error");

      expect(conversation.status.code).toBe(SpanStatusCode.ERROR);
      expect(conversation.attributes["claude_agent.is_error"]).toBe(true);
      expect(conversation.attributes["claude_agent.error.type"]).toBe("error_max_turns");
      expect(conversation.attributes["claude_agent.error.message"]).toBe("Reached maximum number of turns (3)");
      expect(conversation.attributes["gen_ai.cost.total"]).toBe(0.002);
    });

    it("records the tool error text when outputs are visible", async () => {
      const { spans } = await run(mcpErrorJourney(), {}, { hideOutputs: false });
      const tool = one(spans, "tool.mcp__docs__search");
      expect(tool.attributes["claude_agent.tool.error_message"]).toBe("server unavailable");
      expect(tool.status.message).toBe("server unavailable");
    });

    it("treats mcp__ names from options.mcpServers as MCP even without an init message", async () => {
      const journey = mcpErrorJourney().slice(1);
      const { spans } = await run(journey, { mcpServers: { docs: { type: "stdio", command: "docs" } } });
      expect(one(spans, "tool.mcp__docs__search").attributes["claude_agent.span_kind"]).toBe("mcp_tool");
    });

    it("classifies an unknown tool as custom", async () => {
      const journey = mcpErrorJourney().slice(1); // no init, no options: server unknown
      const { spans } = await run(journey);
      const tool = one(spans, "tool.mcp__docs__search");
      expect(tool.attributes["claude_agent.tool.source"]).toBe("custom");
      expect(tool.attributes["claude_agent.span_kind"]).toBe("tool_execution");
    });
  });

  describe("sessions (AC-05)", () => {
    it("marks a resumed session and keeps the session id", async () => {
      const { spans } = await run(simpleToolJourney(), { resume: SESSION_ID });
      const conversation = one(spans, "claude_agent.conversation");
      expect(conversation.attributes["claude_agent.is_resumed"]).toBe(true);
      expect(conversation.attributes["claude_agent.resume_session_id"]).toBe(SESSION_ID);
      expect(conversation.attributes["claude_agent.session.is_resumed"]).toBe(true);
      expect(conversation.attributes["claude_agent.session.is_new"]).toBe(false);
      expect(conversation.attributes["claude_agent.session.previous_id"]).toBe(SESSION_ID);
      expect(conversation.attributes["session.id"]).toBe(SESSION_ID);
    });

    it("records the fork origin and uses the new session id from init", async () => {
      const origin = "99999999-8888-4777-8666-555555555555";
      const { spans } = await run(simpleToolJourney(), { resume: origin, forkSession: true });
      const conversation = one(spans, "claude_agent.conversation");
      expect(conversation.attributes["claude_agent.session.fork_from"]).toBe(origin);
      expect(conversation.attributes["claude_agent.session.is_new"]).toBe(true);
      expect(conversation.attributes["session.id"]).toBe(SESSION_ID);
      expect(conversation.attributes["claude_agent.session.id"]).toBe(SESSION_ID);
    });

    it("uses Options.sessionId before the init message arrives", async () => {
      const custom = "abcdefab-cdef-4abc-8def-abcdefabcdef";
      const journey = simpleToolJourney().slice(1); // no init message
      for (const message of journey) (message as { session_id: string }).session_id = custom;
      const { spans } = await run(journey, { sessionId: custom });
      for (const span of spans) expect(span.attributes["session.id"]).toBe(custom);
    });

    it("leaves an app-set fi-core session.id alone and adds fi-core context attributes", async () => {
      const { provider, exporter } = memoryProvider();
      const fake = makeFakeQuery(simpleToolJourney());
      const traced = wrapQuery(fake.query, { tracerProvider: provider });
      context.setGlobalContextManager(new StackContextManager());
      try {
        const ctx = setUser(setSession(context.active(), { sessionId: "app-session" }), { userId: "user-1" });
        const stream = context.with(ctx, () => traced({ prompt: PROMPT }));
        await drain(stream);
      } finally {
        context.disable();
      }
      const conversation = one(exporter.getFinishedSpans(), "claude_agent.conversation");
      expect(conversation.attributes["session.id"]).toBe("app-session");
      expect(conversation.attributes["user.id"]).toBe("user-1");
      expect(conversation.attributes["claude_agent.session.id"]).toBe(SESSION_ID);
    });
  });

  describe("failure semantics", () => {
    it("AC-06: abort ends every open span with ERROR and claude_agent.cancelled=true", async () => {
      const { provider, exporter } = memoryProvider();
      const journey = simpleToolJourney();
      // Block right after the tool_use: conversation, turn and tool spans are open.
      const fake = makeFakeQuery(journey, { waitForAbortAt: 3 });
      const traced = wrapQuery(fake.query, { tracerProvider: provider });
      const abortController = new AbortController();
      const stream = traced({ prompt: PROMPT, options: { abortController } });

      const seen: unknown[] = [];
      const consumed = (async () => {
        for await (const message of stream) {
          seen.push(message);
          if (seen.length === 3) setTimeout(() => abortController.abort(), 5);
        }
      })();
      await expect(consumed).rejects.toThrow("aborted");

      const spans = exporter.getFinishedSpans();
      expect(spans.map((s) => s.name).sort()).toEqual(
        ["claude_agent.assistant_turn", "claude_agent.conversation", "tool.Read"].sort(),
      );
      for (const span of spans) {
        expect(span.ended).toBe(true);
        expect(span.status.code).toBe(SpanStatusCode.ERROR);
        expect(span.attributes["claude_agent.cancelled"]).toBe(true);
      }
    });

    it("ends spans and rethrows the same error when the SDK iterator throws", async () => {
      const { provider, exporter } = memoryProvider();
      const boom = new Error("CLI exited with code 1");
      const fake = makeFakeQuery(simpleToolJourney(), { throwAt: 3, error: boom });
      const traced = wrapQuery(fake.query, { tracerProvider: provider });
      await expect(drain(traced({ prompt: PROMPT }))).rejects.toBe(boom);

      const spans = exporter.getFinishedSpans();
      const conversation = one(spans, "claude_agent.conversation");
      expect(conversation.status.code).toBe(SpanStatusCode.ERROR);
      expect(conversation.attributes["claude_agent.error.message"]).toBe("CLI exited with code 1");
      expect(conversation.events.some((e) => e.name === "exception")).toBe(true);
      expect(conversation.attributes["claude_agent.cancelled"]).toBeUndefined();
      expect(spans).toHaveLength(3);
      for (const span of spans) expect(span.ended).toBe(true);
    });

    const ASYNC_DISPOSE =
      (Symbol as unknown as { asyncDispose?: symbol }).asyncDispose ?? Symbol.for("Symbol.asyncDispose");

    it.each(["close", "asyncDispose"] as const)(
      "R3: %s() ends every open span as cancelled, then forwards to the original",
      async (method) => {
        const { provider, exporter } = memoryProvider();
        const fake = makeFakeQuery(simpleToolJourney());
        const traced = wrapQuery(fake.query, { tracerProvider: provider });
        const q = traced({ prompt: PROMPT });
        // init, text, tool_use: the conversation, turn and tool spans are open.
        for (let i = 0; i < 3; i += 1) await q.next();
        expect(exporter.getFinishedSpans()).toHaveLength(0);

        if (method === "close") {
          q.close();
        } else {
          await (q as unknown as Record<symbol, () => Promise<void>>)[ASYNC_DISPOSE]();
        }

        expect(method === "close" ? fake.control.closeCalls : fake.control.disposeCalls).toBe(1);
        const spans = exporter.getFinishedSpans();
        expect(spans.map((s) => s.name).sort()).toEqual(
          ["claude_agent.assistant_turn", "claude_agent.conversation", "tool.Read"].sort(),
        );
        for (const span of spans) {
          expect(span.ended).toBe(true);
          expect(span.status.code).toBe(SpanStatusCode.ERROR);
          expect(span.attributes["claude_agent.cancelled"]).toBe(true);
        }
        // Nothing more is recorded once the query is closed.
        await q.next();
        expect(exporter.getFinishedSpans()).toHaveLength(3);
      },
    );

    it("R3: close() after the stream completed leaves the finished spans as they were", async () => {
      const { provider, exporter } = memoryProvider();
      const fake = makeFakeQuery(simpleToolJourney());
      const q = wrapQuery(fake.query, { tracerProvider: provider })({ prompt: PROMPT });
      await drain(q);
      q.close();
      expect(fake.control.closeCalls).toBe(1);
      const spans = exporter.getFinishedSpans();
      expect(spans).toHaveLength(4);
      for (const span of spans) {
        expect(span.status.code).toBe(SpanStatusCode.OK);
        expect(span.attributes["claude_agent.cancelled"]).toBeUndefined();
      }
    });

    /** Read until the stream has yielded a result message (done is still false). */
    async function readThroughResult(q: AsyncGenerator<SDKMessage, void>): Promise<void> {
      for (;;) {
        const step = await q.next();
        if (step.done) throw new Error("stream ended before a result");
        if (step.value.type === "result") return;
      }
    }

    it.each([
      ["close", "simpleToolJourney"],
      ["asyncDispose", "simpleToolJourney"],
      ["close", "streamingInputJourney"],
      ["asyncDispose", "streamingInputJourney"],
    ] as const)(
      "N1: %s() after a result with nothing in flight ends the conversation OK, not cancelled (%s)",
      async (method, journeyName) => {
        const { provider, exporter } = memoryProvider();
        const journey = journeyName === "simpleToolJourney" ? simpleToolJourney() : streamingInputJourney();
        const fake = makeFakeQuery(journey);
        const q = wrapQuery(fake.query, { tracerProvider: provider })({ prompt: PROMPT });
        await readThroughResult(q);
        expect(exporter.getFinishedSpans().some((s) => s.name === "claude_agent.conversation")).toBe(false);

        if (method === "close") {
          q.close();
        } else {
          await (q as unknown as Record<symbol, () => Promise<void>>)[ASYNC_DISPOSE]();
        }

        const spans = exporter.getFinishedSpans();
        const conversation = one(spans, "claude_agent.conversation");
        expect(conversation.status.code).toBe(SpanStatusCode.OK);
        expect(conversation.attributes["claude_agent.cancelled"]).toBeUndefined();
        for (const span of spans) {
          expect(span.ended).toBe(true);
          expect(span.attributes["claude_agent.cancelled"]).toBeUndefined();
        }
      },
    );

    /** Streaming input: turn 1 ends with a result, turn 2 is mid-tool-loop (its turn span already ended). */
    function secondTurnBetweenModelSteps(): SDKMessage[] {
      return [
        init(),
        assistant("msg_s1", [text("First answer.")]),
        resultSuccess("First answer.", { num_turns: 1 }),
        assistant("msg_s2", [toolUse("toolu_read_2", "Read", { file_path: "/tmp/b.md" })]),
        toolResult("toolu_read_2", "# b"),
        assistant("msg_s3", [text("Second answer.")]),
        resultSuccess("Second answer.", { num_turns: 2 }),
      ];
    }

    it.each(["close", "asyncDispose", "abort"] as const)(
      "M1: %s() in a later streaming turn, between model steps, stays cancelled",
      async (method) => {
        const { provider, exporter } = memoryProvider();
        const abortController = new AbortController();
        const fake = makeFakeQuery(secondTurnBetweenModelSteps());
        const q = wrapQuery(fake.query, { tracerProvider: provider })({ prompt: PROMPT, options: { abortController } });
        // Read through turn 2's tool_result: the turn span has ended, the next model step has no span yet.
        for (;;) {
          const step = await q.next();
          if (step.done) throw new Error("stream ended early");
          if (step.value.type === "user") break;
        }
        if (method === "close") q.close();
        else if (method === "asyncDispose") await (q as unknown as Record<symbol, () => Promise<void>>)[ASYNC_DISPOSE]();
        else abortController.abort();

        const conversation = one(exporter.getFinishedSpans(), "claude_agent.conversation");
        expect(conversation.status.code).toBe(SpanStatusCode.ERROR);
        expect(conversation.attributes["claude_agent.cancelled"]).toBe(true);
      },
    );

    it("M1: close() after a background subagent finishes post-result (its messages arrive after the result) ends OK", async () => {
      const { provider, exporter } = memoryProvider();
      const base = backgroundedSubagentJourney();
      const result = base[base.length - 1];
      const idx = base.findIndex((m) => m.type === "assistant" && m.message.id === "msg_13");
      // Main loop answers and yields its result; the background subagent's tool_result,
      // final turn and task_notification arrive afterwards.
      const journey = [...base.slice(0, idx + 1), result, ...base.slice(idx + 1, base.length - 1)];
      const fake = makeFakeQuery(journey);
      const q = wrapQuery(fake.query, { tracerProvider: provider })({ prompt: PROMPT });
      for (;;) {
        const step = await q.next();
        if (step.done) throw new Error("stream ended early");
        if (step.value.type === "system" && step.value.subtype === "task_notification") break;
      }
      q.close();
      const spans = exporter.getFinishedSpans();
      const conversation = one(spans, "claude_agent.conversation");
      expect(conversation.status.code).toBe(SpanStatusCode.OK);
      expect(conversation.attributes["claude_agent.cancelled"]).toBeUndefined();
      expect(one(spans, "claude_agent.subagent.code-reviewer").status.code).toBe(SpanStatusCode.OK);
    });

    it("N1: close() after a result while a background subagent still runs stays cancelled", async () => {
      const { provider, exporter } = memoryProvider();
      // The result arrives before the background subagent's task_notification.
      const journey = backgroundedSubagentJourney().filter(
        (m) => !(m.type === "system" && m.subtype === "task_notification"),
      );
      const fake = makeFakeQuery(journey);
      const q = wrapQuery(fake.query, { tracerProvider: provider })({ prompt: PROMPT });
      await readThroughResult(q);
      q.close();
      const spans = exporter.getFinishedSpans();
      const subagent = one(spans, "claude_agent.subagent.code-reviewer");
      expect(subagent.attributes["claude_agent.cancelled"]).toBe(true);
      const conversation = one(spans, "claude_agent.conversation");
      expect(conversation.status.code).toBe(SpanStatusCode.ERROR);
      expect(conversation.attributes["claude_agent.cancelled"]).toBe(true);
    });

    it("ends open spans when the consumer breaks out early", async () => {
      const { provider, exporter } = memoryProvider();
      const fake = makeFakeQuery(simpleToolJourney());
      const traced = wrapQuery(fake.query, { tracerProvider: provider });
      for await (const message of traced({ prompt: PROMPT })) {
        if (message.type === "assistant") break;
      }
      const spans = exporter.getFinishedSpans();
      expect(spans.map((s) => s.name).sort()).toEqual(["claude_agent.assistant_turn", "claude_agent.conversation"]);
    });

    it("does not reject the iterator when the exporter fails (collector down)", async () => {
      const failing: SpanExporter = {
        // code 1 is ExportResultCode.FAILED (@opentelemetry/core)
        export: (_spans, done) => done({ code: 1, error: new Error("ECONNREFUSED") }),
        shutdown: async () => undefined,
      };
      const provider = new BasicTracerProvider({ spanProcessors: [new BatchSpanProcessor(failing)] });
      const fake = makeFakeQuery(simpleToolJourney());
      const traced = wrapQuery(fake.query, { tracerProvider: provider });
      const messages = await drain(traced({ prompt: PROMPT }));
      expect(messages).toHaveLength(simpleToolJourney().length);
      await expect(shutdown(provider)).resolves.toBeUndefined();
    });

    it("AC-10: shutdown() flushes a batch processor before a short script exits", async () => {
      const exporter = new InMemorySpanExporter();
      const provider = new BasicTracerProvider({
        spanProcessors: [new BatchSpanProcessor(exporter, { scheduledDelayMillis: 60_000 })],
      });
      const instrumentation = new ClaudeAgentSDKInstrumentation({ tracerProvider: provider });
      const traced = instrumentation.wrapQuery(makeFakeQuery(simpleToolJourney()).query);
      await drain(traced({ prompt: PROMPT }));
      expect(exporter.getFinishedSpans()).toHaveLength(0);
      await shutdown();
      expect(exporter.getFinishedSpans()).toHaveLength(4);
    });
  });

  describe("provider name", () => {
    it.each([
      [undefined, "anthropic"],
      ["https://api.anthropic.com", "anthropic"],
      ["http://127.0.0.1:8080", "custom"],
      ["https://gateway.example.com/anthropic", "custom"],
    ])("ANTHROPIC_BASE_URL=%s in options.env -> %s", async (baseUrl, expected) => {
      const env = baseUrl ? { PATH: "/usr/bin", ANTHROPIC_BASE_URL: baseUrl } : { PATH: "/usr/bin" };
      const { spans } = await run(simpleToolJourney(), { env });
      expect(one(spans, "claude_agent.conversation").attributes["gen_ai.provider.name"]).toBe(expected);
    });

    it("reads process.env when options.env is absent", async () => {
      process.env.ANTHROPIC_BASE_URL = "http://localhost:4000";
      const { spans } = await run(simpleToolJourney());
      expect(one(spans, "claude_agent.conversation").attributes["gen_ai.provider.name"]).toBe("custom");
    });
  });

  it("never copies FI_API_KEY / FI_SECRET_KEY onto spans", async () => {
    process.env.FI_API_KEY = "fi-api-key-PLACEHOLDER-123";
    process.env.FI_SECRET_KEY = "fi-secret-key-PLACEHOLDER-456";
    try {
      const { spans } = await run(subagentJourney(), {}, { hideInputs: false, hideOutputs: false });
      const text = allAttributeText(spans);
      expect(text).not.toContain("fi-api-key-PLACEHOLDER-123");
      expect(text).not.toContain("fi-secret-key-PLACEHOLDER-456");
    } finally {
      delete process.env.FI_API_KEY;
      delete process.env.FI_SECRET_KEY;
    }
  });

  it("does not double-wrap and uses the global provider when none is given", async () => {
    const { provider, exporter } = memoryProvider();
    trace.setGlobalTracerProvider(provider);
    try {
      const fake = makeFakeQuery(simpleToolJourney());
      const once = wrapQuery(fake.query);
      const twice = wrapQuery(once);
      expect(isWrappedQuery(once)).toBe(true);
      expect(twice).toBe(once);
      await drain(twice({ prompt: PROMPT }));
      expect(exporter.getFinishedSpans()).toHaveLength(4);
    } finally {
      trace.disable();
    }
  });

  it("does not start spans for a query that is never iterated", async () => {
    const { provider, exporter } = memoryProvider();
    const traced = wrapQuery(makeFakeQuery(simpleToolJourney()).query, { tracerProvider: provider });
    traced({ prompt: PROMPT });
    expect(exporter.getFinishedSpans()).toHaveLength(0);
  });

  describe("streaming input (one query(), two user turns)", () => {
    async function* userTurns() {
      yield { type: "user" as const, message: { role: "user" as const, content: "one" }, parent_tool_use_id: null };
      yield { type: "user" as const, message: { role: "user" as const, content: "two" }, parent_tool_use_id: null };
    }
    const hrMs = (t: [number, number]) => t[0] * 1e3 + t[1] / 1e6;

    it("R5: starts the second turn after the first result, so the turns do not overlap", async () => {
      const { provider, exporter } = memoryProvider();
      const fake = makeFakeQuery(streamingInputJourney(), { delayMs: 5 });
      await drain(wrapQuery(fake.query, { tracerProvider: provider })({ prompt: userTurns() }));
      const spans = exporter.getFinishedSpans();
      const conversation = one(spans, "claude_agent.conversation");
      const [first, second] = byName(spans, "claude_agent.assistant_turn").sort(
        (a, b) => hrMs(a.startTime) - hrMs(b.startTime),
      );
      expect(first.attributes["claude_agent.num_turns"]).toBe(1);
      expect(second.attributes["claude_agent.num_turns"]).toBe(2);
      expect(hrMs(first.startTime)).toBe(hrMs(conversation.startTime));
      expect(hrMs(second.startTime)).toBeGreaterThanOrEqual(hrMs(first.endTime));
      expect(hrMs(second.endTime)).toBeLessThanOrEqual(hrMs(conversation.endTime));
    });
  });

  it("does not trace a streaming-input prompt's content", async () => {
    const { provider, exporter } = memoryProvider();
    const traced = wrapQuery(makeFakeQuery(simpleToolJourney()).query, {
      tracerProvider: provider,
      traceConfig: { hideInputs: false },
    });
    async function* input() {
      yield { type: "user" as const, message: { role: "user" as const, content: "hi" }, parent_tool_use_id: null };
    }
    await drain(traced({ prompt: input() }));
    const conversation = one(exporter.getFinishedSpans(), "claude_agent.conversation");
    expect(conversation.attributes["input.value"]).toBeUndefined();
  });

  it("closes a tool span that never got a result as not completed", async () => {
    const journey = simpleToolJourney().filter((m) => m.type !== "user");
    const { spans } = await run(journey);
    const tool = one(spans, "tool.Read");
    expect(tool.status.code).toBe(SpanStatusCode.ERROR);
    expect(tool.status.message).toBe("Tool span not completed (conversation ended)");
    expect(tool.attributes["claude_agent.cancelled"]).toBeUndefined();
  });

  it("keeps MCP_TOOL_ID fixture id stable", () => {
    expect(MCP_TOOL_ID).toBe("toolu_mcp_1");
  });
});
