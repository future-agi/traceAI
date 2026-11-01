import {
  context,
  trace,
  diag,
  SpanKind,
  SpanStatusCode,
  type Context,
  type Span,
  type TracerProvider,
} from "@opentelemetry/api";
import { isTracingSuppressed } from "@opentelemetry/core";
import {
  FITracer,
  generateTraceConfig,
  getAttributesFromContext,
  type TraceConfigOptions,
} from "@traceai/fi-core";
import { SemanticConventions as S } from "@traceai/fi-semantic-conventions";
import { Capture, record } from "./capture.js";
import { VERSION } from "./version.js";
import { wrapAPIPromise } from "./promise.js";
export { VERSION } from "./version.js";

export interface GoogleGenAITracingOptions {
  tracerProvider?: TracerProvider;
  traceConfig?: TraceConfigOptions;
  /** Total captured content budget per call, in characters. Default 16,384; maximum 65,536. */
  maxContentLength?: number;
}
export interface GoogleGenAITracing {
  /** Stops tracing new calls. In-flight calls finish normally. */
  disable(): void;
  enable(): void;
  /** Restores original public methods. Does not shut down the application's provider. */
  dispose(): void;
}

type Method = (this: unknown, ...args: unknown[]) => unknown;
const handles = new WeakMap<object, GoogleGenAITracing>();
const modelMethods = [
  "generateContent",
  "generateContentStream",
  "embedContent",
  "generateImages",
  "editImage",
  "generateVideos",
];
const interactionMethods = ["create", "get", "cancel", "delete"];
// Observability must never replace a provider result/error with a capture or exporter failure.
function safely(fn: () => void): void {
  try {
    fn();
  } catch {
    // Fail open, but never log the caught object: getters/exporters can quote user content.
    try {
      diag.warn(
        "@traceai/google-genai-sdk: telemetry capture failed; SDK behavior is unchanged",
      );
    } catch {
      /* A user-supplied diagnostic logger must not break the SDK either. */
    }
  }
}

/** Instruments one modern @google/genai client in place, including existing chats.
 * Duplicate setup returns the existing handle (its original options win).
 * No global module patch, private SDK field, exporter, or hidden API call is installed.
 */
export function instrumentGoogleGenAI(
  client: { readonly models: object },
  options: GoogleGenAITracingOptions = {},
): GoogleGenAITracing {
  const existing = handles.get(client);
  if (existing) return existing;
  const config = generateTraceConfig(options.traceConfig);
  const tracer = new FITracer({
    tracer: (options.tracerProvider ?? trace.getTracerProvider()).getTracer(
      "@traceai/google-genai-sdk",
      VERSION,
    ),
    traceConfig: config,
  });
  const requested = options.maxContentLength ?? 16384;
  const limit = Number.isFinite(requested)
    ? Math.max(0, Math.min(65536, Math.floor(requested)))
    : 16384;
  let enabled = true;
  let disposed = false;
  const restorers: (() => void)[] = [];
  const patched = new WeakSet<object>();

  function patch(target: object, methods: string[], resource: string): void {
    if (patched.has(target)) return;
    patched.add(target);
    for (const name of methods) {
      const original = Reflect.get(target, name) as unknown;
      if (typeof original !== "function") continue;
      const descriptor = Object.getOwnPropertyDescriptor(target, name);
      // Frozen or non-configurable resources remain usable and unmodified.
      if (
        descriptor &&
        !descriptor.configurable &&
        (!descriptor.writable || !("value" in descriptor))
      )
        continue;
      if (!descriptor && !Object.isExtensible(target)) continue;
      const wrapped: Method = function (...args) {
        const parent = context.active();
        if (!enabled || disposed || isTracingSuppressed(parent))
          return Reflect.apply(original, this, args);
        let span: Span | undefined;
        let capture: Capture | undefined;
        let child = parent;
        safely(() => {
          span = tracer.startSpan(
            `google.genai.${resource}.${name}`,
            {
              kind: SpanKind.CLIENT,
              attributes: {
                ...getAttributesFromContext(parent),
                [S.FI_SPAN_KIND]:
                  name === "embedContent"
                    ? "EMBEDDING"
                    : resource === "interactions" && name !== "create"
                      ? "CHAIN"
                      : "LLM",
                [S.LLM_PROVIDER]: "google",
                [S.GEN_AI_OPERATION_NAME]:
                  name === "generateVideos" ? "submit_video" : name,
              },
            },
            parent,
          );
          child = trace.setSpan(parent, span);
          capture = new Capture(span, config, limit);
          const params = record(args[0]);
          capture.input(params, name);
          if (resource === "interactions" && name !== "create")
            capture.string("google.genai.interaction_id", args[0]);
        });
        let ended = false;
        const abortCleanups: (() => void)[] = [];
        const finish = (error = false, cancelled = false) => {
          if (ended) return;
          ended = true;
          for (const cleanup of abortCleanups) safely(cleanup);
          safely(() => capture?.finish());
          safely(() => {
            if (cancelled) span?.setAttribute("google.genai.cancelled", true);
          });
          // Provider error bodies and stack traces can contain keys or media even when content capture is enabled.
          safely(() => {
            if (error) {
              span?.setAttribute("error.type", "provider_error");
              span?.setStatus({ code: SpanStatusCode.ERROR });
            }
          });
          safely(() => span?.end());
        };
        const watchAbort = (signal: unknown, deferred = false) =>
          safely(() => {
            if (ended || !(signal instanceof AbortSignal)) return;
            const onAbort = () => {
              if (!deferred) return finish(false, true);
              // Legacy stream cleanup also aborts on errors. Let a pending rejection
              // retain its ERROR status before treating an idle controller abort as cancellation.
              const pending = setImmediate(() => finish(false, true));
              abortCleanups.push(() => clearImmediate(pending));
            };
            signal.addEventListener("abort", onAbort, { once: true });
            abortCleanups.push(() =>
              signal.removeEventListener("abort", onAbort),
            );
            if (signal.aborted) onAbort();
          });
        safely(() =>
          watchAbort(
            record(record(args[0]).config).abortSignal ??
              record(args.at(-1)).signal,
          ),
        );
        const observe = (value: unknown) => {
          if (ended) return;
          safely(() => {
            capture?.response(value);
            const status =
              record(record(value).interaction).status ?? record(value).status;
            if (status === "failed")
              span?.setStatus({ code: SpanStatusCode.ERROR });
          });
        };
        const resolvedStreams = new WeakMap<object, object>();
        const resolve = (value: unknown): unknown => {
          try {
            if (
              value &&
              typeof Reflect.get(Object(value), Symbol.asyncIterator) ===
                "function"
            ) {
              const existingStream = resolvedStreams.get(value as object);
              if (existingStream) return existingStream;
              watchAbort(record(record(value).controller).signal, true);
              const wrappedStream = wrapStream(
                value as object,
                child,
                observe,
                finish,
                () => {
                  safely(() =>
                    span?.setAttribute(
                      "google.genai.stream_capture",
                      "request_only",
                    ),
                  );
                  finish();
                },
              );
              resolvedStreams.set(value as object, wrappedStream);
              return wrappedStream;
            }
          } catch {
            /* Fall back to the original value if instrumentation cannot wrap it. */
          }
          observe(value);
          finish();
          return value;
        };
        try {
          const result = context.with(child, () =>
            Reflect.apply(original, this, args),
          );
          if (result && typeof record(result).then === "function") {
            const reject = (error: unknown): never => {
              finish(true);
              throw error;
            };
            if (
              typeof record(result).withResponse === "function" ||
              typeof record(result).asResponse === "function"
            ) {
              return wrapAPIPromise(
                result as object,
                child,
                resolve,
                reject,
                () => {
                  safely(() =>
                    span?.setAttribute(
                      "google.genai.response_capture",
                      "request_only",
                    ),
                  );
                  finish();
                },
              );
            }
            return Promise.resolve(result).then(resolve, reject);
          }
          return resolve(result);
        } catch (error) {
          finish(true);
          throw error;
        }
      };
      Object.defineProperty(
        target,
        name,
        descriptor
          ? { ...descriptor, value: wrapped }
          : { value: wrapped, writable: true, configurable: true },
      );
      restorers.push(() => {
        if (Reflect.get(target, name) !== wrapped) return;
        if (descriptor) Object.defineProperty(target, name, descriptor);
        else Reflect.deleteProperty(target, name);
      });
    }
  }
  try {
    patch(client.models, modelMethods, "models");
    // Preserve the public accessor and resource identity, without private SDK field writes.
    let owner: object | null = client;
    let interactionDescriptor: PropertyDescriptor | undefined;
    while (owner && !interactionDescriptor) {
      interactionDescriptor = Object.getOwnPropertyDescriptor(
        owner,
        "interactions",
      );
      if (!interactionDescriptor) owner = Object.getPrototypeOf(owner);
    }
    if (interactionDescriptor?.get && Object.isExtensible(client)) {
      const own = Object.getOwnPropertyDescriptor(client, "interactions");
      const getter = interactionDescriptor.get;
      const wrappedGetter = function (this: object): unknown {
        const resource = getter.call(this);
        if (
          !disposed &&
          this === client &&
          resource &&
          typeof resource === "object"
        )
          safely(() => patch(resource, interactionMethods, "interactions"));
        return resource;
      };
      Object.defineProperty(client, "interactions", {
        configurable: true,
        enumerable: interactionDescriptor.enumerable,
        get: wrappedGetter,
      });
      // Public accessor only: also cover a resource reference obtained before setup.
      // Unsupported backend/version getters may throw; generation must still work.
      safely(() => wrappedGetter.call(client));
      restorers.push(() => {
        if (
          Object.getOwnPropertyDescriptor(client, "interactions")?.get !==
          wrappedGetter
        )
          return;
        if (own) Object.defineProperty(client, "interactions", own);
        else Reflect.deleteProperty(client, "interactions");
      });
    } else if (
      interactionDescriptor &&
      "value" in interactionDescriptor &&
      interactionDescriptor.value
    ) {
      patch(interactionDescriptor.value, interactionMethods, "interactions");
    }
  } catch (error) {
    for (const restore of restorers.reverse()) safely(restore);
    throw error;
  }
  const handle: GoogleGenAITracing = {
    disable() {
      enabled = false;
    },
    enable() {
      if (!disposed) enabled = true;
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      for (const restore of restorers.reverse()) safely(restore);
      handles.delete(client);
    },
  };
  handles.set(client, handle);
  return handle;
}

/** Pull-driven: do not read ahead, and keep next/return/throw under the call's context. */
function wrapStream(
  target: object,
  child: Context,
  observe: (value: unknown) => void,
  finish: (error?: boolean, cancelled?: boolean) => void,
  requestOnly: () => void,
): object {
  const iterators = new WeakMap<object, object>();
  function proxy(source: object): object {
    const existing = iterators.get(source);
    if (existing) return existing;
    const wrapped = new Proxy(source, {
      get(object, key) {
        const method = Reflect.get(object, key, object);
        if (typeof method !== "function") return method;
        if (key === Symbol.asyncIterator)
          return () => proxy(Reflect.apply(method, object, []));
        if (key === "getReader" || key === "values")
          return (...args: unknown[]) =>
            proxy(Reflect.apply(method, object, args));
        // The legacy helper consumes this[Symbol.asyncIterator], so keep it on our observed path.
        if (key === "toReadableStream") return method.bind(wrapped);
        // Native piping/branching bypasses public reads. Preserve it unchanged and
        // end an explicitly request-only span rather than leave an orphan open forever.
        if (["pipeTo", "pipeThrough", "tee"].includes(String(key)))
          return (...args: unknown[]) => {
            const result = Reflect.apply(method, object, args);
            requestOnly();
            return result;
          };
        if (
          !["next", "read", "return", "throw", "cancel"].includes(String(key))
        )
          return method.bind(object);
        return (...args: unknown[]) => {
          try {
            return Promise.resolve(
              context.with(child, () => Reflect.apply(method, object, args)),
            ).then(
              (result) => {
                if (key === "cancel" || record(result).done)
                  finish(false, key === "return" || key === "cancel");
                else observe(record(result).value);
                return result;
              },
              (error) => {
                finish(true);
                throw error;
              },
            );
          } catch (error) {
            finish(true);
            throw error;
          }
        };
      },
    });
    iterators.set(source, wrapped);
    return wrapped;
  }
  return proxy(target);
}
