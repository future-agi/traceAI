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
}
interface QueryMetadata {
  tableName?: string;
  dimensions?: number;
  limit?: number;
}
// Ownership is per method, not a single global flag: a second instrumentor must
// neither double-wrap nor undo the first instrumentor's hooks.
const activePatches = new Set<Patch>();
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
  private _queries = new WeakMap<object, QueryMetadata>();
  private _tables = new WeakMap<object, string>();

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
    for (const patch of this._patches ?? []) this.install(patch);
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
    const installed = this.wrap(target, "connect", (original) => {
      const instrumentation = this;
      return function (...args: any[]) {
        const result = original.apply(this, args);
        if (!instrumentation.isEnabled()) return result;
        return result.then((connection: any) => {
          if (instrumentation.isEnabled()) instrumentation.instrumentConnection(connection);
          return connection;
        });
      };
    });
    if (!installed) this._diag.warn("LanceDB: no writable connect export; pass the CommonJS module or its ESM default before connecting.");
    return moduleExports;
  }

  private unpatch(): void {
    for (const patch of this._patches ?? []) {
      if (Object.getOwnPropertyDescriptor(patch.target, patch.key)?.value === patch.wrapper) {
        Object.defineProperty(patch.target, patch.key, patch.descriptor);
      }
      activePatches.delete(patch);
    }
  }

  private install(patch: Patch): void {
    const current = Object.getOwnPropertyDescriptor(patch.target, patch.key);
    if (!current || current.value !== patch.descriptor.value || current.get !== patch.descriptor.get) return;
    if ([...activePatches].some(p => p.target === patch.target && p.key === patch.key)) return;
    Object.defineProperty(patch.target, patch.key, {
      configurable: patch.descriptor.configurable, enumerable: patch.descriptor.enumerable,
      writable: patch.descriptor.writable ?? true, value: patch.wrapper,
    });
    activePatches.add(patch);
  }

  private wrap(target: any, key: PropertyKey, factory: (original: Method) => Method): boolean {
    // Find the concrete owner rather than the exported abstract Connection/Table.
    while (target && !Object.prototype.hasOwnProperty.call(target, key)) target = Object.getPrototypeOf(target);
    if (!target) return false;
    const descriptor = Object.getOwnPropertyDescriptor(target, key)!;
    const original = target[key];
    if (typeof original !== "function" || (!descriptor.configurable && !descriptor.writable)) return false;
    if ([...activePatches].some(p => p.target === target && p.key === key)) return true;
    const existing = this._patches.find(p => p.target === target && p.key === key &&
      p.descriptor.value === descriptor.value && p.descriptor.get === descriptor.get);
    const patch = existing ?? { target, key, descriptor, wrapper: factory(original) };
    if (!existing) this._patches.push(patch);
    this.install(patch);
    return activePatches.has(patch);
  }

  private instrumentConnection(connection: any): void {
    for (const operation of ["createTable", "openTable", "dropTable", "tableNames"]) {
      const instrumentation = this;
      this.wrap(connection, operation, original => function (...args: any[]) {
        if (!instrumentation.isEnabled()) return original.apply(this, args);
        const tableName = typeof args[0] === "string" ? args[0] : args[0]?.name;
        return instrumentation.run(this, operation, () => original.apply(this, args), { tableName }, false,
          (result) => {
            if (instrumentation.isEnabled() && (operation === "createTable" || operation === "openTable")) {
              instrumentation.instrumentTable(result, tableName);
            }
            return result;
          });
      });
    }
  }

  private instrumentTable(table: any, tableName?: string): void {
    if (typeof tableName === "string") this._tables.set(table, tableName);
    const instrumentation = this;
    for (const operation of ["add", "update", "delete", "countRows"]) {
      this.wrap(table, operation, original => function (...args: any[]) {
        const attributes: Attributes = {};
        if (operation === "add" && Array.isArray(args[0])) attributes["db.operation.batch_size"] = args[0].length;
        return instrumentation.run(this, operation, () => original.apply(this, args), { tableName: instrumentation._tables.get(this) ?? this.name }, false, undefined, attributes);
      });
    }
    for (const factory of ["query", "search", "vectorSearch"]) {
      this.wrap(table, factory, original => function (...args: any[]) {
        // Builders are synchronous. Do not start a span until execution begins.
        const query = original.apply(this, args);
        if (instrumentation.isEnabled()) {
          const vector = args[0];
          instrumentation.instrumentQuery(query, {
            tableName: instrumentation._tables.get(this) ?? this.name,
            ...(Array.isArray(vector) && typeof vector[0] === "number" ? { dimensions: vector.length } : {}),
          });
        }
        return query;
      });
    }
  }

  private instrumentQuery(query: any, metadata: QueryMetadata): void {
    if (!query || typeof query !== "object") return;
    this._queries.set(query, { ...this._queries.get(query), ...metadata });
    const instrumentation = this;
    for (const factory of ["nearestTo", "nearestToText", "fullTextSearch", "limit"]) {
      this.wrap(query, factory, original => function (...args: any[]) {
        const result = original.apply(this, args);
        if (instrumentation.isEnabled()) {
          const metadata = { ...instrumentation._queries.get(this) };
          if (factory === "limit" && Number.isFinite(args[0])) metadata.limit = args[0];
          if (factory === "nearestTo" && Array.isArray(args[0])) metadata.dimensions = args[0].length;
          instrumentation.instrumentQuery(result, metadata);
        }
        return result;
      });
    }
    for (const terminal of ["toArray", "toArrow", "execute", Symbol.asyncIterator]) {
      this.wrap(query, terminal, original => function (...args: any[]) {
        return instrumentation.run(this, "search", () => original.apply(this, args),
          instrumentation._queries.get(this), true);
      });
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
