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

const VERSION = "0.1.2"; // x-release-please-version
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
// All instances patching one SDK export share its wrappers and lifecycle. This also
// lets an enabled instance take over existing clients when another disables.
type PatchGroup = {
  owners: Set<WeaviateInstrumentation>;
  targets: Set<WeakRef<Target>>;
  finalizer: FinalizationRegistry<WeakRef<Target>>;
};
const roots = new WeakMap<Target, PatchGroup>();
const patches = new WeakMap<Target, Map<string, Patch>>();
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
type Patch = {
  descriptor?: PropertyDescriptor;
  wrapper: Function;
  group: PatchGroup;
};

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
  private groups?: Set<PatchGroup>;
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
    if (!this.isEnabled() || !moduleExports) return moduleExports;
    const defaultExport = moduleExports.default;
    const primary = roots.get(moduleExports);
    const secondary =
      defaultExport && typeof defaultExport === "object"
        ? roots.get(defaultExport)
        : undefined;
    // Retired wrappers held by callers must never spring back to life on re-enable.
    const group =
      [primary, secondary].find((candidate) => candidate?.owners.size) ??
      this.newGroup();
    let patched = false;
    // CJS namespace, ESM namespace (read-only named exports), or the default object.
    for (const target of new Set([moduleExports, defaultExport])) {
      for (const name of FACTORIES) {
        patched =
          this.wrapFactory(target, name, group, (owner, client) =>
            owner.patchClient(client, group),
          ) || patched;
      }
    }
    if (patched) {
      if (!this.groups?.size) patchCount++;
      this.groups ??= new Set();
      this.groups.add(group);
      group.owners.add(this);
      roots.set(moduleExports, group);
      if (defaultExport && typeof defaultExport === "object")
        roots.set(defaultExport, group);
    }
    return moduleExports;
  }

  private newGroup(): PatchGroup {
    const group: PatchGroup = {
      owners: new Set(),
      targets: new Set(),
      finalizer: new FinalizationRegistry((ref) => group.targets.delete(ref)),
    };
    return group;
  }

  private unpatch(moduleExports?: Target) {
    // Invalidate in-flight factories; an active co-owner keeps shared wrappers.
    this.generation = (this.generation ?? 0) + 1;
    for (const group of this.groups ?? []) {
      group.owners.delete(this);
      if (group.owners.size) continue;
      for (const ref of group.targets) {
        const target = ref.deref();
        if (target) {
          const entries = patches.get(target);
          for (const [name, patch] of entries ?? []) {
            if (patch.group !== group) continue;
            if (target[name] === patch.wrapper) {
              if (patch.descriptor)
                Object.defineProperty(target, name, patch.descriptor);
              else delete target[name];
            }
            entries?.delete(name);
          }
          if (!entries?.size) patches.delete(target);
        }
        group.finalizer.unregister(ref);
      }
      group.targets.clear();
    }
    if (this.groups?.size) patchCount--;
    this.groups?.clear();
    return moduleExports;
  }

  private wrap(
    target: Target | undefined,
    name: string,
    group: PatchGroup,
    makeWrapper: (original: Function) => Function,
  ): boolean {
    const original = target?.[name];
    if (!target || typeof original !== "function") return false;
    const existing = patches.get(target)?.get(name);
    if (existing && original === existing.wrapper)
      return existing.group === group;
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
    let entries = patches.get(target);
    if (!entries) {
      patches.set(target, (entries = new Map()));
      const ref = new WeakRef(target);
      group.targets.add(ref);
      group.finalizer.register(target, ref, ref);
    } else if (
      !Array.from(entries.values()).some((patch) => patch.group === group)
    ) {
      const ref = new WeakRef(target);
      group.targets.add(ref);
      group.finalizer.register(target, ref, ref);
    }
    entries.set(name, { descriptor, wrapper, group });
    return true;
  }

  private wrapFactory(
    target: Target | undefined,
    name: string,
    group: PatchGroup,
    instrument: (owner: WeaviateInstrumentation, value: any) => void,
  ): boolean {
    return this.wrap(
      target,
      name,
      group,
      (original) =>
        function (this: unknown, ...args: any[]) {
          const owner = Array.from(group.owners).find((entry) =>
            entry.isEnabled(),
          );
          const generation = owner?.generation ?? 0;
          const result = original.apply(this, args);
          const finish = (value: any) => {
            const current =
              owner?.isEnabled() &&
              group.owners.has(owner) &&
              generation === (owner.generation ?? 0)
                ? owner
                : Array.from(group.owners).find((entry) => entry.isEnabled());
            if (current) instrument(current, value);
            return value;
          };
          // get/use/withTenant/withConsistency must remain synchronous.
          return result && typeof result.then === "function"
            ? result.then(finish)
            : finish(result);
        },
    );
  }

  private patchClient(client: Target, group: PatchGroup) {
    for (const name of [
      "get",
      "use",
      "create",
      "createFromSchema",
      "createFromJson",
    ]) {
      this.wrapFactory(client?.collections, name, group, (owner, collection) =>
        owner.patchCollection(collection, group),
      );
    }
  }

  private patchCollection(collection: Target, group: PatchGroup) {
    if (!collection) return;
    for (const name of ["withTenant", "withConsistency"]) {
      this.wrapFactory(collection, name, group, (owner, variant) =>
        owner.patchCollection(variant, group),
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
        this.wrap(
          target,
          method,
          group,
          (original) =>
            function (this: unknown, ...args: any[]) {
              const owner = Array.from(group.owners).find((entry) =>
                entry.isEnabled(),
              );
              if (!owner) return original.apply(this, args);
              return owner.traceOperation(
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
