import * as os from "os";
import {
  SHOW_ALL,
  defaultScrubContext,
  redactMessageText,
  resolveContentPolicy,
  scrubContent,
  scrubErrorText,
} from "../privacy";

const ENV_KEYS = [
  "FI_HIDE_INPUTS",
  "FI_HIDE_OUTPUTS",
  "FI_HIDE_INPUT_MESSAGES",
  "FI_HIDE_OUTPUT_MESSAGES",
  "FI_HIDE_INPUT_TEXT",
  "FI_HIDE_OUTPUT_TEXT",
] as const;

describe("resolveContentPolicy (traceAI TraceConfig flags)", () => {
  const saved: Record<string, string | undefined> = {};
  beforeEach(() => {
    for (const key of ENV_KEYS) {
      saved[key] = process.env[key];
      delete process.env[key];
    }
  });
  afterEach(() => {
    for (const key of ENV_KEYS) {
      if (saved[key] === undefined) delete process.env[key];
      else process.env[key] = saved[key];
    }
  });

  it("captures content by default, with or without a traceConfig", () => {
    expect(resolveContentPolicy()).toEqual(SHOW_ALL);
    expect(resolveContentPolicy({})).toEqual(SHOW_ALL);
    expect(Object.values(SHOW_ALL).every((value) => value === false)).toBe(true);
  });

  it("reads every FI_HIDE_* env var even when no traceConfig is passed", () => {
    for (const key of ENV_KEYS) process.env[key] = "true";
    expect(Object.values(resolveContentPolicy()).every((value) => value === true)).toBe(true);
  });

  it("uses fi-core's env rule: only 'true' (any case) hides", () => {
    process.env.FI_HIDE_INPUTS = "TRUE";
    process.env.FI_HIDE_OUTPUTS = "1";
    const policy = resolveContentPolicy();
    expect(policy.hideInputs).toBe(true);
    expect(policy.hideOutputs).toBe(false);
  });

  it("gives traceConfig precedence over the env var", () => {
    process.env.FI_HIDE_INPUTS = "true";
    process.env.FI_HIDE_OUTPUT_TEXT = "false";
    const policy = resolveContentPolicy({ hideInputs: false, hideOutputText: true });
    expect(policy.hideInputs).toBe(false);
    expect(policy.hideOutputText).toBe(true);
  });
});

describe("redactMessageText (FI_HIDE_INPUT_TEXT / FI_HIDE_OUTPUT_TEXT)", () => {
  it("keeps roles and replaces content and parts", () => {
    const value = JSON.stringify([
      { role: "system", content: "s" },
      { role: "user", parts: [{ type: "text", text: "u" }], id: "m1" },
    ]);
    expect(JSON.parse(redactMessageText(value) as string)).toEqual([
      { role: "system", content: "__REDACTED__" },
      { role: "user", parts: "__REDACTED__" },
    ]);
  });

  it("drops anything that is not a message list", () => {
    expect(redactMessageText("plain text")).toBeUndefined();
    expect(redactMessageText('{"role":"user"}')).toBeUndefined();
    expect(redactMessageText(3)).toBeUndefined();
  });
});

describe("scrubContent (paths and stack traces, either mode)", () => {
  const ctx = { cwd: "/Users/someone/app", home: "/Users/someone" };

  it("rewrites the cwd, the home directory and other local paths", () => {
    expect(scrubContent("open /Users/someone/app/src/a.ts", ctx)).toBe("open [cwd]/src/a.ts");
    expect(scrubContent("read /Users/someone/notes.md", ctx)).toBe("read ~/notes.md");
    expect(scrubContent("in /Users/someone/app2/x", ctx)).toBe("in ~/app2/x");
    expect(scrubContent("tmp /tmp/run-1/out.log and /private/var/x", ctx)).toBe("tmp [path] and [path]");
    expect(scrubContent("see file:///etc/hosts", ctx)).toBe("see [path]");
    expect(scrubContent("win C:\\Users\\someone\\a.txt", ctx)).toBe("win [path]");
  });

  it("leaves URLs, routes and relative paths alone", () => {
    const text = "POST http://127.0.0.1:8317/v1/chat/completions then src/a.ts and /v1/messages";
    expect(scrubContent(text, ctx)).toBe(text);
  });

  it("removes V8 stack frames and Python tracebacks", () => {
    const js = "Error: boom\n    at f (/Users/someone/app/a.js:1:2)\n    at g (node:internal/x:3:4)";
    expect(scrubContent(js, ctx)).toBe("Error: boom [stack trace removed]");
    const py = 'oops Traceback (most recent call last):\n  File "/home/x/a.py", line 1\nValueError: v';
    expect(scrubContent(py, ctx)).toBe("oops [stack trace removed]");
  });

  it("scrubs inside JSON (VoltAgent's serialized messages and tool results) and keeps it valid JSON", () => {
    const toolError = { error: true, message: "boom", stack: "Error: boom\n    at f (/Users/someone/app/a.js:1:2)" };
    const messages = JSON.stringify([
      { role: "tool", content: [{ type: "tool-result", output: { type: "json", value: toolError } }] },
      { role: "user", content: "file /Users/someone/app/README.md" },
    ]);
    const out = scrubContent(messages, ctx);
    expect(out).not.toContain("/Users/");
    expect(out).not.toContain("    at ");
    const parsed = JSON.parse(out);
    expect(parsed[0].content[0].output.value.stack).toBe("Error: boom [stack trace removed]");
    expect(parsed[1].content).toBe("file [cwd]/README.md");
    // JSON nested in a JSON string (tool output serialized twice).
    const nested = JSON.stringify({ output: JSON.stringify({ path: "/Users/someone/app/x" }) });
    expect(JSON.parse(JSON.parse(scrubContent(nested, ctx)).output).path).toBe("[cwd]/x");
  });

  it("returns JSON untouched (same formatting) when nothing needs scrubbing", () => {
    const text = '{ "a": 1,  "b": "plain" }';
    expect(scrubContent(text, ctx)).toBe(text);
  });

  it("handles escaped newlines in text that is not valid JSON", () => {
    const broken = '{"stack":"Error: x\\n    at f (/Users/someone/app/a.js:1:2)"'; // truncated JSON
    const out = scrubContent(broken, ctx);
    expect(out).not.toContain("    at ");
    expect(out).not.toContain("/Users/");
  });

  it("defaults to this process's cwd and home directory", () => {
    const real = defaultScrubContext();
    expect(real.cwd).toBe(process.cwd());
    expect(real.home).toBe(os.homedir());
    expect(scrubContent(`${process.cwd()}/x`, real)).toBe("[cwd]/x");
  });
});

describe("scrubErrorText", () => {
  it("also removes emails and this machine's user and host names", () => {
    const user = os.userInfo().username;
    const host = os.hostname();
    const out = scrubErrorText(`denied for ops@example.com as ${user} on ${host} at /home/ci/x`, {});
    expect(out).not.toContain("ops@example.com");
    if (user.length >= 3) expect(out).not.toContain(user);
    if (host.length >= 3 && host !== "localhost") expect(out).not.toContain(host);
    expect(out).toContain("<email>");
    expect(out).toContain("[path]");
  });

  it("keeps localhost and short words", () => {
    expect(scrubErrorText("connect ECONNREFUSED localhost:4318", {})).toBe("connect ECONNREFUSED localhost:4318");
  });
});
