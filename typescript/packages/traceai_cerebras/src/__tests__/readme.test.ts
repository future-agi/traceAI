import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import * as ts from "typescript";

const root = resolve(__dirname, "../..");
const snippets = Array.from(
  readFileSync(join(root, "README.md"), "utf8").matchAll(
    /```typescript\n([\s\S]*?)```/g,
  ),
  (match) => match[1],
);

it("type-checks the setup, streaming and response-helper examples against the selected SDK", () => {
  const directory = mkdtempSync(join(root, ".readme-"));
  try {
    const sdk =
      process.env.CEREBRAS_SDK_PATH ||
      dirname(require.resolve("@cerebras/cerebras_cloud_sdk/package.json"));
    const source = snippets
      .slice(0, 3)
      .join("\n")
      .replace(
        '"@cerebras/cerebras_cloud_sdk"',
        JSON.stringify(join(sdk, "index.mjs")),
      )
      .replace(
        '"@traceai/cerebras"',
        JSON.stringify(join(root, "dist/esm/index.js")),
      );
    const filename = join(directory, "examples.mts");
    writeFileSync(filename, source);
    const program = ts.createProgram([filename], {
      strict: true,
      noEmit: true,
      skipLibCheck: true,
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.NodeNext,
      moduleResolution: ts.ModuleResolutionKind.NodeNext,
      types: ["node"],
    });
    const errors = ts
      .getPreEmitDiagnostics(program)
      .map((diagnostic) =>
        ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n"),
      );
    expect(errors).toEqual([]);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

it("runs the streaming example with missing, null and empty choices", async () => {
  const output = jest.fn();
  const client = {
    chat: {
      completions: {
        create: async function* () {
          yield { usage: { total_tokens: 1 } };
          yield { choices: null };
          yield { choices: [] };
          yield { choices: [{ delta: { content: "hello" } }] };
        },
      },
    },
  };
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  const run = new AsyncFunction("client", "model", "process", snippets[1]);
  await run(client, "offline-model", { stdout: { write: output } });
  expect(output.mock.calls.map(([text]) => text).join("")).toBe("hello");
});
