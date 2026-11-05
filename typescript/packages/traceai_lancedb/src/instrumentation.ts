import {
  InstrumentationBase,
  InstrumentationConfig,
  InstrumentationNodeModuleDefinition,
} from "@opentelemetry/instrumentation";
import {
  context, trace, createContextKey, SpanKind, SpanStatusCode, Attributes, Span,
} from "@opentelemetry/api";
import { FITracer, TraceConfigOptions } from "@traceai/fi-core";
import { SemanticConventions } from "@traceai/fi-semantic-conventions";

const VERSION = "0.1.1"; // x-release-please-version
const SUPPORTED_VERSIONS = [">=0.4.20 <=0.41.0"];
const ACTIVE_QUERY = createContextKey("traceai.lancedb.query");

type Method = (this: any, ...args: any[]) => any;
interface Patch {
  target: any;
  key: PropertyKey;
  descriptor: PropertyDescriptor;
  wrapper: Method;
  owners: Set<LanceDBInstrumentation>;
  group: ModuleGroup;
}
interface ModuleGroup {
  owners: Set<LanceDBInstrumentation>;
  patches: Set<Patch>;
}
interface QueryMetadata {
  tableName?: string;
  dimensions?: number;
  limit?: number;
}
// Ownership is per method, not a single global flag: a second instrumentor must
// neither double-wrap nor undo the first instrumentor's hooks.
const activePatches = new Set<Patch>();
// Separate LanceDB module instances (including different SDK versions) do not share owners.
const moduleGroups = new WeakMap<object, ModuleGroup>();
const queryMetadata = new WeakMap<object, QueryMetadata>();
const tableNames = new WeakMap<object, string>();
export function isPatched(): boolean {
  return activePatches.size > 0;
}

export interface LanceDBInstrumentationConfig extends InstrumentationConfig {
  /** @deprecated Vectors are never captured. Retained for source compatibility. */
  captureQueryVectors?: boolean;
  /** @deprecated Vectors are never captured. Retained for source compatibility. */
  captureResultVectors?: boolean;
}

export class LanceDBInstrumentation extends InstrumentationBase {
  private _traceConfig?: TraceConfigOptions;
  private _patches: Patch[] = [];
  private _groups = new Set<ModuleGroup>();
  private _queries = queryMetadata;
  private _tables = tableNames;

  constructor({ instrumentationConfig, traceConfig }: {
    instrumentationConfig?: LanceDBInstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super("@traceai/lancedb", VERSION, { ...instrumentationConfig });
    this._traceConfig = traceConfig;
  }

  protected init() {
    return new InstrumentationNodeModuleDefinition(
      "@lancedb/lancedb", SUPPORTED_VERSIONS,
      this.patch.bind(this), this.unpatch.bind(this),
    );
  }

  public override enable(): void {
    super.enable();
    // InstrumentationBase calls enable from its constructor, before our fields
    // exist. Later enables also restore hooks installed by manuallyInstrument.
    for (const group of this._groups ?? []) group.owners.add(this);
    for (const patch of this._patches ?? []) {
      patch.owners.add(this);
      this.install(patch);
    }
  }

  public override disable(): void {
    super.disable();
    this.unpatch();
  }

  public manuallyInstrument(lanceModule: any): void {
    this.patch(lanceModule);
  }

  private patch(moduleExports: any) {
    if (!this.isEnabled()) return moduleExports;
    // Node ESM namespace objects are immutable; their CJS default is mutable.
    const target = moduleExports?.default?.connect ? moduleExports.default : moduleExports;
    if (!target || (typeof target !== "object" && typeof target !== "function")) return moduleExports;
    const group = moduleGroups.get(target) ?? { owners: new Set(), patches: new Set() };
    const installed = this.wrap(target, "connect", (original, owner) => function (...args: any[]) {
      const result = original.apply(this, args);
      return result.then((connection: any) => {
        owner()?.instrumentConnection(connection, group);
        return connection;
      });
    }, group);
    if (installed) {
      moduleGroups.set(target, group);
      this._groups.add(group);
      group.owners.add(this);
      // Adopt already-discovered connection, table, and query methods too.
      for (const p of group.patches) {
        p.owners.add(this);
        if (!this._patches.includes(p)) this._patches.push(p);
      }
    }
    if (!installed) this._diag.warn("LanceDB: no writable connect export; pass the CommonJS module or its ESM default before connecting.");
    return moduleExports;
  }

  private unpatch(): void {
    for (const group of this._groups ?? []) group.owners.delete(this);
    for (const patch of this._patches ?? []) {
      patch.owners.delete(this);
      if (patch.owners.size) continue;
      if (Object.getOwnPropertyDescriptor(patch.target, patch.key)?.value === patch.wrapper) {
        Object.defineProperty(patch.target, patch.key, patch.descriptor);
      }
      activePatches.delete(patch);
    }
  }

  private install(patch: Patch): void {
    const current = Object.getOwnPropertyDescriptor(patch.target, patch.key);
    if (current?.value === patch.wrapper && activePatches.has(patch)) return;
    if (!current || current.value !== patch.descriptor.value || current.get !== patch.descriptor.get) return;
    if ([...activePatches].some(p => p.target === patch.target && p.key === patch.key)) return;
    Object.defineProperty(patch.target, patch.key, {
      configurable: patch.descriptor.configurable, enumerable: patch.descriptor.enumerable,
      writable: patch.descriptor.writable ?? true, value: patch.wrapper,
    });
    activePatches.add(patch);
  }

  private wrap(target: any, key: PropertyKey,
    factory: (original: Method, owner: () => LanceDBInstrumentation | undefined) => Method,
    group: ModuleGroup): boolean {
    // Find the concrete owner rather than the exported abstract Connection/Table.
    while (target && !Object.prototype.hasOwnProperty.call(target, key)) target = Object.getPrototypeOf(target);
    if (!target) return false;
    const descriptor = Object.getOwnPropertyDescriptor(target, key)!;
    const original = target[key];
    if (typeof original !== "function" || (!descriptor.configurable && !descriptor.writable)) return false;
    const active = [...activePatches].find(p => p.target === target && p.key === key);
    if (active) {
      if (active.group !== group) return false;
      active.owners.add(this);
      if (!this._patches.includes(active)) this._patches.push(active);
      return true;
    }
    const existing = [...group.patches].find(p => p.target === target && p.key === key &&
      p.descriptor.value === descriptor.value && p.descriptor.get === descriptor.get);
    const patch: Patch = existing ?? { target, key, descriptor, wrapper: original, owners: new Set(), group };
    if (!existing) {
      patch.wrapper = factory(original, () => [...patch.owners].find(p => p.isEnabled()));
      group.patches.add(patch);
    }
    for (const owner of group.owners) {
      patch.owners.add(owner);
      if (!owner._patches.includes(patch)) owner._patches.push(patch);
    }
    if (!this._patches.includes(patch)) this._patches.push(patch);
    // A pending openTable may settle after disable. Remember its methods for
    // re-enable, but do not install hooks while no instrumentor is active.
    if (this.isEnabled()) {
      patch.owners.add(this);
      this.install(patch);
    }
    return activePatches.has(patch);
  }

  private instrumentConnection(connection: any, group: ModuleGroup): void {
    for (const operation of ["createTable", "openTable", "dropTable", "tableNames"]) {
      this.wrap(connection, operation, (original, owner) => function (...args: any[]) {
        const instrumentation = owner();
        if (!instrumentation) return original.apply(this, args);
        const tableName = typeof args[0] === "string" ? args[0] : args[0]?.name;
        return instrumentation.run(this, operation, () => original.apply(this, args), { tableName }, false,
          (result) => {
            if (operation === "createTable" || operation === "openTable") {
              // Ownership can change while the call is in flight. Discover the
              // returned table under the current owner, or retain dormant hooks
              // for this owner's next enable if everyone was disabled.
              (owner() ?? instrumentation).instrumentTable(result, tableName, group);
            }
            return result;
          });
      }, group);
    }
  }

  private instrumentTable(table: any, tableName: string | undefined, group: ModuleGroup): void {
    if (typeof tableName === "string") this._tables.set(table, tableName);
    for (const operation of ["add", "update", "delete", "countRows"]) {
      this.wrap(table, operation, (original, owner) => function (...args: any[]) {
        const instrumentation = owner();
        if (!instrumentation) return original.apply(this, args);
        const attributes: Attributes = {};
        if (operation === "add" && Array.isArray(args[0])) attributes["db.operation.batch_size"] = args[0].length;
        return instrumentation.run(this, operation, () => original.apply(this, args), { tableName: instrumentation._tables.get(this) ?? this.name }, false, undefined, attributes);
      }, group);
    }
    for (const factory of ["query", "search", "vectorSearch"]) {
      this.wrap(table, factory, (original, owner) => function (...args: any[]) {
        // Builders are synchronous. Do not start a span until execution begins.
        const query = original.apply(this, args);
        const instrumentation = owner();
        if (instrumentation) {
          const vector = args[0];
          instrumentation.instrumentQuery(query, {
            tableName: instrumentation._tables.get(this) ?? this.name,
            ...(Array.isArray(vector) && typeof vector[0] === "number" ? { dimensions: vector.length } : {}),
          }, group);
        }
        return query;
      }, group);
    }
  }

  private instrumentQuery(query: any, metadata: QueryMetadata, group: ModuleGroup): void {
    if (!query || typeof query !== "object") return;
    this._queries.set(query, { ...this._queries.get(query), ...metadata });
    for (const factory of ["nearestTo", "nearestToText", "fullTextSearch", "limit"]) {
      this.wrap(query, factory, (original, owner) => function (...args: any[]) {
        const result = original.apply(this, args);
        const instrumentation = owner();
        if (instrumentation) {
          const metadata = { ...instrumentation._queries.get(this) };
          if (factory === "limit" && Number.isFinite(args[0])) metadata.limit = args[0];
          if (factory === "nearestTo" && Array.isArray(args[0])) metadata.dimensions = args[0].length;
          instrumentation.instrumentQuery(result, metadata, group);
        }
        return result;
      }, group);
    }
    for (const terminal of ["toArray", "toArrow", "execute", Symbol.asyncIterator]) {
      this.wrap(query, terminal, (original, owner) => function (...args: any[]) {
        const instrumentation = owner();
        return instrumentation
          ? instrumentation.run(this, "search", () => original.apply(this, args), instrumentation._queries.get(this), true)
          : original.apply(this, args);
      }, group);
    }
  }

  private run(
    receiver: any, operation: string, call: () => any, metadata: QueryMetadata = {},
    query = false, transform: (value: any) => any = value => value, extra: Attributes = {},
  ): any {
    if (!this.isEnabled() || (query && context.active().getValue(ACTIVE_QUERY) === receiver)) return call();
    const attributes: Attributes = {
      "db.system": "lancedb", "db.operation.name": operation,
      [SemanticConventions.FI_SPAN_KIND]: "VECTOR_DB", ...extra,
    };
    if (typeof metadata.tableName === "string") attributes["db.collection.name"] = metadata.tableName;
    if (metadata.dimensions !== undefined) attributes["db.vector.query.dimensions"] = metadata.dimensions;
    if (metadata.limit !== undefined) attributes["db.vector.query.top_k"] = metadata.limit;
    // Read the current tracer, including a provider supplied after construction.
    const span = new FITracer({ tracer: this.tracer, traceConfig: this._traceConfig })
      .startSpan(`lancedb ${operation}`, { kind: SpanKind.CLIENT, attributes });
    let spanContext = trace.setSpan(context.active(), span);
    if (query) spanContext = spanContext.setValue(ACTIVE_QUERY, receiver);
    const success = (result: any) => {
      const rows = Array.isArray(result) ? result.length : result?.numRows;
      if (typeof rows === "number") span.setAttribute("db.response.returned_rows", rows);
      span.setStatus({ code: SpanStatusCode.OK });
      span.end();
      return transform(result);
    };
    const failure = (error: unknown): never => {
      this.fail(span, error);
      span.end();
      throw error;
    };
    try {
      const result = context.with(spanContext, call);
      if (result && typeof result.next === "function") {
        return this.traceIterator(result, span, spanContext);
      }
      if (result && typeof result.then === "function") return result.then(success, failure);
      return success(result);
    } catch (error) {
      return failure(error);
    }
  }

  private fail(span: Span, error: unknown): void {
    const exception = error instanceof Error ? error : String(error);
    span.recordException(exception);
    span.setStatus({ code: SpanStatusCode.ERROR, message: error instanceof Error ? error.message : String(error) });
  }

  private traceIterator(iterator: any, span: Span, spanContext: ReturnType<typeof context.active>): any {
    let ended = false;
    let rows = 0;
    const finish = () => {
      if (ended) return;
      ended = true;
      span.setAttribute("db.response.returned_rows", rows);
      span.end();
    };
    for (const method of ["next", "return", "throw"] as const) {
      const original = iterator[method];
      iterator[method] = async (...args: any[]) => {
        try {
          const result = await context.with(spanContext, () => {
            if (original) return original.apply(iterator, args);
            if (method === "throw") throw args[0];
            return { done: true, value: args[0] };
          });
          if (!ended) {
            if (!result.done) rows += result.value?.numRows ?? 0;
            if (result.done || method === "return") {
              span.setStatus({ code: SpanStatusCode.OK });
              finish();
            }
          }
          return result;
        } catch (error) {
          if (!ended) { this.fail(span, error); finish(); }
          throw error;
        }
      };
    }
    return iterator;
  }
}

export default LanceDBInstrumentation;
