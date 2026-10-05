/**
 * Collector contract that the shared Python Receiver cannot check (it drops
 * resource attributes and headers): the real fi-core register() + OTLP/HTTP
 * exporter posts to {endpoint}/tracer/v1/traces with X-Api-Key / X-Secret-Key,
 * and the resource carries project_name and project_type=observe.
 *
 * Loopback only; placeholder keys; no Anthropic call.
 */
import { createServer, IncomingHttpHeaders, Server } from "http";
import { AddressInfo } from "net";
import { ProjectType, register } from "@traceai/fi-core";
import { shutdown, wrapQuery } from "../index";
import { makeFakeQuery } from "./fixtures/fakeQuery";
import { PROMPT, simpleToolJourney } from "./fixtures/messages";
import { drain } from "./helpers";

interface Captured {
  path: string;
  headers: IncomingHttpHeaders;
  body: Buffer;
}

const API_KEY = "test-api-key-PLACEHOLDER";
const SECRET_KEY = "test-secret-key-PLACEHOLDER";

type OtlpValue = { stringValue?: string; intValue?: string | number; doubleValue?: number; boolValue?: boolean };
type OtlpAttr = { key: string; value: OtlpValue };

function attrMap(attributes: OtlpAttr[] = []): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const { key, value } of attributes) {
    out[key] = value.stringValue ?? value.intValue ?? value.doubleValue ?? value.boolValue;
  }
  return out;
}

describe("fi-core export contract", () => {
  let server: Server;
  let origin: string;
  let captured: Captured[];
  const savedEnv: Record<string, string | undefined> = {};

  beforeAll(async () => {
    captured = [];
    server = createServer((req, res) => {
      const chunks: Buffer[] = [];
      req.on("data", (chunk) => chunks.push(chunk));
      req.on("end", () => {
        captured.push({ path: req.url ?? "", headers: req.headers, body: Buffer.concat(chunks) });
        res.writeHead(200, { "content-type": "application/json" });
        res.end("{}");
      });
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
    for (const key of ["FI_API_KEY", "FI_SECRET_KEY", "FI_BASE_URL", "FI_PROJECT_NAME"]) {
      savedEnv[key] = process.env[key];
    }
    process.env.FI_API_KEY = API_KEY;
    process.env.FI_SECRET_KEY = SECRET_KEY;
    delete process.env.FI_BASE_URL;
    delete process.env.FI_PROJECT_NAME;
  });

  afterAll(async () => {
    for (const [key, value] of Object.entries(savedEnv)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    await new Promise<void>((resolve) => server.close(() => resolve()));
  });

  beforeEach(() => {
    captured.length = 0;
  });

  async function runProject(projectName: string) {
    const provider = register({
      projectName,
      projectType: ProjectType.OBSERVE,
      endpoint: origin,
      batch: true,
      setGlobalTracerProvider: false,
    });
    const traced = wrapQuery(makeFakeQuery(simpleToolJourney()).query, { tracerProvider: provider });
    await drain(traced({ prompt: PROMPT }));
    await shutdown(provider);
    await provider.shutdown();
  }

  function decode(request: Captured) {
    expect(String(request.headers["content-type"])).toContain("application/json");
    return JSON.parse(request.body.toString("utf8")) as {
      resourceSpans: { resource: { attributes: OtlpAttr[] }; scopeSpans: { spans: { name: string; attributes: OtlpAttr[] }[] }[] }[];
    };
  }

  it("posts to /tracer/v1/traces with X-Api-Key, X-Secret-Key and the project resource", async () => {
    await runProject("th8235-contract");
    expect(captured.length).toBeGreaterThan(0);
    const names: string[] = [];
    for (const request of captured) {
      expect(request.path).toBe("/tracer/v1/traces");
      expect(request.headers["x-api-key"]).toBe(API_KEY);
      expect(request.headers["x-secret-key"]).toBe(SECRET_KEY);
      expect(request.headers["authorization"]).toBeUndefined();
      for (const resourceSpans of decode(request).resourceSpans) {
        const resource = attrMap(resourceSpans.resource.attributes);
        expect(resource["project_name"]).toBe("th8235-contract");
        expect(resource["project_type"]).toBe("observe");
        expect(resource["openinference.project.name"]).toBeUndefined();
        for (const scope of resourceSpans.scopeSpans) names.push(...scope.spans.map((s) => s.name));
      }
    }
    expect(names.sort()).toEqual(
      ["claude_agent.assistant_turn", "claude_agent.assistant_turn", "claude_agent.conversation", "tool.Read"].sort(),
    );
  });

  it("keeps two projects apart and never puts the API key or secret on a span", async () => {
    await runProject("th8235-project-a");
    await runProject("th8235-project-b");
    const projects = new Set<unknown>();
    for (const request of captured) {
      expect(request.body.toString("utf8")).not.toContain(API_KEY);
      expect(request.body.toString("utf8")).not.toContain(SECRET_KEY);
      for (const resourceSpans of decode(request).resourceSpans) {
        projects.add(attrMap(resourceSpans.resource.attributes)["project_name"]);
        for (const scope of resourceSpans.scopeSpans) {
          for (const span of scope.spans) {
            expect(JSON.stringify(span.attributes)).not.toContain("PLACEHOLDER");
          }
        }
      }
    }
    expect([...projects].sort()).toEqual(["th8235-project-a", "th8235-project-b"]);
  });
});
