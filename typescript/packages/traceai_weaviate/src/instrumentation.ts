import {
  InstrumentationBase,
  InstrumentationConfig,
  InstrumentationNodeModuleDefinition,
} from "@opentelemetry/instrumentation";
import {
  context,
  trace,
  SpanKind,
  SpanStatusCode,
  Attributes,
  Span,
} from "@opentelemetry/api";
import { FITracer, TraceConfigOptions } from "@traceai/fi-core";
import { SemanticConventions } from "@traceai/fi-semantic-conventions";

const VERSION = "0.1.1"; // x-release-please-version
const FACTORIES = [
  "client",
  "connectToLocal",
  "connectToCustom",
  "connectToWeaviateCloud",
  "connectToWCD",
  "connectToWCS",
];
const QUERIES = [
  "nearVector",
  "nearText",
  "hybrid",
  "bm25",
  "fetchObjects",
  "fetchObjectById",
  "nearObject",
];
const WRITES = [
  "insert",
  "insertMany",
  "deleteById",
  "deleteMany",
  "update",
  "replace",
];
const AGGREGATES = [
  "overAll",
  "nearVector",
  "nearText",
  "nearObject",
  "hybrid",
];
// Shared only for duplicate detection, not a global gate that skips other module copies.
const wrappers = new WeakSet<Function>();
const patchedInstances = new WeakSet<WeaviateInstrumentation>();
let patchCount = 0;

export function isPatched(): boolean {
  return patchCount > 0;
}

export interface WeaviateInstrumentationConfig extends InstrumentationConfig {
  captureQueryVectors?: boolean;
  captureResultVectors?: boolean;
  captureDocuments?: boolean;
}

type Target = Record<string, any>;
type Patch = { descriptor?: PropertyDescriptor; wrapper: Function };

function documentProperties(object: Target | undefined): unknown {
  if (!object || typeof object !== "object") return undefined;
  // Match the SDK's data-object envelope without accidentally capturing vectors/references.
  return ["id", "properties", "references", "vectors"].some(
    (key) => key in object,
  )
    ? object.properties
    : object;
}

export class WeaviateInstrumentation extends InstrumentationBase<WeaviateInstrumentationConfig> {
  private _traceConfig?: TraceConfigOptions;
  // Lazy initialization: InstrumentationBase can call enable/patch in super().
  private targets?: Set<WeakRef<Target>>;
  private patches?: WeakMap<Target, Map<string, Patch>>;
  private finalizer?: FinalizationRegistry<WeakRef<Target>>;
  private generation?: number;

  constructor({
    instrumentationConfig,
    traceConfig,
  }: {
    instrumentationConfig?: WeaviateInstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super("@traceai/weaviate", VERSION, { ...instrumentationConfig });
    this._traceConfig = traceConfig;
  }

  public override getConfig(): WeaviateInstrumentationConfig {
    return super.getConfig();
  }

  protected init() {
    return new InstrumentationNodeModuleDefinition(
      "weaviate-client",
      [">=3.0.0 <4"],
      this.patch.bind(this),
      this.unpatch.bind(this),
    );
  }

  public manuallyInstrument(weaviateModule: any): void {
    this.patch(weaviateModule);
  }

  public override disable(): void {
    super.disable();
    // Manual instrumentation must be restored too, including existing collections.
    this.unpatch();
  }

  private patch(moduleExports: Target) {
    if (!this.isEnabled()) return moduleExports;
    let patched = false;
    // CJS namespace, ESM namespace (read-only named exports), or the default object.
    for (const target of new Set([moduleExports, moduleExports?.default])) {
      for (const name of FACTORIES) {
        patched =
          this.wrapFactory(target, name, (client) =>
            this.patchClient(client),
          ) || patched;
      }
    }
    if (patched && !patchedInstances.has(this)) {
      patchedInstances.add(this);
      patchCount++;
    }
    return moduleExports;
  }

  private unpatch(moduleExports?: Target) {
    // Invalidate saved wrappers and in-flight factories before restoring properties.
    this.generation = (this.generation ?? 0) + 1;
    for (const ref of this.targets ?? []) {
      const target = ref.deref();
      if (target) {
        for (const [name, patch] of this.patches?.get(target) ?? []) {
          if (target[name] !== patch.wrapper) continue; // Do not clobber another owner.
          if (patch.descriptor)
            Object.defineProperty(target, name, patch.descriptor);
          else delete target[name];
        }
      }
      this.finalizer?.unregister(ref);
    }
    this.targets?.clear();
    this.patches = new WeakMap();
    if (patchedInstances.delete(this)) patchCount--;
    return moduleExports;
  }

  private wrap(
    target: Target | undefined,
    name: string,
    makeWrapper: (original: Function) => Function,
  ): boolean {
    const original = target?.[name];
    if (!target || typeof original !== "function" || wrappers.has(original))
      return false;
    const descriptor = Object.getOwnPropertyDescriptor(target, name);
    // ESM namespaces have non-configurable exports. Their mutable default object is supported.
    if (
      descriptor &&
      !descriptor.configurable &&
      (!descriptor.writable || descriptor.get)
    )
      return false;
    const wrapper = makeWrapper(original);
    try {
      Object.defineProperty(
        target,
        name,
        descriptor
          ? { ...descriptor, value: wrapper }
          : {
              value: wrapper,
              writable: true,
              configurable: true,
              enumerable: false,
            },
      );
    } catch {
      return false;
    }
    wrappers.add(wrapper);
    this.patches ??= new WeakMap();
    let entries = this.patches.get(target);
    if (!entries) {
      this.patches.set(target, (entries = new Map()));
      this.targets ??= new Set();
      this.finalizer ??= new FinalizationRegistry((ref) =>
        this.targets?.delete(ref),
      );
      const ref = new WeakRef(target);
      this.targets.add(ref);
      this.finalizer.register(target, ref, ref);
    }
    entries.set(name, { descriptor, wrapper });
    return true;
  }

  private wrapFactory(
    target: Target | undefined,
    name: string,
    instrument: (value: any) => void,
  ): boolean {
    const generation = this.generation ?? 0;
    const instrumentation = this;
    return this.wrap(
      target,
      name,
      (original) =>
        function (this: unknown, ...args: any[]) {
          const result = original.apply(this, args);
          const finish = (value: any) => {
            if (
              instrumentation.isEnabled() &&
              generation === (instrumentation.generation ?? 0)
            )
              instrument(value);
            return value;
          };
          // get/use/withTenant/withConsistency must remain synchronous.
          return result && typeof result.then === "function"
            ? result.then(finish)
            : finish(result);
        },
    );
  }

  private patchClient(client: Target) {
    for (const name of [
      "get",
      "use",
      "create",
      "createFromSchema",
      "createFromJson",
    ]) {
      this.wrapFactory(client?.collections, name, (collection) =>
        this.patchCollection(collection),
      );
    }
  }

  private patchCollection(collection: Target) {
    if (!collection) return;
    for (const name of ["withTenant", "withConsistency"]) {
      this.wrapFactory(collection, name, (variant) =>
        this.patchCollection(variant),
      );
    }
    const collectionName =
      typeof collection.name === "string" ? collection.name : "unknown";
    for (const [target, methods, prefix] of [
      [collection.query, QUERIES, ""],
      [collection.data, WRITES, ""],
      [collection.aggregate, AGGREGATES, "aggregate."],
      [collection.aggregate?.groupBy, AGGREGATES, "aggregate.groupBy."],
    ] as const) {
      for (const method of methods) {
        const instrumentation = this;
        const generation = this.generation ?? 0;
        this.wrap(
          target,
          method,
          (original) =>
            function (this: unknown, ...args: any[]) {
              if (
                !instrumentation.isEnabled() ||
                generation !== (instrumentation.generation ?? 0)
              ) {
                return original.apply(this, args);
              }
              return instrumentation.traceOperation(
                this,
                original,
                `${prefix}${method}`,
                collectionName,
                args,
              );
            },
        );
      }
    }
  }

  private inputAttributes(operation: string, args: any[]): Attributes {
    const method = operation.split(".").pop()!;
    const options =
      method === "fetchObjects" || method === "overAll" ? args[0] : args[1];
    const attributes: Attributes = {};
    for (const [key, attribute] of Object.entries({
      limit: "top_k",
      offset: "offset",
      certainty: "certainty",
      distance: "distance",
      alpha: "alpha",
    })) {
      if (typeof options?.[key] === "number")
        attributes[`db.vector.query.${attribute}`] = options[key];
    }
    if (method === "nearVector" && Array.isArray(args[0])) {
      attributes["db.vector.query.dimensions"] = args[0].length;
    }
    if (["insert", "deleteById", "update", "replace"].includes(method))
      attributes["db.operation.batch_size"] = 1;
    if (method === "insertMany" && Array.isArray(args[0]))
      attributes["db.operation.batch_size"] = args[0].length;
    if (method === "deleteMany")
      attributes["db.operation.has_filter"] = args[0] != null;

    const config = this.getConfig();
    const input: Target = {};
    if (
      config.captureDocuments &&
      ["nearText", "hybrid", "bm25"].includes(method)
    )
      input.query = args[0];
    if (
      config.captureDocuments &&
      ["insert", "update", "replace"].includes(method)
    ) {
      input.properties = documentProperties(args[0]);
    }
    if (
      config.captureDocuments &&
      method === "insertMany" &&
      Array.isArray(args[0])
    ) {
      input.properties = args[0].map(documentProperties);
    }
    if (config.captureQueryVectors && method === "nearVector")
      input.vector = args[0];
    if (config.captureQueryVectors && method === "hybrid" && options?.vector)
      input.vector = options.vector;
    if (Object.keys(input).length) {
      attributes[SemanticConventions.INPUT_VALUE] = JSON.stringify(input);
      attributes[SemanticConventions.INPUT_MIME_TYPE] = "application/json";
    }
    return attributes;
  }

  private resultAttributes(span: Span, operation: string, result: any): void {
    if (!result) return;
    if (Array.isArray(result.objects))
      span.setAttribute("db.response.returned_rows", result.objects.length);
    if (operation === "insertMany" && result.uuids) {
      span.setAttribute(
        "db.response.inserted_count",
        Object.keys(result.uuids).length,
      );
      if (result.hasErrors) span.setStatus({ code: SpanStatusCode.ERROR });
    }
    if (typeof result.totalCount === "number")
      span.setAttribute("db.response.total_count", result.totalCount);
    const config = this.getConfig();
    const objects = Array.isArray(result.objects)
      ? result.objects
      : operation === "fetchObjectById"
        ? [result]
        : [];
    if (
      objects.length &&
      (config.captureDocuments || config.captureResultVectors)
    ) {
      span.setAttribute(
        SemanticConventions.OUTPUT_VALUE,
        JSON.stringify(
          objects.map((obj: Target) => ({
            ...(config.captureDocuments ? { properties: obj.properties } : {}),
            ...(config.captureResultVectors ? { vectors: obj.vectors } : {}),
          })),
        ),
      );
      span.setAttribute(
        SemanticConventions.OUTPUT_MIME_TYPE,
        "application/json",
      );
    }
  }

  private traceOperation(
    thisArg: unknown,
    original: Function,
    operation: string,
    collectionName: string,
    args: any[],
  ): any {
    const attributes: Attributes = {
      "db.system": "weaviate",
      "db.operation.name": operation,
      "db.collection.name": collectionName,
      [SemanticConventions.FI_SPAN_KIND]: "VECTOR_DB",
    };
    try {
      Object.assign(attributes, this.inputAttributes(operation, args));
    } catch {
      /* Telemetry must not break an SDK call. */
    }
    // Use the current provider/config, including setTracerProvider after construction.
    const span = new FITracer({
      tracer: this.tracer,
      traceConfig: this._traceConfig,
    }).startSpan(`weaviate ${operation}`, {
      kind: SpanKind.CLIENT,
      attributes,
    });
    const success = (result: any) => {
      span.setStatus({ code: SpanStatusCode.OK });
      try {
        this.resultAttributes(span, operation, result);
      } catch {
        /* Keep the original result. */
      }
      span.end();
      return result;
    };
    const failure = (error: any): never => {
      span.recordException(error);
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: error?.message || "Weaviate operation failed",
      });
      span.end();
      throw error;
    };
    try {
      const result = context.with(trace.setSpan(context.active(), span), () =>
        original.apply(thisArg, args),
      );
      return result && typeof result.then === "function"
        ? result.then(success, failure)
        : success(result);
    } catch (error) {
      return failure(error);
    }
  }
}

export default WeaviateInstrumentation;
