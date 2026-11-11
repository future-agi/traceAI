import { context, trace, SpanKind, SpanStatusCode } from "@opentelemetry/api";
import { isTracingSuppressed } from "@opentelemetry/core";
import { type FITracer } from "@traceai/fi-core";
import { ProviderMediaCapture, attachmentsActive, speechMediaRoute, type ProviderMediaOptions } from "@traceai/provider-media";
import { observeAPIPromise } from "./apiPromise";

type Create = (this: unknown, ...args: unknown[]) => Promise<unknown>;
type Resource = { create: Create };
type SDK = { Audio?: { Transcriptions?: { prototype: Resource }; Translations?: { prototype: Resource }; Speech?: { prototype: Resource } } };
const owners = new WeakMap<Resource, { owner: object; original: Create; wrapped: Create }>();
function resources(sdk: object): Array<[Resource | undefined, string]> {
  const audio = (sdk as SDK).Audio;
  return [[audio?.Transcriptions?.prototype, "transcription"], [audio?.Translations?.prototype, "translation"], [audio?.Speech?.prototype, "speech"]];
}

/** Speech owns no new recorder. It observes input Blobs and explicitly consumed response bytes. */
export function patchSpeech(sdk: object, owner: object, tracer: () => FITracer, options: () => ProviderMediaOptions, enabled: () => boolean): void {
  for (const [resource, operation] of resources(sdk)) {
    if (!resource || typeof resource.create !== "function" || owners.has(resource)) continue;
    const original = resource.create;
    const wrapped: Create = function (...args) {
      const mediaOptions = options();
      // Attachments OFF (the default) leaves audio calls exactly as before: no span, no wrapper work.
      if (!enabled() || isTracingSuppressed(context.active()) || !attachmentsActive(mediaOptions.attachments)) return original.apply(this, args);
      const span = tracer().startSpan(`OpenAI Audio ${operation}`, { kind: SpanKind.CLIENT, attributes: {
        "fi.span.kind": "LLM", "gen_ai.operation.name": operation,
      } });
      // Provider bytes and filenames are not span attributes. The existing provider
      // manager supplies policy, transport credentials and lifecycle to this hook.
      const media = new ProviderMediaCapture(span, speechMediaRoute(this) ? mediaOptions : {});
      if (operation !== "speech") {
        try { media.speechInput((args[0] as { file?: unknown } | undefined)?.file); } catch { /* No payload diagnostics. */ }
      }
      let ended = false;
      const finish = (failed: boolean) => {
        if (ended) return;
        ended = true;
        if (failed) span.setAttribute("error.type", "provider_error");
        span.setStatus({ code: failed ? SpanStatusCode.ERROR : SpanStatusCode.OK });
        span.end();
      };
      const observe = <T>(result: T): T => {
        if (operation === "speech") media.speechResponse(result);
        finish(false);
        return result;
      };
      try {
        const result = context.with(trace.setSpan(context.active(), span), () => original.apply(this, args));
        return observeAPIPromise(result, observe, () => finish(true), response => {
          if (response.status < 400) observe(response);
          else finish(true);
        });
      } catch (error) { finish(true); throw error; }
    };
    resource.create = wrapped;
    owners.set(resource, { owner, original, wrapped });
  }
}

export function unpatchSpeech(sdk: object, owner: object): void {
  for (const [resource] of resources(sdk)) {
    if (!resource) continue;
    const state = owners.get(resource);
    if (state?.owner !== owner || resource.create !== state.wrapped) continue;
    resource.create = state.original;
    owners.delete(resource);
  }
}
