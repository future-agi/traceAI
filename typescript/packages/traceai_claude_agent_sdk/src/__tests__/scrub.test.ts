import * as os from "os";
import { SpanStatusCode } from "@opentelemetry/api";
import { wrapQuery } from "../index";
import { CWD_TOKEN, PATH_REMOVED, STACK_TRACE_REMOVED, scrubContent, scrubErrorText } from "../scrub";
import { makeFakeQuery } from "./fixtures/fakeQuery";
import { MODEL, SESSION_ID, assistant, init, resultSuccess, text, toolResult, toolUse } from "./fixtures/messages";
import { allAttributeText, drain, memoryProvider, one } from "./helpers";

const ctx = { cwd: "/Users/alice/work/proj", home: "/Users/alice" };

describe("scrubContent", () => {
  it("rewrites the cwd to [cwd] and the home directory to ~", () => {
    expect(scrubContent('{"file_path":"/Users/alice/work/proj/src/a.ts"}', ctx)).toBe(
      `{"file_path":"${CWD_TOKEN}/src/a.ts"}`,
    );
    expect(scrubContent("see /Users/alice/.config/x.json", ctx)).toBe("see ~/.config/x.json");
    expect(scrubContent("cwd is /Users/alice/work/proj", ctx)).toBe(`cwd is ${CWD_TOKEN}`);
  });

  it("does not treat a sibling directory as the cwd", () => {
    expect(scrubContent("/Users/alice/work/proj2/a.ts", ctx)).toBe("~/work/proj2/a.ts");
  });

  it("replaces other absolute local paths, file URLs and Windows paths", () => {
    expect(scrubContent("output_file: /private/tmp/claude-502/-Users-bob-x/tasks/a.output", ctx)).toBe(
      `output_file: ${PATH_REMOVED}`,
    );
    expect(scrubContent("open /home/bob/notes.txt now", ctx)).toBe(`open ${PATH_REMOVED} now`);
    expect(scrubContent("at file:///Users/bob/app/index.mjs:3:9", ctx)).toBe(`at ${PATH_REMOVED}`);
    expect(scrubContent('{"p":"C:\\\\Users\\\\bob\\\\a.txt"}', {})).toBe(`{"p":"${PATH_REMOVED}"}`);
  });

  it("leaves URLs, API routes and relative paths alone", () => {
    for (const value of [
      "POST /v1/messages?beta=true",
      "https://example.com/Users/docs",
      "src/index.ts and ./README.md",
      "http://127.0.0.1:8080/v1/messages",
    ]) {
      expect(scrubContent(value, ctx)).toBe(value);
    }
  });

  it("cuts Python tracebacks and V8 stack frames", () => {
    const py = 'failed\nTraceback (most recent call last):\n  File "/home/bob/x.py", line 1, in <module>\nValueError: x';
    expect(scrubContent(py, ctx)).toBe(`failed\n${STACK_TRACE_REMOVED}`);
    const js = "Error: boom\n    at run (/Users/bob/app/x.js:1:2)\n    at main (node:internal/x:3:4)";
    expect(scrubContent(js, ctx)).toBe(`Error: boom\n${STACK_TRACE_REMOVED}`);
  });
});

describe("scrubErrorText", () => {
  it("also removes emails and this machine's user and host names", () => {
    const user = os.userInfo().username;
    const host = os.hostname();
    const out = scrubErrorText(`denied for ${user} on ${host} (mail ops@example.com)`, ctx);
    expect(out).not.toContain(user);
    expect(out).not.toContain(host);
    expect(out).not.toContain("ops@example.com");
    expect(out).toContain("<email>");
  });
});

describe("exported content never carries local paths or stack traces", () => {
  it("rewrites paths in prompt, tool input, tool output and assistant text", async () => {
    const home = os.homedir();
    const cwd = `${home}/th8235-scrub-proj`;
    const toolId = "toolu_scrub_1";
    const messages = [
      init(),
      assistant("msg_s1", [text(`Reading ${cwd}/notes.txt`), toolUse(toolId, "Read", { file_path: `${cwd}/notes.txt` })]),
      toolResult(toolId, `contents of ${home}/secret/notes.txt\nError: x\n    at f (${home}/app/x.js:1:2)`),
      assistant("msg_s2", [text("Done.")]),
      resultSuccess("Done."),
    ];
    const { provider, exporter } = memoryProvider();
    const traced = wrapQuery(makeFakeQuery(messages as never).query, { tracerProvider: provider });
    await drain(traced({ prompt: `Summarize ${cwd}/notes.txt`, options: { cwd } }));
    const spans = exporter.getFinishedSpans();
    const blob = allAttributeText(spans);
    expect(blob).not.toContain(home);
    expect(blob).not.toContain(os.userInfo().username);
    expect(blob).not.toContain("    at ");
    const tool = one(spans, "tool.Read");
    expect(tool.attributes["claude_agent.tool.file_path"]).toBe(`${CWD_TOKEN}/notes.txt`);
    expect(tool.attributes["claude_agent.tool.output"]).toBe(`contents of ~/secret/notes.txt\nError: x\n${STACK_TRACE_REMOVED}`);
    expect(one(spans, "claude_agent.conversation").attributes["input.value"]).toBe(`Summarize ${CWD_TOKEN}/notes.txt`);
    expect(one(spans, "claude_agent.conversation").attributes["session.id"]).toBe(SESSION_ID);
    expect(one(spans, "claude_agent.conversation").attributes["gen_ai.request.model"]).toBe(MODEL);
    for (const span of spans) expect(span.status.code).toBe(SpanStatusCode.OK);
  });
});
