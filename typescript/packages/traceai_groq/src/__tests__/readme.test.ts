import { readFileSync } from "node:fs";
import { join } from "node:path";

const readme = readFileSync(join(__dirname, "../../README.md"), "utf8");

it("uses current model IDs in runnable examples", () => {
  expect(readme).not.toMatch(/mixtral-8x7b-32768|llama3-groq-70b-8192-tool-use-preview|llama-3\.1-70b-versatile|llama3-70b-8192|llama3-8b-8192|gemma2-9b-it/);
  expect(readme).toContain("llama-3.3-70b-versatile");
});

it("names the emitted GenAI attributes instead of retired aliases", () => {
  expect(readme).toContain("`gen_ai.request.model`");
  expect(readme).toContain("`gen_ai.output.messages`");
  expect(readme).toContain("`gen_ai.usage.total_tokens`");
  expect(readme).not.toMatch(/`(?:llm\.|message\.tool_calls)/);
});
