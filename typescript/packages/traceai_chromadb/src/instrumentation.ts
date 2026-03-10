import {
  InstrumentationBase,
  InstrumentationConfig,
  InstrumentationNodeModuleDefinition,
  safeExecuteInTheMiddle,
} from "@opentelemetry/instrumentation";
import {
  context,
  trace,
  SpanKind,
  SpanStatusCode,
  Span,
  Attributes,
} from "@opentelemetry/api";
import { VERSION } from "./version";
import { SemanticConventions } from "@traceai/fi-semantic-conventions";

// Vector DB semantic conventions
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
  DB_VECTOR_COLLECTION_NAME: "db.vector.collection.name",
} as const;
import { FITracer, TraceConfigOptions } from "@traceai/fi-core";

const MODULE_NAME = "chromadb";

let _isPatched = false;

export function isPatched() {
  return _isPatched;
}

/**
 * Configuration options for ChromaDB instrumentation
 */
export interface ChromaDBInstrumentationConfig extends InstrumentationConfig {
  /**
   * Whether to capture query vectors in spans (may be large)
   */
  captureQueryVectors?: boolean;
  /**
   * Whether to capture result vectors in spans (may be large)
   */
  captureResultVectors?: boolean;
  /**
   * Whether to capture document content in spans
   */
  captureDocuments?: boolean;
}

/**
 * OpenTelemetry instrumentation for ChromaDB vector database
 */
export class ChromaDBInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;
  private _chromaConfig: ChromaDBInstrumentationConfig;

  constructor({
    instrumentationConfig,
    traceConfig,
  }: {
    instrumentationConfig?: ChromaDBInstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super(
      "@traceai/chromadb",
      VERSION,
      Object.assign({}, instrumentationConfig)
    );
    this._traceConfig = traceConfig;
    this._chromaConfig = instrumentationConfig || {};
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
      "chromadb",
      ["^1.0.0"],
      this.patch.bind(this),
      this.unpatch.bind(this)
    );
  }

  /**
   * Manually instruments the ChromaDB module
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

    // Patch Collection class methods
    if (moduleExports.Collection) {
      const CollectionPrototype = moduleExports.Collection.prototype;

      // Patch add method
      if (CollectionPrototype.add) {
        const originalAdd = CollectionPrototype.add;
        CollectionPrototype.add = function (this: any, params: any) {
          return instrumentation.wrapAdd(originalAdd, this, params);
        };
      }

      // Patch query method
      if (CollectionPrototype.query) {
        const originalQuery = CollectionPrototype.query;
        CollectionPrototype.query = function (this: any, params: any) {
          return instrumentation.wrapQuery(originalQuery, this, params);
        };
      }

      // Patch get method
      if (CollectionPrototype.get) {
        const originalGet = CollectionPrototype.get;
        CollectionPrototype.get = function (this: any, params?: any) {
          return instrumentation.wrapGet(originalGet, this, params);
        };
      }

      // Patch update method
      if (CollectionPrototype.update) {
        const originalUpdate = CollectionPrototype.update;
        CollectionPrototype.update = function (this: any, params: any) {
          return instrumentation.wrapUpdate(originalUpdate, this, params);
        };
      }

      // Patch upsert method
      if (CollectionPrototype.upsert) {
        const originalUpsert = CollectionPrototype.upsert;
        CollectionPrototype.upsert = function (this: any, params: any) {
          return instrumentation.wrapUpsert(originalUpsert, this, params);
        };
      }

      // Patch delete method
      if (CollectionPrototype.delete) {
        const originalDelete = CollectionPrototype.delete;
        CollectionPrototype.delete = function (this: any, params?: any) {
          return instrumentation.wrapDelete(originalDelete, this, params);
        };
      }

      // Patch count method
      if (CollectionPrototype.count) {
        const originalCount = CollectionPrototype.count;
        CollectionPrototype.count = function (this: any) {
          return instrumentation.wrapCount(originalCount, this);
        };
      }

      // Patch peek method
      if (CollectionPrototype.peek) {
        const originalPeek = CollectionPrototype.peek;
        CollectionPrototype.peek = function (this: any, params?: any) {
          return instrumentation.wrapPeek(originalPeek, this, params);
        };
      }
    }

    _isPatched = true;
    return moduleExports;
  }

  private unpatch(moduleExports: any, moduleVersion?: string) {
    _isPatched = false;
  }

  private getCollectionName(instance: any): string {
    return instance?.name || instance?._name || "unknown";
  }

  private getCommonAttributes(
    operation: string,
    collectionName: string
  ): Attributes {
    return {
      [VectorDBConventions.DB_SYSTEM]: "chromadb",
      [VectorDBConventions.DB_OPERATION_NAME]: operation,
      [VectorDBConventions.DB_NAMESPACE]: collectionName,
      [VectorDBConventions.DB_VECTOR_COLLECTION_NAME]: collectionName,
      [SemanticConventions.FI_SPAN_KIND]: "VECTOR_DB",
    };
  }

  private async wrapAdd(
    original: Function,
    instance: any,
    params: any
  ): Promise<any> {
    const collectionName = this.getCollectionName(instance);
    const attributes = this.getCommonAttributes("add", collectionName);

    // Extract counts
    const ids = params?.ids || [];
    const embeddings = params?.embeddings;
    const documents = params?.documents;

    attributes[VectorDBConventions.DB_VECTOR_UPSERT_COUNT] = ids.length;

    if (embeddings && embeddings.length > 0 && embeddings[0]) {
      attributes[VectorDBConventions.DB_VECTOR_UPSERT_DIMENSIONS] =
        embeddings[0].length;
    }

    if (documents) {
      attributes["db.vector.documents.count"] = documents.length;
    }

    const span = this.fiTracer.startSpan("chroma add", {
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

  private async wrapQuery(
    original: Function,
    instance: any,
    params: any
  ): Promise<any> {
    const collectionName = this.getCollectionName(instance);
    const attributes = this.getCommonAttributes("query", collectionName);

    const nResults = params?.nResults || params?.n_results || 10;
    attributes[VectorDBConventions.DB_VECTOR_QUERY_TOP_K] = nResults;

    if (params?.where) {
      attributes[VectorDBConventions.DB_VECTOR_QUERY_FILTER] =
        JSON.stringify(params.where);
    }

    if (params?.include) {
      attributes[VectorDBConventions.DB_VECTOR_QUERY_INCLUDE_METADATA] =
        params.include.includes("metadatas");
      attributes[VectorDBConventions.DB_VECTOR_QUERY_INCLUDE_VECTORS] =
        params.include.includes("embeddings");
    }

    const span = this.fiTracer.startSpan("chroma query", {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.call(instance, params)
      );

      // Add result attributes
      if (result?.ids) {
        const resultCount = Array.isArray(result.ids[0])
          ? result.ids[0].length
          : result.ids.length;
        span.setAttribute(
          VectorDBConventions.DB_VECTOR_RESULTS_COUNT,
          resultCount
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

  private async wrapGet(
    original: Function,
    instance: any,
    params?: any
  ): Promise<any> {
    const collectionName = this.getCollectionName(instance);
    const attributes = this.getCommonAttributes("get", collectionName);

    if (params?.ids) {
      attributes["db.vector.get.ids_count"] = params.ids.length;
    }

    if (params?.limit) {
      attributes[VectorDBConventions.DB_VECTOR_QUERY_TOP_K] =
        params.limit;
    }

    const span = this.fiTracer.startSpan("chroma get", {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.call(instance, params)
      );

      if (result?.ids) {
        span.setAttribute(
          VectorDBConventions.DB_VECTOR_RESULTS_COUNT,
          result.ids.length
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
    const collectionName = this.getCollectionName(instance);
    const attributes = this.getCommonAttributes("update", collectionName);

    const ids = params?.ids || [];
    attributes[VectorDBConventions.DB_VECTOR_UPSERT_COUNT] = ids.length;

    const span = this.fiTracer.startSpan("chroma update", {
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

  private async wrapUpsert(
    original: Function,
    instance: any,
    params: any
  ): Promise<any> {
    const collectionName = this.getCollectionName(instance);
    const attributes = this.getCommonAttributes("upsert", collectionName);

    const ids = params?.ids || [];
    attributes[VectorDBConventions.DB_VECTOR_UPSERT_COUNT] = ids.length;

    if (params?.embeddings && params.embeddings.length > 0) {
      attributes[VectorDBConventions.DB_VECTOR_UPSERT_DIMENSIONS] =
        params.embeddings[0].length;
    }

    const span = this.fiTracer.startSpan("chroma upsert", {
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

  private async wrapDelete(
    original: Function,
    instance: any,
    params?: any
  ): Promise<any> {
    const collectionName = this.getCollectionName(instance);
    const attributes = this.getCommonAttributes("delete", collectionName);

    if (params?.ids) {
      attributes[VectorDBConventions.DB_VECTOR_DELETE_COUNT] =
        params.ids.length;
    }

    const span = this.fiTracer.startSpan("chroma delete", {
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

  private async wrapCount(original: Function, instance: any): Promise<any> {
    const collectionName = this.getCollectionName(instance);
    const attributes = this.getCommonAttributes("count", collectionName);

    const span = this.fiTracer.startSpan("chroma count", {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.call(instance)
      );

      if (typeof result === "number") {
        span.setAttribute(
          VectorDBConventions.DB_VECTOR_RESULTS_COUNT,
          result
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

  private async wrapPeek(
    original: Function,
    instance: any,
    params?: any
  ): Promise<any> {
    const collectionName = this.getCollectionName(instance);
    const attributes = this.getCommonAttributes("peek", collectionName);

    if (params?.limit) {
      attributes[VectorDBConventions.DB_VECTOR_QUERY_TOP_K] =
        params.limit;
    }

    const span = this.fiTracer.startSpan("chroma peek", {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.call(instance, params)
      );

      if (result?.ids) {
        span.setAttribute(
          VectorDBConventions.DB_VECTOR_RESULTS_COUNT,
          result.ids.length
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
