import {
  resolveAuth,
  resolveEndpoint,
  resolveResourceAttributes,
  createFIMastraExporter,
  createFIObservability,
} from "../FIObservability";
import { FIMastraSpanExporter } from "../FIMastraSpanExporter";

const ENV_KEYS = ["FI_API_KEY", "FI_SECRET_KEY", "FI_BASE_URL", "FI_PROJECT_NAME"];
const saved: Record<string, string | undefined> = {};

beforeEach(() => {
  for (const k of ENV_KEYS) {
    saved[k] = process.env[k];
    delete process.env[k];
  }
});

afterEach(() => {
  for (const k of ENV_KEYS) {
    if (saved[k] === undefined) delete process.env[k];
    else process.env[k] = saved[k];
  }
});

describe("resolveAuth", () => {
  it("throws when credentials are missing", () => {
    expect(() => resolveAuth({})).toThrow(/Missing Future AGI credentials/);
  });

  it("reads credentials from options", () => {
    expect(resolveAuth({ apiKey: "a", secretKey: "b" })).toEqual({
      apiKey: "a",
      secretKey: "b",
    });
  });

  it("reads credentials from the environment", () => {
    process.env.FI_API_KEY = "ea";
    process.env.FI_SECRET_KEY = "es";
    expect(resolveAuth({})).toEqual({ apiKey: "ea", secretKey: "es" });
  });
});

describe("resolveEndpoint", () => {
  it("appends the traces path to baseUrl", () => {
    expect(resolveEndpoint({ baseUrl: "https://x.com" })).toBe(
      "https://x.com/tracer/v1/traces",
    );
  });

  it("strips a trailing slash from baseUrl", () => {
    expect(resolveEndpoint({ baseUrl: "https://x.com/" })).toBe(
      "https://x.com/tracer/v1/traces",
    );
  });

  it("uses an explicit endpoint verbatim", () => {
    expect(resolveEndpoint({ endpoint: "https://x.com/custom" })).toBe(
      "https://x.com/custom",
    );
  });

  it("falls back to FI_BASE_URL", () => {
    process.env.FI_BASE_URL = "https://env.example.com";
    expect(resolveEndpoint({})).toBe(
      "https://env.example.com/tracer/v1/traces",
    );
  });

  it("defaults to api.futureagi.com (not app.futureagi.com)", () => {
    expect(resolveEndpoint({})).toBe(
      "https://api.futureagi.com/tracer/v1/traces",
    );
  });
});

describe("resolveResourceAttributes", () => {
  it("sets project_name and defaults project_type to observe", () => {
    expect(resolveResourceAttributes({ projectName: "p" })).toEqual({
      project_name: "p",
      project_type: "observe",
    });
  });

  it("omits project_name when none is available", () => {
    expect(resolveResourceAttributes({})).toEqual({ project_type: "observe" });
  });

  it("honors projectType experiment", () => {
    expect(
      resolveResourceAttributes({ projectName: "p", projectType: "experiment" }),
    ).toMatchObject({ project_type: "experiment" });
  });

  it("reads FI_PROJECT_NAME from the environment", () => {
    process.env.FI_PROJECT_NAME = "env-project";
    expect(resolveResourceAttributes({})).toMatchObject({
      project_name: "env-project",
    });
  });

  it("merges custom resourceAttributes", () => {
    expect(
      resolveResourceAttributes({
        projectName: "p",
        resourceAttributes: { foo: "bar" },
      }),
    ).toMatchObject({ foo: "bar" });
  });
});

describe("createFIMastraExporter", () => {
  it("throws without credentials", () => {
    expect(() => createFIMastraExporter({})).toThrow(
      /Missing Future AGI credentials/,
    );
  });

  it("returns an FIMastraSpanExporter named 'future-agi'", async () => {
    const exporter = createFIMastraExporter({ apiKey: "a", secretKey: "b" });
    expect(exporter).toBeInstanceOf(FIMastraSpanExporter);
    expect(exporter.name).toBe("future-agi");
    await exporter.shutdown();
  });
});

describe("createFIObservability", () => {
  it("returns an Observability instance", async () => {
    const observability = createFIObservability({
      apiKey: "a",
      secretKey: "b",
      serviceName: "svc",
    });
    expect(observability).toBeDefined();
    expect(typeof observability.shutdown).toBe("function");
    await observability.shutdown();
  });

  it("throws without credentials", () => {
    expect(() => createFIObservability({ serviceName: "svc" })).toThrow(
      /Missing Future AGI credentials/,
    );
  });
});
