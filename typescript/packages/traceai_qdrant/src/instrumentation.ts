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
  DB_VECTOR_QUERY_SCORE_THRESHOLD: "db.vector.query.score_threshold",
  DB_VECTOR_RESULTS_COUNT: "db.vector.results.count",
  DB_VECTOR_UPSERT_COUNT: "db.vector.upsert.count",
  DB_VECTOR_UPSERT_DIMENSIONS: "db.vector.upsert.dimensions",
  DB_VECTOR_DELETE_COUNT: "db.vector.delete.count",
  DB_VECTOR_COLLECTION_NAME: "db.vector.collection.name",
  DB_VECTOR_INDEX_DIMENSIONS: "db.vector.index.dimensions",
} as const;

const MODULE_NAME = "@qdrant/js-client-rest";

let _isPatched = false;

export function isPatched() {
  return _isPatched;
}

/**
 * Configuration options for Qdrant instrumentation
 */
export interface QdrantInstrumentationConfig extends InstrumentationConfig {
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
 * OpenTelemetry instrumentation for Qdrant vector database
 */
export class QdrantInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;
  private _qdrantConfig: QdrantInstrumentationConfig;

  constructor({
    instrumentationConfig,
    traceConfig,
  }: {
    instrumentationConfig?: QdrantInstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super("@traceai/qdrant", VERSION, Object.assign({}, instrumentationConfig));
    this._traceConfig = traceConfig;
    this._qdrantConfig = instrumentationConfig || {};
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
      "@qdrant/js-client-rest",
      ["^1.0.0"],
      this.patch.bind(this),
      this.unpatch.bind(this)
    );
  }

  /**
   * Manually instruments the Qdrant module
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

    // Qdrant has a QdrantClient class with methods for operations
    if (moduleExports.QdrantClient) {
      const ClientPrototype = moduleExports.QdrantClient.prototype;

      // Patch search (query)
      if (ClientPrototype.search) {
        const originalSearch = ClientPrototype.search;
        ClientPrototype.search = function (
          this: any,
          collectionName: string,
          params: any
        ) {
          return instrumentation.wrapSearch(
            originalSearch,
            this,
            collectionName,
            params
          );
        };
      }

      // Patch query (newer API)
      if (ClientPrototype.query) {
        const originalQuery = ClientPrototype.query;
        ClientPrototype.query = function (
          this: any,
          collectionName: string,
          params: any
        ) {
          return instrumentation.wrapQuery(
            originalQuery,
            this,
            collectionName,
            params
          );
        };
      }

      // Patch queryPoints
      if (ClientPrototype.queryPoints) {
        const originalQueryPoints = ClientPrototype.queryPoints;
        ClientPrototype.queryPoints = function (
          this: any,
          collectionName: string,
          params: any
        ) {
          return instrumentation.wrapQueryPoints(
            originalQueryPoints,
            this,
            collectionName,
            params
          );
        };
      }

      // Patch upsert
      if (ClientPrototype.upsert) {
        const originalUpsert = ClientPrototype.upsert;
        ClientPrototype.upsert = function (
          this: any,
          collectionName: string,
          params: any
        ) {
          return instrumentation.wrapUpsert(
            originalUpsert,
            this,
            collectionName,
            params
          );
        };
      }

      // Patch delete
      if (ClientPrototype.delete) {
        const originalDelete = ClientPrototype.delete;
        ClientPrototype.delete = function (
          this: any,
          collectionName: string,
          params: any
        ) {
          return instrumentation.wrapDelete(
            originalDelete,
            this,
            collectionName,
            params
          );
        };
      }

      // Patch retrieve
      if (ClientPrototype.retrieve) {
        const originalRetrieve = ClientPrototype.retrieve;
        ClientPrototype.retrieve = function (
          this: any,
          collectionName: string,
          params: any
        ) {
          return instrumentation.wrapRetrieve(
            originalRetrieve,
            this,
            collectionName,
            params
          );
        };
      }

      // Patch scroll
      if (ClientPrototype.scroll) {
        const originalScroll = ClientPrototype.scroll;
        ClientPrototype.scroll = function (
          this: any,
          collectionName: string,
          params?: any
        ) {
          return instrumentation.wrapScroll(
            originalScroll,
            this,
            collectionName,
            params
          );
        };
      }

      // Patch count
      if (ClientPrototype.count) {
        const originalCount = ClientPrototype.count;
        ClientPrototype.count = function (
          this: any,
          collectionName: string,
          params?: any
        ) {
          return instrumentation.wrapCount(
            originalCount,
            this,
            collectionName,
            params
          );
        };
      }

      // Patch getCollection
      if (ClientPrototype.getCollection) {
        const originalGetCollection = ClientPrototype.getCollection;
        ClientPrototype.getCollection = function (
          this: any,
          collectionName: string
        ) {
          return instrumentation.wrapGetCollection(
            originalGetCollection,
            this,
            collectionName
          );
        };
      }
    }

    _isPatched = true;
    return moduleExports;
  }

  private unpatch(moduleExports: any, moduleVersion?: string) {
    _isPatched = false;
  }

  private getCommonAttributes(
    operation: string,
    collectionName: string
  ): Attributes {
    return {
      [VectorDBConventions.DB_SYSTEM]: "qdrant",
      [VectorDBConventions.DB_OPERATION_NAME]: operation,
      [VectorDBConventions.DB_NAMESPACE]: collectionName,
      [VectorDBConventions.DB_VECTOR_COLLECTION_NAME]: collectionName,
      [SemanticConventions.FI_SPAN_KIND]: "VECTOR_DB",
    };
  }

  private async wrapSearch(
    original: Function,
    instance: any,
    collectionName: string,
    params: any
  ): Promise<any> {
    const attributes = this.getCommonAttributes("search", collectionName);

    const limit = params?.limit || 10;
    attributes[VectorDBConventions.DB_VECTOR_QUERY_TOP_K] = limit;

    if (params?.filter) {
      attributes[VectorDBConventions.DB_VECTOR_QUERY_FILTER] =
        JSON.stringify(params.filter);
    }

    if (params?.with_payload !== undefined) {
      attributes[VectorDBConventions.DB_VECTOR_QUERY_INCLUDE_METADATA] =
        params.with_payload;
    }

    if (params?.with_vector !== undefined) {
      attributes[VectorDBConventions.DB_VECTOR_QUERY_INCLUDE_VECTORS] =
        params.with_vector;
    }

    if (params?.score_threshold !== undefined) {
      attributes[VectorDBConventions.DB_VECTOR_QUERY_SCORE_THRESHOLD] =
        params.score_threshold;
    }

    const span = this.fiTracer.startSpan("qdrant search", {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.call(instance, collectionName, params)
      );

      if (Array.isArray(result)) {
        span.setAttribute(
          VectorDBConventions.DB_VECTOR_RESULTS_COUNT,
          result.length
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

  private async wrapQuery(
    original: Function,
    instance: any,
    collectionName: string,
    params: any
  ): Promise<any> {
    const attributes = this.getCommonAttributes("query", collectionName);

    const limit = params?.limit || 10;
    attributes[VectorDBConventions.DB_VECTOR_QUERY_TOP_K] = limit;

    if (params?.filter) {
      attributes[VectorDBConventions.DB_VECTOR_QUERY_FILTER] =
        JSON.stringify(params.filter);
    }

    const span = this.fiTracer.startSpan("qdrant query", {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.call(instance, collectionName, params)
      );

      if (result?.points) {
        span.setAttribute(
          VectorDBConventions.DB_VECTOR_RESULTS_COUNT,
          result.points.length
        );
      } else if (Array.isArray(result)) {
        span.setAttribute(
          VectorDBConventions.DB_VECTOR_RESULTS_COUNT,
          result.length
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

  private async wrapQueryPoints(
    original: Function,
    instance: any,
    collectionName: string,
    params: any
  ): Promise<any> {
    const attributes = this.getCommonAttributes("query_points", collectionName);

    const limit = params?.limit || 10;
    attributes[VectorDBConventions.DB_VECTOR_QUERY_TOP_K] = limit;

    if (params?.filter) {
      attributes[VectorDBConventions.DB_VECTOR_QUERY_FILTER] =
        JSON.stringify(params.filter);
    }

    const span = this.fiTracer.startSpan("qdrant query_points", {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.call(instance, collectionName, params)
      );

      if (result?.points) {
        span.setAttribute(
          VectorDBConventions.DB_VECTOR_RESULTS_COUNT,
          result.points.length
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
    collectionName: string,
    params: any
  ): Promise<any> {
    const attributes = this.getCommonAttributes("upsert", collectionName);

    const points = params?.points || [];
    attributes[VectorDBConventions.DB_VECTOR_UPSERT_COUNT] =
      points.length;

    if (points.length > 0 && points[0]?.vector) {
      const vector = points[0].vector;
      const dimensions = Array.isArray(vector) ? vector.length : 0;
      if (dimensions > 0) {
        attributes[VectorDBConventions.DB_VECTOR_UPSERT_DIMENSIONS] =
          dimensions;
      }
    }

    const span = this.fiTracer.startSpan("qdrant upsert", {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.call(instance, collectionName, params)
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
    collectionName: string,
    params: any
  ): Promise<any> {
    const attributes = this.getCommonAttributes("delete", collectionName);

    if (params?.points) {
      attributes[VectorDBConventions.DB_VECTOR_DELETE_COUNT] =
        Array.isArray(params.points) ? params.points.length : 1;
    }

    if (params?.filter) {
      attributes[VectorDBConventions.DB_VECTOR_QUERY_FILTER] =
        JSON.stringify(params.filter);
    }

    const span = this.fiTracer.startSpan("qdrant delete", {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.call(instance, collectionName, params)
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

  private async wrapRetrieve(
    original: Function,
    instance: any,
    collectionName: string,
    params: any
  ): Promise<any> {
    const attributes = this.getCommonAttributes("retrieve", collectionName);

    const ids = params?.ids || [];
    attributes["db.vector.retrieve.ids_count"] = ids.length;

    const span = this.fiTracer.startSpan("qdrant retrieve", {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.call(instance, collectionName, params)
      );

      if (Array.isArray(result)) {
        span.setAttribute(
          VectorDBConventions.DB_VECTOR_RESULTS_COUNT,
          result.length
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

  private async wrapScroll(
    original: Function,
    instance: any,
    collectionName: string,
    params?: any
  ): Promise<any> {
    const attributes = this.getCommonAttributes("scroll", collectionName);

    if (params?.limit) {
      attributes[VectorDBConventions.DB_VECTOR_QUERY_TOP_K] =
        params.limit;
    }

    if (params?.filter) {
      attributes[VectorDBConventions.DB_VECTOR_QUERY_FILTER] =
        JSON.stringify(params.filter);
    }

    const span = this.fiTracer.startSpan("qdrant scroll", {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.call(instance, collectionName, params)
      );

      if (result?.points) {
        span.setAttribute(
          VectorDBConventions.DB_VECTOR_RESULTS_COUNT,
          result.points.length
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

  private async wrapCount(
    original: Function,
    instance: any,
    collectionName: string,
    params?: any
  ): Promise<any> {
    const attributes = this.getCommonAttributes("count", collectionName);

    if (params?.filter) {
      attributes[VectorDBConventions.DB_VECTOR_QUERY_FILTER] =
        JSON.stringify(params.filter);
    }

    const span = this.fiTracer.startSpan("qdrant count", {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.call(instance, collectionName, params)
      );

      if (result?.count !== undefined) {
        span.setAttribute(
          VectorDBConventions.DB_VECTOR_RESULTS_COUNT,
          result.count
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

  private async wrapGetCollection(
    original: Function,
    instance: any,
    collectionName: string
  ): Promise<any> {
    const attributes = this.getCommonAttributes(
      "get_collection",
      collectionName
    );

    const span = this.fiTracer.startSpan("qdrant get_collection", {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.call(instance, collectionName)
      );

      if (result?.vectors_count !== undefined) {
        span.setAttribute(
          VectorDBConventions.DB_VECTOR_RESULTS_COUNT,
          result.vectors_count
        );
      }

      if (result?.config?.params?.vectors?.size) {
        span.setAttribute(
          VectorDBConventions.DB_VECTOR_INDEX_DIMENSIONS,
          result.config.params.vectors.size
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
