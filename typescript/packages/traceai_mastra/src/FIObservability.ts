import { Observability } from "@mastra/observability";
import { SpanType } from "@mastra/core/observability";
import {
  FIMastraSpanExporter,
  type FISpanExporterConfig,
} from "./FIMastraSpanExporter.js";


const DEFAULT_FI_BASE_URL = "https://api.futureagi.com";
const FI_TRACES_PATH = "/tracer/v1/traces";

export interface FIMastraExporterOptions {
  /** Service name (resource `service.name`). Defaults to `"mastra-app"`. */
  serviceName?: string;
  /** Future AGI API key. Defaults to `process.env.FI_API_KEY`. */
  apiKey?: string;
  /** Future AGI secret key. Defaults to `process.env.FI_SECRET_KEY`. */
  secretKey?: string;
  /**
   * Full traces endpoint URL. When set, overrides `baseUrl`.
   * Defaults to `${baseUrl}/tracer/v1/traces`.
   */
  endpoint?: string;
  /**
   * Base collector URL. The `/tracer/v1/traces` path is appended.
   * Defaults to `process.env.FI_BASE_URL` or `https://api.futureagi.com`.
   */
  baseUrl?: string;
  /** Extra headers merged into the export request. */
  headers?: Record<string, string>;
  /**
   * Future AGI project name. This is the `project_name` resource attribute the
   * Future AGI collector keys on — WITHOUT it, traces are ingested but no project
   * is created and nothing shows in the dashboard. Defaults to `FI_PROJECT_NAME`
   * env, then (via {@link createFIObservability}) the `serviceName`.
   */
  projectName?: string;
  /**
   * Future AGI project type. `"observe"` for continuous tracing (default),
   * `"experiment"` for evaluation runs with project versions.
   */
  projectType?: "observe" | "experiment";
  /** Extra OTel resource attributes merged onto every span's resource. */
  resourceAttributes?: Record<string, string>;
  /** Export request timeout in ms. */
  timeout?: number;
  /** Spans per export batch. */
  batchSize?: number;
}

/** @internal Exported for unit testing; not part of the public package API. */
export function resolveResourceAttributes(
  options: FIMastraExporterOptions,
): Record<string, string> {
  const projectName = options.projectName ?? process.env.FI_PROJECT_NAME;
  return {
    // Future AGI keys the project on these resource attributes.
    ...(projectName ? { project_name: projectName } : {}),
    project_type: options.projectType ?? "observe",
    ...(options.resourceAttributes ?? {}),
  };
}

/** @internal Exported for unit testing; not part of the public package API. */
export function resolveEndpoint(options: FIMastraExporterOptions): string {
  if (options.endpoint) return options.endpoint;
  const base = options.baseUrl ?? process.env.FI_BASE_URL ?? DEFAULT_FI_BASE_URL;
  return base.replace(/\/+$/, "") + FI_TRACES_PATH;
}

/** @internal Exported for unit testing; not part of the public package API. */
export function resolveAuth(options: FIMastraExporterOptions): {
  apiKey: string;
  secretKey: string;
} {
  const apiKey = options.apiKey ?? process.env.FI_API_KEY;
  const secretKey = options.secretKey ?? process.env.FI_SECRET_KEY;
  if (!apiKey || !secretKey) {
    throw new Error(
      "[@traceai/mastra] Missing Future AGI credentials. Set FI_API_KEY and " +
        "FI_SECRET_KEY environment variables, or pass { apiKey, secretKey } to " +
        "createFIMastraExporter()/createFIObservability().",
    );
  }
  return { apiKey, secretKey };
}

/**
 * Create a Mastra v1 observability exporter pre-configured for Future AGI.
 *
 */
export function createFIMastraExporter(
  options: FIMastraExporterOptions = {},
): FIMastraSpanExporter {
  const { apiKey, secretKey } = resolveAuth(options);
  const config: FISpanExporterConfig = {
    endpoint: resolveEndpoint(options),
    headers: {
      "x-api-key": apiKey,
      "x-secret-key": secretKey,
      ...(options.headers ?? {}),
    },
    serviceName: options.serviceName ?? "mastra-app",
    resourceAttributes: resolveResourceAttributes(options),
    ...(options.timeout !== undefined ? { timeout: options.timeout } : {}),
    ...(options.batchSize !== undefined ? { batchSize: options.batchSize } : {}),
  };
  return new FIMastraSpanExporter(config);
}

export interface FIObservabilityOptions extends FIMastraExporterOptions {
  /**
   * Mastra span types to drop before export. Defaults to `[SpanType.MODEL_CHUNK]`
   * — per-chunk streaming spans that are pure noise in an observability backend.
   * Pass `[]` to export everything.
   */
  excludeSpanTypes?: SpanType[];
}

/**
 * Create a ready-to-use Mastra v1 {@link Observability} instance wired to Future AGI.
 *
 * @example
 * ```ts
 * import { Mastra } from "@mastra/core";
 * import { createFIObservability } from "@traceai/mastra";
 *
 * export const mastra = new Mastra({
 *   agents: { ... },
 *   observability: createFIObservability({ serviceName: "my-app" }),
 * });
 * ```
 */
export function createFIObservability(
  options: FIObservabilityOptions = {},
): Observability {
  const {
    serviceName = "mastra-app",
    excludeSpanTypes = [SpanType.MODEL_CHUNK],
    ...exporterOptions
  } = options;
  return new Observability({
    configs: {
      otel: {
        serviceName,
        excludeSpanTypes,
        exporters: [
          createFIMastraExporter({
            ...exporterOptions,
            serviceName,
            // Default the FI project to the service name so a project is created.
            projectName: exporterOptions.projectName ?? serviceName,
          }),
        ],
      },
    },
  });
}
