/**
 * manuallyInstrument() reads `module.OpenAI`, so at run time it accepts both the SDK's
 * default export (`import OpenAI from "openai"`) and its module namespace
 * (`import * as OpenAI from "openai"`, as the native ESM setup in the docs uses).
 *
 * The first block type-checks both import styles exactly as an application would write
 * them, with the TypeScript compiler, and expects no diagnostics. The second block checks
 * that each style patches chat completions at run time.
 */
import { describe, it, expect, beforeAll, afterAll, afterEach } from "@jest/globals";
import http from "http";
import { AddressInfo } from "net";
import path from "path";
import * as ts from "typescript";
import * as OpenAINamespace from "openai";
import OpenAIDefault from "openai";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { OpenAIInstrumentation, isPatched } from "../instrumentation";

const TYPECHECK_TIMEOUT = 120_000;

// Application files are kept in memory, inside the package, so "openai" resolves from the
// package's own dependencies and "@traceai/openai" resolves to this source tree.
const FIXTURE_ROOT = toPosix(path.resolve(__dirname, "..", "..", "typecheck-fixtures"));
const PACKAGE_ENTRY = toPosix(path.resolve(__dirname, "..", "index"));

const NAMESPACE_IMPORT = `
import * as OpenAI from "openai";
import { OpenAIInstrumentation } from "@traceai/openai";

const openAIInstrumentation = new OpenAIInstrumentation({});
openAIInstrumentation.manuallyInstrument(OpenAI);

const client = new OpenAI.OpenAI();
void client.chat.completions.create({ model: "gpt-4o", messages: [{ role: "user", content: "Hello" }] });
`;

const DEFAULT_IMPORT = `
import OpenAI from "openai";
import { OpenAIInstrumentation } from "@traceai/openai";

const openAIInstrumentation = new OpenAIInstrumentation({});
openAIInstrumentation.manuallyInstrument(OpenAI);

const client = new OpenAI();
void client.chat.completions.create({ model: "gpt-4o", messages: [{ role: "user", content: "Hello" }] });
`;

const NOT_THE_SDK = `
import { OpenAIInstrumentation } from "@traceai/openai";

new OpenAIInstrumentation().manuallyInstrument({});
`;

const shared: ts.CompilerOptions = {
  noEmit: true,
  strict: true,
  skipLibCheck: true,
  types: [],
  paths: { "@traceai/openai": [PACKAGE_ENTRY] },
};

const settings: Record<string, ts.CompilerOptions> = {
  // ESM application, as with bundlers, tsx and editors (app.mjs is checked JavaScript)
  bundler: {
    ...shared,
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Bundler,
    moduleDetection: ts.ModuleDetectionKind.Force,
    allowJs: true,
    checkJs: true,
  },
  // CommonJS application with the settings this package builds with
  commonjs: {
    ...shared,
    target: ts.ScriptTarget.ES2016,
    module: ts.ModuleKind.CommonJS,
    moduleResolution: ts.ModuleResolutionKind.Node10,
    esModuleInterop: true,
  },
};

const applications: Record<string, Record<string, string>> = {
  bundler: {
    "app.mjs": NAMESPACE_IMPORT,
    "namespace.ts": NAMESPACE_IMPORT,
    "default.ts": DEFAULT_IMPORT,
    "not-the-sdk.ts": NOT_THE_SDK,
  },
  commonjs: {
    "namespace.ts": NAMESPACE_IMPORT,
    "default.ts": DEFAULT_IMPORT,
  },
};

function toPosix(fileName: string): string {
  return fileName.split(path.sep).join("/");
}

function describeDiagnostic(diagnostic: ts.Diagnostic): string {
  const message = ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n");
  if (!diagnostic.file || diagnostic.start === undefined) {
    return `error TS${diagnostic.code}: ${message}`;
  }
  const { line, character } = diagnostic.file.getLineAndCharacterOfPosition(diagnostic.start);
  const name = path.posix.relative(FIXTURE_ROOT, diagnostic.file.fileName);
  return `${name}(${line + 1},${character + 1}): error TS${diagnostic.code}: ${message}`;
}

/** Type-checks in-memory application files; returns each file's diagnostics as text */
function typecheck(setting: string): Map<string, string[]> {
  const options = settings[setting];
  const files = new Map(
    Object.entries(applications[setting]).map(([name, text]) => [`${FIXTURE_ROOT}/${setting}/${name}`, text]),
  );
  const host = ts.createCompilerHost(options, true);
  const { fileExists, readFile, getSourceFile } = host;
  host.fileExists = (fileName) => files.has(fileName) || fileExists.call(host, fileName);
  host.readFile = (fileName) => files.get(fileName) ?? readFile.call(host, fileName);
  host.getSourceFile = (fileName, languageVersion, onError, shouldCreate) => {
    const text = files.get(fileName);
    return text === undefined
      ? getSourceFile.call(host, fileName, languageVersion, onError, shouldCreate)
      : ts.createSourceFile(fileName, text, languageVersion, true);
  };
  const program = ts.createProgram({ rootNames: [...files.keys()], options, host });
  const programDiagnostics = [...program.getOptionsDiagnostics(), ...program.getGlobalDiagnostics()];
  const results = new Map<string, string[]>();
  for (const fileName of files.keys()) {
    const sourceFile = program.getSourceFile(fileName);
    if (!sourceFile) throw new Error(`${fileName} was not loaded`);
    const diagnostics = [
      ...programDiagnostics,
      ...program.getSyntacticDiagnostics(sourceFile),
      ...program.getSemanticDiagnostics(sourceFile),
    ];
    results.set(`${setting}/${path.posix.basename(fileName)}`, diagnostics.map(describeDiagnostic));
  }
  return results;
}

describe("manuallyInstrument argument types", () => {
  let diagnostics = new Map<string, string[]>();

  beforeAll(() => {
    diagnostics = new Map([...typecheck("bundler"), ...typecheck("commonjs")]);
  }, TYPECHECK_TIMEOUT);

  it.each([
    ["bundler/app.mjs", "module namespace, checked JavaScript (docs quickstart)"],
    ["bundler/namespace.ts", "module namespace, ESM"],
    ["bundler/default.ts", "default export, ESM"],
    ["commonjs/namespace.ts", "module namespace, CommonJS"],
    ["commonjs/default.ts", "default export, CommonJS"],
  ])("%s type-checks: %s", (file) => {
    expect(diagnostics.get(file)).toEqual([]);
  }, TYPECHECK_TIMEOUT);

  it("still rejects a value that is not the SDK", () => {
    const found = diagnostics.get("bundler/not-the-sdk.ts") ?? [];
    expect(found).toHaveLength(1);
    expect(found[0]).toContain("error TS2345");
  }, TYPECHECK_TIMEOUT);
});

describe("manuallyInstrument at run time", () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  let server: http.Server;
  let baseURL: string;
  let instrumentation: OpenAIInstrumentation | undefined;

  beforeAll(async () => {
    server = http.createServer((req, res) => {
      req.resume();
      req.on("end", () => {
        res.setHeader("content-type", "application/json");
        res.end(JSON.stringify({
          id: "chatcmpl-1",
          object: "chat.completion",
          created: 1,
          model: "gpt-4o",
          choices: [{ index: 0, message: { role: "assistant", content: "Hi" }, finish_reason: "stop" }],
        }));
      });
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    baseURL = `http://127.0.0.1:${(server.address() as AddressInfo).port}/v1`;
  });

  afterEach(() => {
    instrumentation?.disable();
    instrumentation = undefined;
    exporter.reset();
  });

  afterAll(async () => {
    await provider.shutdown();
    await new Promise((resolve) => server.close(resolve));
  });

  it.each([
    { form: "module namespace", sdk: OpenAINamespace, Client: OpenAINamespace.OpenAI },
    { form: "default export", sdk: OpenAIDefault, Client: OpenAIDefault },
  ])("patches chat completions through the $form", async ({ sdk, Client }) => {
    instrumentation = new OpenAIInstrumentation();
    instrumentation.setTracerProvider(provider);
    // The argument types are covered above; this checks only what the call does.
    instrumentation.manuallyInstrument(sdk as unknown as Parameters<OpenAIInstrumentation["manuallyInstrument"]>[0]);
    expect(isPatched()).toBe(true);

    const client = new Client({ apiKey: "test-key", baseURL, maxRetries: 0 });
    await client.chat.completions.create({ model: "gpt-4o", messages: [{ role: "user", content: "Hello" }] });

    expect(exporter.getFinishedSpans().map((span) => span.name)).toEqual(["OpenAI Chat Completions"]);
    instrumentation.disable();
    expect(isPatched()).toBe(false);
  });
});
