/**
 * The wrapper patches the SDK's private `Thread.runStreamedInternal`, the one
 * generator both `run()` and `runStreamed()` drive. The SDK is ESM-only and
 * jest runs CommonJS here, so this reads the installed SDK's source instead of
 * importing it; `contract/run_real_sdk.mjs` imports and runs it for real.
 */
import { readFileSync } from "fs";
import { join } from "path";

// The SDK's exports map has only an `import` condition, so resolve it on disk.
const SDK_DIR = join(__dirname, "../../node_modules/@openai/codex-sdk");

function sdkFile(name: string): string {
  return readFileSync(join(SDK_DIR, "dist", name), "utf8");
}

function methodBody(source: string, signature: RegExp): string {
  const match = signature.exec(source);
  if (!match) throw new Error(`signature not found: ${signature}`);
  let depth = 0;
  for (let i = source.indexOf("{", match.index + match[0].length); i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    if (source[i] === "}") depth -= 1;
    if (depth === 0) return source.slice(match.index, i + 1);
  }
  throw new Error("unbalanced braces");
}

describe("installed @openai/codex-sdk shape", () => {
  it("is the version the package is tested against", () => {
    expect(JSON.parse(readFileSync(join(SDK_DIR, "package.json"), "utf8")).version).toBe("0.162.1");
  });

  const js = sdkFile("index.js");
  const dts = sdkFile("index.d.ts");

  it("Thread has the private runStreamedInternal generator", () => {
    expect(js).toMatch(/async \*runStreamedInternal\(input, turnOptions = \{\}\)/);
    expect(dts).toMatch(/private runStreamedInternal;/);
  });

  it("run() and runStreamed() both drive runStreamedInternal", () => {
    expect(methodBody(js, /async run\(input, turnOptions = \{\}\)/)).toContain("this.runStreamedInternal(input, turnOptions)");
    expect(methodBody(js, /async runStreamed\(input, turnOptions = \{\}\)/)).toContain("this.runStreamedInternal(input, turnOptions)");
  });

  it("thread.started sets the thread id and the options live on _threadOptions", () => {
    const body = methodBody(js, /async \*runStreamedInternal\(input, turnOptions = \{\}\)/);
    expect(body).toContain('parsed.type === "thread.started"');
    expect(body).toContain("this._id = parsed.thread_id");
    expect(body).toContain("this._threadOptions");
  });

  it("declares the event and item types the span model maps", () => {
    for (const type of ["thread.started", "turn.started", "turn.completed", "turn.failed", "item.started", "item.updated", "item.completed", "error"]) {
      expect(dts).toContain(`type: "${type}"`);
    }
    for (const type of ["agent_message", "reasoning", "command_execution", "file_change", "mcp_tool_call", "web_search", "todo_list", "error"]) {
      expect(dts).toContain(`type: "${type}"`);
    }
    for (const field of ["input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens"]) {
      expect(dts).toContain(`${field}: number`);
    }
  });
});
