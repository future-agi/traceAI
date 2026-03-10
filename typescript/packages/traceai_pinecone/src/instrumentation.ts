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

const MODULE_NAME = "@pinecone-database/pinecone";

let _isPatched = false;

export function isPatched() {
  return _isPatched;
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

  private patch(moduleExports: any, moduleVersion?: string) {
    if (_isPatched) {
      return moduleExports;
    }

    const instrumentation = this;

    // Ensure fiTracer is properly initialized
    if (!instrumentation.fiTracer) {
      instrumentation.fiTracer = new FITracer({
        tracer: instrumentation.tracer,
        traceConfig: instrumentation._traceConfig,
      });
    }

    // Pinecone has an Index class with namespace() method that returns operations
    // We need to patch the Index operations: query, upsert, update, deleteOne, deleteMany, fetch

    if (moduleExports.Index) {
      const IndexPrototype = moduleExports.Index.prototype;

      // Patch query
      if (IndexPrototype.query) {
        const originalQuery = IndexPrototype.query;
        IndexPrototype.query = function (this: any, params: any) {
          return instrumentation.wrapQuery(originalQuery, this, params);
        };
      }

      // Patch upsert
      if (IndexPrototype.upsert) {
        const originalUpsert = IndexPrototype.upsert;
        IndexPrototype.upsert = function (this: any, vectors: any) {
          return instrumentation.wrapUpsert(originalUpsert, this, vectors);
        };
      }

      // Patch fetch
      if (IndexPrototype.fetch) {
        const originalFetch = IndexPrototype.fetch;
        IndexPrototype.fetch = function (this: any, ids: any) {
          return instrumentation.wrapFetch(originalFetch, this, ids);
        };
      }

      // Patch update
      if (IndexPrototype.update) {
        const originalUpdate = IndexPrototype.update;
        IndexPrototype.update = function (this: any, params: any) {
          return instrumentation.wrapUpdate(originalUpdate, this, params);
        };
      }

      // Patch deleteOne
      if (IndexPrototype.deleteOne) {
        const originalDeleteOne = IndexPrototype.deleteOne;
        IndexPrototype.deleteOne = function (this: any, id: any) {
          return instrumentation.wrapDeleteOne(originalDeleteOne, this, id);
        };
      }

      // Patch deleteMany
      if (IndexPrototype.deleteMany) {
        const originalDeleteMany = IndexPrototype.deleteMany;
        IndexPrototype.deleteMany = function (this: any, params: any) {
          return instrumentation.wrapDeleteMany(
            originalDeleteMany,
            this,
            params
          );
        };
      }

      // Patch deleteAll
      if (IndexPrototype.deleteAll) {
        const originalDeleteAll = IndexPrototype.deleteAll;
        IndexPrototype.deleteAll = function (this: any) {
          return instrumentation.wrapDeleteAll(originalDeleteAll, this);
        };
      }

      // Patch listPaginated
      if (IndexPrototype.listPaginated) {
        const originalList = IndexPrototype.listPaginated;
        IndexPrototype.listPaginated = function (this: any, params?: any) {
          return instrumentation.wrapList(originalList, this, params);
        };
      }

      // Patch describeIndexStats
      if (IndexPrototype.describeIndexStats) {
        const originalDescribe = IndexPrototype.describeIndexStats;
        IndexPrototype.describeIndexStats = function (this: any) {
          return instrumentation.wrapDescribeStats(originalDescribe, this);
        };
      }
    }

    _isPatched = true;
    return moduleExports;
  }

  private unpatch(moduleExports: any, moduleVersion?: string) {
    _isPatched = false;
  }

  private getIndexName(instance: any): string {
    return instance?.indexName || instance?._indexName || "unknown";
  }

  private getNamespace(instance: any): string {
    return instance?.namespace || instance?._namespace || "";
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
