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
  TracerProvider,
} from "@opentelemetry/api";
import { FITracer, TraceConfigOptions } from "@traceai/fi-core";
import { SemanticConventions } from "@traceai/fi-semantic-conventions";

const VERSION = "0.1.1"; // x-release-please-version

type MilvusOperation =
  | "search"
  | "query"
  | "insert"
  | "upsert"
  | "delete"
  | "get"
  | "createCollection"
  | "dropCollection"
  | "hasCollection"
  | "describeCollection"
  | "createIndex"
  | "dropIndex";

interface MethodPatch {
  name: string;
  descriptor?: PropertyDescriptor;
  original: Function;
  wrapper: Function;
}

interface ClientPatch {
  owners: Set<MilvusInstrumentation>;
  methods: MethodPatch[];
}

// A module-wide boolean cannot distinguish SDK copies or instrumentation owners.
const clientPatches = new WeakMap<object, ClientPatch>();
const originalMethods = new WeakMap<Function, Function>();
let activeTargets = 0;

export function isPatched(): boolean {
  return activeTargets > 0;
}

export interface MilvusInstrumentationConfig extends InstrumentationConfig {
  captureQueryVectors?: boolean;
  captureResultVectors?: boolean;
}

export class MilvusInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;
  // Lazily initialized: InstrumentationBase calls enable() from its constructor.
  private _targets?: Set<object>;

  constructor({
    instrumentationConfig,
    traceConfig,
  }: {
    instrumentationConfig?: MilvusInstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super("@traceai/milvus", VERSION, Object.assign({}, instrumentationConfig));
    this._traceConfig = traceConfig;
    this._refreshTracer();
  }

  private _refreshTracer(): void {
    this.fiTracer = new FITracer({
      tracer: this.tracer,
      traceConfig: this._traceConfig,
    });
  }

  public override setTracerProvider(provider: TracerProvider): void {
    super.setTracerProvider(provider);
    this._refreshTracer();
  }

  public override enable(): void {
    this._refreshTracer();
    super.enable();
    this._targets?.forEach(target => this._patchClient(target));
  }

  public override disable(): void {
    super.disable();
    // The base class only unpatches modules seen by its loader hooks, not
    // modules/instances passed to manuallyInstrument().
    this._targets?.forEach(target => this._unpatchClient(target));
  }

  protected init() {
    return new InstrumentationNodeModuleDefinition(
      "@zilliz/milvus2-sdk-node",
      [">=2.0.0"],
      this.patch.bind(this),
      this.unpatch.bind(this)
    );
  }

  public manuallyInstrument(milvusModule: any): void {
    this.patch(milvusModule);
  }

  /** Instrument an existing gRPC client, including aliases saved before setup.
   * For SDK 2.0's manager API, pass dataManager/collectionManager/indexManager.
   * The separate HTTP client is not supported.
   */
  public manuallyInstrumentClient(client: object): void {
    this._trackTarget(client);
  }

  private patch(moduleExports: any, moduleVersion?: string) {
    const prototype = moduleExports?.MilvusClient?.prototype;
    if (prototype) this._trackTarget(prototype);
    return moduleExports;
  }

  private unpatch(moduleExports: any, moduleVersion?: string) {
    const prototype = moduleExports?.MilvusClient?.prototype;
    if (prototype) this._unpatchClient(prototype);
    return moduleExports;
  }

  private _trackTarget(target: object): void {
    (this._targets ??= new Set()).add(target);
    if (this.isEnabled()) this._patchClient(target);
  }

  private _patchClient(prototype: any): void {
    let patch = clientPatches.get(prototype);
    if (patch) {
      if (patch.owners.has(this)) return;
      if (patch.owners.size === 0) {
        for (const method of patch.methods) {
          // Do not overwrite another library's replacement. Saved aliases (or
          // a foreign wrapper around ours) still use this patch's live owners.
          if (prototype[method.name] === method.original) {
            this._installMethod(prototype, method);
          }
        }
        activeTargets++;
      }
      patch.owners.add(this);
      return;
    }

    patch = { owners: new Set(), methods: [] };
    const owners = patch.owners;
    const methodsToPatch: { name: string; operation: MilvusOperation }[] = [
      { name: "search", operation: "search" },
      // Current SDKs assign this.search to an own hybridSearch property. Keep
      // the existing search span name and do not wrap a wrapped search twice.
      { name: "hybridSearch", operation: "search" },
      { name: "query", operation: "query" },
      { name: "insert", operation: "insert" },
      { name: "upsert", operation: "upsert" },
      { name: "delete", operation: "delete" },
      { name: "get", operation: "get" },
      { name: "createCollection", operation: "createCollection" },
      { name: "dropCollection", operation: "dropCollection" },
      { name: "hasCollection", operation: "hasCollection" },
      { name: "describeCollection", operation: "describeCollection" },
      { name: "createIndex", operation: "createIndex" },
      { name: "dropIndex", operation: "dropIndex" },
    ];

    for (const { name, operation } of methodsToPatch) {
      const original = prototype[name];
      if (typeof original !== "function") continue;
      const descriptor = Object.getOwnPropertyDescriptor(prototype, name);
      // Avoid invoking setters or trying to replace an immutable SDK method.
      if (descriptor && (!("value" in descriptor) || !descriptor.writable)) continue;
      if (!descriptor && !Object.isExtensible(prototype)) continue;
      const unwrapped = originalMethods.get(original) ?? original;
      const wrapper = function (this: any, ...args: any[]) {
        const owner = owners.values().next().value;
        // Aliases can retain this function after the prototype is restored.
        // Consult ownership on every call so disable also silences those aliases.
        return owner
          ? owner._wrapMethod(this, unwrapped, operation, args)
          : original.apply(this, args);
      };
      originalMethods.set(wrapper, unwrapped);
      const method = { name, descriptor, original, wrapper };
      this._installMethod(prototype, method);
      patch.methods.push(method);
    }
    if (patch.methods.length) {
      patch.owners.add(this);
      clientPatches.set(prototype, patch);
      activeTargets++;
    }
  }

  private _installMethod(target: any, method: MethodPatch): void {
    Object.defineProperty(target, method.name, method.descriptor
      ? { ...method.descriptor, value: method.wrapper }
      : { configurable: true, enumerable: false, writable: true, value: method.wrapper });
  }

  private _unpatchClient(target: any): void {
    const patch = clientPatches.get(target);
    if (!patch?.owners.delete(this) || patch.owners.size) return;
    activeTargets--;
    for (const method of patch.methods) {
      if (target[method.name] !== method.wrapper) continue;
      if (method.descriptor) {
        Object.defineProperty(target, method.name, method.descriptor);
      } else {
        // Restore inheritance rather than leaving a shadowing own property.
        delete target[method.name];
      }
    }
  }

  private getCommonAttributes(operation: MilvusOperation, collectionName?: string): Attributes {
    const attrs: Attributes = {
      "db.system": "milvus",
      "db.operation.name": operation,
      [SemanticConventions.FI_SPAN_KIND]: "VECTOR_DB",
    };
    if (collectionName) {
      attrs["db.collection.name"] = collectionName;
    }
    return attrs;
  }

  private async _wrapMethod(
    thisArg: any,
    original: Function,
    operation: MilvusOperation,
    args: any[]
  ): Promise<any> {
    const params = args[0] || {};
    const collectionName = params.collection_name;
    const attributes = this.getCommonAttributes(operation, collectionName);

    // Operation-specific attributes
    if (operation === "search" || operation === "query") {
      if (params.limit) attributes["db.vector.query.top_k"] = params.limit;
      if (params.topk) attributes["db.vector.query.top_k"] = params.topk;
      if (params.vectors?.length) {
        attributes["db.vector.query.dimensions"] = params.vectors[0]?.length || 0;
      }
    }

    if (operation === "insert" || operation === "upsert") {
      if (params.data) attributes["db.operation.batch_size"] = params.data.length;
      if (params.fields_data) attributes["db.operation.batch_size"] = params.fields_data.length;
    }

    const span = this.fiTracer.startSpan(`milvus ${operation}`, {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.apply(thisArg, args)
      );

      if (result?.results) {
        span.setAttribute("db.response.returned_rows", result.results.length);
      }

      span.setStatus({ code: SpanStatusCode.OK });
      return result;
    } catch (error: any) {
      span.recordException(error);
      span.setStatus({ code: SpanStatusCode.ERROR, message: error?.message });
      throw error;
    } finally {
      span.end();
    }
  }
}

export default MilvusInstrumentation;
