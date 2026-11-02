import {
  OpenAIInstrumentation,
  isPatched as isSharedRoutePatched,
} from "@traceai/openai";

type SharedRouteOptions = ConstructorParameters<typeof OpenAIInstrumentation>[0];
type SharedRouteModule = Parameters<OpenAIInstrumentation["manuallyInstrument"]>[0];

let warned = false;

/**
 * Whether the shared OpenAI-compatible route currently instruments the SDK.
 */
export function isPatched(): boolean {
  return isSharedRoutePatched();
}

/**
 * @deprecated Use `OpenAIInstrumentation` from `@traceai/openai` with a
 * Fireworks `baseURL`. This class delegates to that shared route: it traces
 * every OpenAI SDK client and sets `gen_ai.provider.name=fireworks` for
 * `api.fireworks.ai`. Enabling both produces one span per request.
 */
export class FireworksInstrumentation extends OpenAIInstrumentation {
  constructor(options: SharedRouteOptions = {}) {
    super(options);
    if (!warned) {
      warned = true;
      process.emitWarning(
        "FireworksInstrumentation is deprecated. Use OpenAIInstrumentation from @traceai/openai " +
          "with baseURL https://api.fireworks.ai/inference/v1 instead.",
        { type: "DeprecationWarning", code: "TRACEAI_FIREWORKS_DEPRECATED" },
      );
    }
  }

  /** Instruments an explicitly imported OpenAI SDK module, as ESM applications require. */
  override manuallyInstrument(module: object): void {
    super.manuallyInstrument(module as SharedRouteModule);
  }
}
