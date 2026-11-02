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
import { types } from "node:util";
import { FITracer, TraceConfigOptions } from "@traceai/fi-core";
import { SemanticConventions } from "@traceai/fi-semantic-conventions";

const VERSION = "0.1.2"; // x-release-please-version

// node-redis generates different client types for standalone, cluster, pool and
// sentinel clients. Only the public methods below are inspected, not internals.
type RedisTarget = Record<string, any>;
type RedisVectorOperation =
  | "ftSearch" | "ftCreate" | "ftDropIndex" | "ftInfo" | "ftAggregate"
  | "hSet" | "hGet" | "hGetAll" | "del" | "json.set" | "json.get";
const factories = ["createClient", "createCluster", "createClientPool", "createSentinel"];
const commands: [string | undefined, string, RedisVectorOperation][] = [
  ["ft", "search", "ftSearch"],
  ["ft", "create", "ftCreate"],
  ["ft", "dropIndex", "ftDropIndex"],
  ["ft", "info", "ftInfo"],
  ["ft", "aggregate", "ftAggregate"],
  [undefined, "hSet", "hSet"],
  [undefined, "hGet", "hGet"],
  [undefined, "hGetAll", "hGetAll"],
  [undefined, "del", "del"],
  ["json", "set", "json.set"],
  ["json", "get", "json.get"],
];

// Enumerate live targets for disable/re-enable without retaining closed clients.
class WeakRegistry<T extends object> {
  private readonly entries = new Set<WeakRef<T>>();
  private readonly seen = new WeakSet<T>();
  private readonly cleanup = new FinalizationRegistry<WeakRef<T>>((ref) => {
    this.entries.delete(ref);
  });

  add(value: T): void {
    if (this.seen.has(value)) return;
    this.seen.add(value);
    const ref = new WeakRef(value);
    this.entries.add(ref);
    this.cleanup.register(value, ref);
  }

  *values(): IterableIterator<T> {
    for (const ref of this.entries) {
      const value = ref.deref();
      if (value) yield value;
      else this.entries.delete(ref);
    }
  }
}

interface MethodPatch {
  target: WeakRef<RedisTarget>;
  name: string;
  descriptor?: PropertyDescriptor;
  original: Function;
  wrapper: Function;
  owners: Set<RedisInstrumentation>;
}

// Share one wrapper across auto/manual setup and instrumentation instances.
const patches = new WeakMap<object, Map<string, MethodPatch>>();
const livePatches = new WeakRegistry<MethodPatch>();

export function isPatched(): boolean {
  for (const patch of livePatches.values()) {
    if ([...patch.owners.keys()].some((owner) => owner.isEnabled())) return true;
  }
  return false;
}

export interface RedisInstrumentationConfig extends InstrumentationConfig {
  /** @deprecated Retained for compatibility; query vectors are never captured. */
  captureQueryVectors?: boolean;
  /** @deprecated Retained for compatibility; result vectors are never captured. */
  captureResultVectors?: boolean;
}

export class RedisInstrumentation extends InstrumentationBase<RedisInstrumentationConfig> {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;
  // Lazily initialized: InstrumentationBase calls enable() from its constructor.
  private modules?: WeakRegistry<RedisTarget>;
  private clients?: WeakRegistry<RedisTarget>;
  private ownedPatches?: WeakRegistry<MethodPatch>;
  private facades?: WeakMap<object, RedisTarget>;

  constructor({
    instrumentationConfig,
    traceConfig,
  }: {
    instrumentationConfig?: RedisInstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super("@traceai/redis", VERSION, { ...instrumentationConfig });
    this._traceConfig = traceConfig;
    this.refreshTracer();
  }

  private refreshTracer(): void {
    this.fiTracer = new FITracer({ tracer: this.tracer, traceConfig: this._traceConfig });
  }

  public override setTracerProvider(provider: TracerProvider): void {
    super.setTracerProvider(provider);
    this.refreshTracer();
  }

  public override enable(): void {
    this.refreshTracer();
    super.enable();
    for (const module of this.modules?.values() ?? []) this.patch(module);
    for (const client of this.clients?.values() ?? []) this.instrumentClient(client);
  }

  public override disable(): void {
    super.disable();
    // Base.disable only knows modules loaded by its hooks, not manual targets.
    this.restore();
  }

  protected init() {
    return new InstrumentationNodeModuleDefinition(
      "redis", [">=4.0.0 <7"], this.patch.bind(this), this.unpatch.bind(this)
    );
  }

  /**
   * Mutable CommonJS exports are patched in place. For ESM namespaces (whose
   * bindings cannot be reassigned), use the returned, typed module facade.
   */
  public manuallyInstrument<T extends object>(redisModule: T): T {
    return this.patch(redisModule) as T;
  }

  /** Instrument an already-created client without changing its identity. */
  public instrumentClient<T extends object>(client: T): T {
    (this.clients ??= new WeakRegistry()).add(client);
    if (!this.isEnabled()) return client;
    const target = client as RedisTarget;
    for (const [namespace, name, operation] of commands) {
      const receiver = namespace ? target[namespace] : target;
      if (!receiver) continue;
      this.wrap(receiver, name, operation);
      // node-redis also exposes uppercase command aliases.
      this.wrap(receiver, name.toUpperCase(), operation);
    }
    return client;
  }

  private patch(moduleExports: RedisTarget): RedisTarget {
    const immutable = types.isModuleNamespaceObject(moduleExports) ||
      !Object.isExtensible(moduleExports) || factories.some((name) => {
        const descriptor = Object.getOwnPropertyDescriptor(moduleExports, name);
        return descriptor && !descriptor.configurable && !descriptor.writable;
      });
    let target = moduleExports;
    if (immutable) {
      this.facades ??= new WeakMap();
      target = this.facades.get(moduleExports) ?? { ...moduleExports };
      this.facades.set(moduleExports, target);
    }
    (this.modules ??= new WeakRegistry()).add(target);
    if (this.isEnabled()) {
      for (const name of factories) this.wrap(target, name, "factory");
    }
    return target;
  }

  private unpatch(moduleExports: RedisTarget): RedisTarget {
    this.restore();
    return moduleExports;
  }

  private wrap(target: RedisTarget, name: string, operation: RedisVectorOperation | "factory"): void {
    if (typeof target[name] !== "function") return;
    let methods = patches.get(target);
    if (!methods) patches.set(target, methods = new Map());
    let patch = methods.get(name);
    // A later wrapper may delegate to ours. Rewrapping it would nest two spans.
    // Leave application replacements alone until the next disable/re-enable.
    if (!patch) {
      const descriptor = Object.getOwnPropertyDescriptor(target, name);
      if (descriptor && !descriptor.configurable && !descriptor.writable) return;
      const record: MethodPatch = {
        target: new WeakRef(target), name, descriptor, original: target[name],
        wrapper: () => undefined, owners: new Set(),
      };
      record.wrapper = function (this: unknown, ...args: any[]) {
        const owners = [...record.owners].filter((owner) => owner.isEnabled());
        if (operation === "factory") {
          const client = record.original.apply(this, args);
          for (const owner of owners) owner.instrumentClient(client);
          return client;
        }
        const owner = owners[0];
        return owner
          ? owner._wrapMethod(this, record.original, operation, args)
          : record.original.apply(this, args);
      };
      Object.defineProperty(target, name, descriptor && "value" in descriptor
        ? { ...descriptor, value: record.wrapper }
        : { configurable: true, enumerable: descriptor?.enumerable ?? true, writable: true, value: record.wrapper });
      methods.set(name, patch = record);
      livePatches.add(record);
    }
    patch.owners.add(this);
    (this.ownedPatches ??= new WeakRegistry()).add(patch);
  }

  private restore(): void {
    for (const patch of this.ownedPatches?.values() ?? []) {
      patch.owners.delete(this);
      if (patch.owners.size) continue;
      const target = patch.target.deref();
      if (!target) continue;
      // Do not undo a later wrapper installed by the application or another SDK.
      if (target[patch.name] === patch.wrapper) {
        if (patch.descriptor) Object.defineProperty(target, patch.name, patch.descriptor);
        else delete target[patch.name];
      }
      if (patches.get(target)?.get(patch.name) === patch) patches.get(target)?.delete(patch.name);
    }
  }

  private getCommonAttributes(operation: RedisVectorOperation): Attributes {
    return {
      "db.system": "redis",
      "db.operation.name": operation,
      [SemanticConventions.FI_SPAN_KIND]: "VECTOR_DB",
    };
  }

  private _setOperationAttributes(attributes: Attributes, operation: RedisVectorOperation, args: any[]): void {
    switch (operation) {
      case "ftSearch":
        if (typeof args[0] === "string") attributes["db.redis.index_name"] = args[0];
        // Inspect only the query shape; never serialize query text or PARAMS.
        if (typeof args[1] === "string" && /\bKNN\b/i.test(args[1])) {
          attributes["db.operation.type"] = "vector_search";
        }
        if (typeof args[2]?.LIMIT?.size === "number") {
          attributes["db.vector.query.top_k"] = args[2].LIMIT.size;
        }
        break;
      case "ftCreate":
      case "ftDropIndex":
      case "ftInfo":
      case "ftAggregate":
        if (typeof args[0] === "string") attributes["db.redis.index_name"] = args[0];
        break;
      case "del":
        if (Array.isArray(args[0])) {
          attributes["db.operation.batch_size"] = args[0].length;
          break;
        }
        // Single-key DEL has the same metadata as hash/JSON commands.
      default:
        if (typeof args[0] === "string") attributes["db.redis.key"] = args[0];
    }
  }

  private _setResultAttributes(span: ReturnType<FITracer["startSpan"]>, operation: RedisVectorOperation, result: any): void {
    if (operation === "ftSearch") {
      if (typeof result?.total === "number") span.setAttribute("db.response.total_count", result.total);
      if (Array.isArray(result?.documents)) span.setAttribute("db.response.returned_rows", result.documents.length);
    } else if (operation === "ftInfo" && typeof result?.numDocs === "number") {
      span.setAttribute("db.redis.num_docs", result.numDocs);
    }
  }

  private async _wrapMethod(
    thisArg: unknown, original: Function, operation: RedisVectorOperation, args: any[]
  ): Promise<any> {
    const attributes = this.getCommonAttributes(operation);
    this._setOperationAttributes(attributes, operation, args);
    const span = this.fiTracer.startSpan(`redis ${operation}`, { kind: SpanKind.CLIENT, attributes });
    try {
      const result = await context.with(trace.setSpan(context.active(), span), () => original.apply(thisArg, args));
      this._setResultAttributes(span, operation, result);
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

export default RedisInstrumentation;
