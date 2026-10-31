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
} from "@opentelemetry/api";
import { VERSION } from "./version";
import { SemanticConventions } from "@traceai/fi-semantic-conventions";
import { FITracer, TraceConfigOptions } from "@traceai/fi-core";

// Vector DB semantic conventions (inline to avoid build order issues)
const VectorDBConventions = {
  DB_SYSTEM: "db.system",
  DB_OPERATION_NAME: "db.operation.name",
  DB_NAMESPACE: "db.namespace",
  DB_VECTOR_QUERY_TOP_K: "db.vector.query.top_k",
  DB_VECTOR_QUERY_FILTER: "db.vector.query.filter",
  DB_VECTOR_QUERY_INCLUDE_METADATA: "db.vector.query.include_metadata",
  DB_VECTOR_QUERY_INCLUDE_VECTORS: "db.vector.query.include_vectors",
  DB_VECTOR_RESULTS_COUNT: "db.vector.results.count",
  DB_VECTOR_UPSERT_COUNT: "db.vector.upsert.count",
  DB_VECTOR_UPSERT_DIMENSIONS: "db.vector.upsert.dimensions",
  DB_VECTOR_DELETE_COUNT: "db.vector.delete.count",
  DB_VECTOR_DELETE_ALL: "db.vector.delete.all",
  DB_VECTOR_INDEX_NAME: "db.vector.index.name",
  DB_VECTOR_INDEX_DIMENSIONS: "db.vector.index.dimensions",
  DB_VECTOR_NAMESPACE: "db.vector.namespace",
} as const;

type IndexMethod = (...args: unknown[]) => Promise<unknown>;
type IndexPrototype = Record<string, IndexMethod>;
interface PineconeModule {
  Index?: { prototype: IndexPrototype };
}
const patchedPrototypes = new Set<IndexPrototype>();

export function isPatched(): boolean {
  return patchedPrototypes.size > 0;
}

/**
 * Configuration options for Pinecone instrumentation
 */
export interface PineconeInstrumentationConfig extends InstrumentationConfig {
  /**
   * Whether to capture query vectors in spans (may be large)
   */
  captureQueryVectors?: boolean;
  /**
   * Whether to capture result vectors in spans (may be large)
   */
  captureResultVectors?: boolean;
}

/**
 * OpenTelemetry instrumentation for Pinecone vector database
 */
export class PineconeInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;
  private _pineconeConfig: PineconeInstrumentationConfig;
  // Lazily initialized: InstrumentationBase calls enable() during construction.
  private _knownModules?: Set<PineconeModule>;
  private _wrappedMethods?: Map<IndexPrototype, Map<string, IndexMethod>>;

  constructor({
    instrumentationConfig,
    traceConfig,
  }: {
    instrumentationConfig?: PineconeInstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super(
      "@traceai/pinecone",
      VERSION,
      Object.assign({}, instrumentationConfig)
    );
    this._traceConfig = traceConfig;
    this._pineconeConfig = instrumentationConfig || {};
  }

  public override enable(): void {
    super.enable();
    this.fiTracer = new FITracer({
      tracer: this.tracer,
      traceConfig: this._traceConfig,
    });
    for (const module of this._knownModules ?? []) {
      this.patch(module);
    }
  }

  public override disable(): void {
    super.disable();
    // The base class only knows modules loaded through its require/import hooks.
    // Explicit registrations and additional SDK copies need the same cleanup.
    for (const module of this._knownModules ?? []) {
      this.unpatch(module);
    }
  }

  protected init() {
    return new InstrumentationNodeModuleDefinition(
      "@pinecone-database/pinecone",
      ["^2.0.0", "^3.0.0", "^4.0.0"],
      this.patch.bind(this),
      this.unpatch.bind(this)
    );
  }

  /**
   * Manually instruments the Pinecone module
   */
  manuallyInstrument(module: any) {
    this.patch(module);
  }

  private patch(moduleExports: PineconeModule) {
    const prototype = moduleExports?.Index?.prototype;
    if (!prototype) return moduleExports;

    (this._knownModules ??= new Set()).add(moduleExports);
    if (patchedPrototypes.has(prototype)) return moduleExports;

    if (!this.fiTracer) {
      this.fiTracer = new FITracer({
        tracer: this.tracer,
        traceConfig: this._traceConfig,
      });
    }

    const instrumentation = this;
    const wrappers = {
      query: this.wrapQuery,
      upsert: this.wrapUpsert,
      fetch: this.wrapFetch,
      update: this.wrapUpdate,
      deleteOne: this.wrapDeleteOne,
      deleteMany: this.wrapDeleteMany,
      deleteAll: this.wrapDeleteAll,
      listPaginated: this.wrapList,
      describeIndexStats: this.wrapDescribeStats,
    };
    const wrappedMethods = new Map<string, IndexMethod>();
    for (const [method, wrapper] of Object.entries(wrappers)) {
      if (typeof prototype[method] !== "function") continue;
      this._wrap(prototype, method, original => {
        const wrapped: IndexMethod = function (this: unknown, ...args: unknown[]) {
          // Cached references or later wrappers may outlive unpatch(). Only
          // this registration may emit, including after a subsequent re-enable.
          if (instrumentation._wrappedMethods?.get(prototype)?.get(method) !== wrapped) {
            return original.apply(this, args);
          }
          return wrapper.call(instrumentation, original, this, args[0]);
        };
        return wrapped;
      });
      wrappedMethods.set(method, prototype[method]);
    }
    if (wrappedMethods.size > 0) {
      (this._wrappedMethods ??= new Map()).set(prototype, wrappedMethods);
      patchedPrototypes.add(prototype);
    }
    return moduleExports;
  }

  private unpatch(moduleExports: PineconeModule) {
    const prototype = moduleExports?.Index?.prototype;
    if (!prototype) return;
    const wrappedMethods = this._wrappedMethods?.get(prototype);
    if (!wrappedMethods) return;

    for (const [method, wrapped] of wrappedMethods) {
      // Do not remove a replacement installed by somebody else after us.
      if (prototype[method] === wrapped) this._unwrap(prototype, method);
    }
    this._wrappedMethods!.delete(prototype);
    patchedPrototypes.delete(prototype);
  }

  private getIndexName(instance: any): string {
    return instance?.target?.index ?? instance?.indexName ?? instance?._indexName ?? "unknown";
  }

  private getNamespace(instance: any): string {
    // In supported SDKs namespace() is a method, not the namespace value.
    return instance?.target?.namespace ?? instance?._namespace ??
      (typeof instance?.namespace === "string" ? instance.namespace : "");
  }

  private getCommonAttributes(
    operation: string,
    indexName: string,
    namespace?: string
  ): Attributes {
    const attrs: Attributes = {
      [VectorDBConventions.DB_SYSTEM]: "pinecone",
      [VectorDBConventions.DB_OPERATION_NAME]: operation,
      [VectorDBConventions.DB_VECTOR_INDEX_NAME]: indexName,
      [SemanticConventions.FI_SPAN_KIND]: "VECTOR_DB",
    };

    if (namespace) {
      attrs[VectorDBConventions.DB_VECTOR_NAMESPACE] = namespace;
      attrs[VectorDBConventions.DB_NAMESPACE] = namespace;
    }

    return attrs;
  }

  private async wrapQuery(
    original: Function,
    instance: any,
    params: any
  ): Promise<any> {
    const indexName = this.getIndexName(instance);
    const namespace = params?.namespace || this.getNamespace(instance);
    const attributes = this.getCommonAttributes("query", indexName, namespace);

    const topK = params?.topK || 10;
    attributes[VectorDBConventions.DB_VECTOR_QUERY_TOP_K] = topK;

    if (params?.filter) {
      attributes[VectorDBConventions.DB_VECTOR_QUERY_FILTER] =
        JSON.stringify(params.filter);
    }

    if (params?.includeMetadata !== undefined) {
      attributes[VectorDBConventions.DB_VECTOR_QUERY_INCLUDE_METADATA] =
        params.includeMetadata;
    }

    if (params?.includeValues !== undefined) {
      attributes[VectorDBConventions.DB_VECTOR_QUERY_INCLUDE_VECTORS] =
        params.includeValues;
    }

    const span = this.fiTracer.startSpan("pinecone query", {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.call(instance, params)
      );

      if (result?.matches) {
        span.setAttribute(
          VectorDBConventions.DB_VECTOR_RESULTS_COUNT,
          result.matches.length
        );
      }

      span.setStatus({ code: SpanStatusCode.OK });
      return result;
    } catch (error: any) {
      span.recordException(error);
      span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
      throw error;
    } finally {
      span.end();
    }
  }

  private async wrapUpsert(
    original: Function,
    instance: any,
    vectors: any
  ): Promise<any> {
    const indexName = this.getIndexName(instance);
    const namespace = this.getNamespace(instance);
    const attributes = this.getCommonAttributes("upsert", indexName, namespace);

    const vectorCount = Array.isArray(vectors) ? vectors.length : 1;
    attributes[VectorDBConventions.DB_VECTOR_UPSERT_COUNT] = vectorCount;

    if (Array.isArray(vectors) && vectors.length > 0 && vectors[0]?.values) {
      attributes[VectorDBConventions.DB_VECTOR_UPSERT_DIMENSIONS] =
        vectors[0].values.length;
    }

    const span = this.fiTracer.startSpan("pinecone upsert", {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.call(instance, vectors)
      );
      span.setStatus({ code: SpanStatusCode.OK });
      return result;
    } catch (error: any) {
      span.recordException(error);
      span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
      throw error;
    } finally {
      span.end();
    }
  }

  private async wrapFetch(
    original: Function,
    instance: any,
    ids: any
  ): Promise<any> {
    const indexName = this.getIndexName(instance);
    const namespace = this.getNamespace(instance);
    const attributes = this.getCommonAttributes("fetch", indexName, namespace);

    const idCount = Array.isArray(ids) ? ids.length : 1;
    attributes["db.vector.fetch.ids_count"] = idCount;

    const span = this.fiTracer.startSpan("pinecone fetch", {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.call(instance, ids)
      );

      if (result?.records || result?.vectors) {
        const records = result.records || result.vectors;
        span.setAttribute(
          VectorDBConventions.DB_VECTOR_RESULTS_COUNT,
          Object.keys(records).length
        );
      }

      span.setStatus({ code: SpanStatusCode.OK });
      return result;
    } catch (error: any) {
      span.recordException(error);
      span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
      throw error;
    } finally {
      span.end();
    }
  }

  private async wrapUpdate(
    original: Function,
    instance: any,
    params: any
  ): Promise<any> {
    const indexName = this.getIndexName(instance);
    const namespace = params?.namespace || this.getNamespace(instance);
    const attributes = this.getCommonAttributes("update", indexName, namespace);

    attributes[VectorDBConventions.DB_VECTOR_UPSERT_COUNT] = 1;

    const span = this.fiTracer.startSpan("pinecone update", {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.call(instance, params)
      );
      span.setStatus({ code: SpanStatusCode.OK });
      return result;
    } catch (error: any) {
      span.recordException(error);
      span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
      throw error;
    } finally {
      span.end();
    }
  }

  private async wrapDeleteOne(
    original: Function,
    instance: any,
    id: any
  ): Promise<any> {
    const indexName = this.getIndexName(instance);
    const namespace = this.getNamespace(instance);
    const attributes = this.getCommonAttributes("delete", indexName, namespace);

    attributes[VectorDBConventions.DB_VECTOR_DELETE_COUNT] = 1;

    const span = this.fiTracer.startSpan("pinecone delete", {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.call(instance, id)
      );
      span.setStatus({ code: SpanStatusCode.OK });
      return result;
    } catch (error: any) {
      span.recordException(error);
      span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
      throw error;
    } finally {
      span.end();
    }
  }

  private async wrapDeleteMany(
    original: Function,
    instance: any,
    params: any
  ): Promise<any> {
    const indexName = this.getIndexName(instance);
    const namespace = this.getNamespace(instance);
    const attributes = this.getCommonAttributes("delete", indexName, namespace);

    if (Array.isArray(params)) {
      attributes[VectorDBConventions.DB_VECTOR_DELETE_COUNT] =
        params.length;
    } else if (params?.ids) {
      attributes[VectorDBConventions.DB_VECTOR_DELETE_COUNT] =
        params.ids.length;
    }

    const span = this.fiTracer.startSpan("pinecone delete_many", {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.call(instance, params)
      );
      span.setStatus({ code: SpanStatusCode.OK });
      return result;
    } catch (error: any) {
      span.recordException(error);
      span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
      throw error;
    } finally {
      span.end();
    }
  }

  private async wrapDeleteAll(
    original: Function,
    instance: any
  ): Promise<any> {
    const indexName = this.getIndexName(instance);
    const namespace = this.getNamespace(instance);
    const attributes = this.getCommonAttributes(
      "delete_all",
      indexName,
      namespace
    );

    attributes[VectorDBConventions.DB_VECTOR_DELETE_ALL] = true;

    const span = this.fiTracer.startSpan("pinecone delete_all", {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.call(instance)
      );
      span.setStatus({ code: SpanStatusCode.OK });
      return result;
    } catch (error: any) {
      span.recordException(error);
      span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
      throw error;
    } finally {
      span.end();
    }
  }

  private async wrapList(
    original: Function,
    instance: any,
    params?: any
  ): Promise<any> {
    const indexName = this.getIndexName(instance);
    const namespace = params?.namespace || this.getNamespace(instance);
    const attributes = this.getCommonAttributes("list", indexName, namespace);

    if (params?.limit) {
      attributes[VectorDBConventions.DB_VECTOR_QUERY_TOP_K] =
        params.limit;
    }

    const span = this.fiTracer.startSpan("pinecone list", {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.call(instance, params)
      );

      if (result?.vectors) {
        span.setAttribute(
          VectorDBConventions.DB_VECTOR_RESULTS_COUNT,
          result.vectors.length
        );
      }

      span.setStatus({ code: SpanStatusCode.OK });
      return result;
    } catch (error: any) {
      span.recordException(error);
      span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
      throw error;
    } finally {
      span.end();
    }
  }

  private async wrapDescribeStats(
    original: Function,
    instance: any
  ): Promise<any> {
    const indexName = this.getIndexName(instance);
    const attributes = this.getCommonAttributes("describe_stats", indexName);

    const span = this.fiTracer.startSpan("pinecone describe_stats", {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.call(instance)
      );

      if (result?.totalRecordCount !== undefined) {
        span.setAttribute(
          VectorDBConventions.DB_VECTOR_RESULTS_COUNT,
          result.totalRecordCount
        );
      }

      if (result?.dimension) {
        span.setAttribute(
          VectorDBConventions.DB_VECTOR_INDEX_DIMENSIONS,
          result.dimension
        );
      }

      span.setStatus({ code: SpanStatusCode.OK });
      return result;
    } catch (error: any) {
      span.recordException(error);
      span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
      throw error;
    } finally {
      span.end();
    }
  }
}
