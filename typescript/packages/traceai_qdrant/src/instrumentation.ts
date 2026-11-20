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

// Ownership is per client prototype, not process-wide: applications can load
// multiple client copies, or register instrumentation more than once.
const patchOwners = new WeakMap<object, QdrantInstrumentation>();
let patchedPrototypeCount = 0;

/** Whether this package currently owns any patched client prototypes. */
export function isPatched(): boolean {
  return patchedPrototypeCount > 0;
}

interface MethodPatch {
  name: string;
  descriptor?: PropertyDescriptor;
  wrapped: Function;
  active: boolean;
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
  private readonly knownPrototypes = new Set<any>();
  private readonly patches = new Map<any, MethodPatch[]>();

  constructor({
    instrumentationConfig,
    traceConfig,
  }: {
    instrumentationConfig?: QdrantInstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    // The base constructor calls enable(); defer it until our state and privacy
    // configuration exist, including when a client was already loaded.
    super("@traceai/qdrant", VERSION, { ...instrumentationConfig, enabled: false });
    this._traceConfig = traceConfig;
    this.setConfig(instrumentationConfig || {});
    this.refreshTracer();
    if (this.getConfig().enabled) this.enable();
  }

  public override enable(): void {
    if (this.isEnabled()) return;
    this.refreshTracer();
    super.enable();
    // OTel retains only the last module export; keep all copies and manually
    // registered clients participating in disable/enable cycles.
    for (const prototype of this.knownPrototypes) this.patchPrototype(prototype);
  }

  public override disable(): void {
    super.disable();
    for (const prototype of this.patches.keys()) this.unpatchPrototype(prototype);
  }

  public override setTracerProvider(provider: TracerProvider): void {
    super.setTracerProvider(provider);
    this.refreshTracer();
  }

  private refreshTracer(): void {
    this.fiTracer = new FITracer({
      tracer: this.tracer,
      traceConfig: this._traceConfig,
    });
  }

  protected init() {
    return new InstrumentationNodeModuleDefinition(
      MODULE_NAME,
      ["^1.0.0"],
      this.patch.bind(this),
      this.unpatch.bind(this)
    );
  }

  /**
   * Manually instruments the Qdrant module
   */
  manuallyInstrument(module: any): void {
    this.patch(module);
  }

  private patch(moduleExports: any, _moduleVersion?: string) {
    const prototype = moduleExports?.QdrantClient?.prototype;
    if (prototype) {
      this.knownPrototypes.add(prototype);
      if (this.isEnabled()) this.patchPrototype(prototype);
    }
    return moduleExports;
  }

  private patchPrototype(prototype: any): void {
    if (patchOwners.has(prototype)) return;

    const instrumentation = this;
    const wrappers = {
      search: this.wrapSearch,
      query: this.wrapQuery,
      queryPoints: this.wrapQueryPoints,
      queryBatch: this.wrapQueryBatch,
      queryGroups: this.wrapQueryGroups,
      upsert: this.wrapUpsert,
      delete: this.wrapDelete,
      retrieve: this.wrapRetrieve,
      scroll: this.wrapScroll,
      count: this.wrapCount,
      getCollection: this.wrapGetCollection,
    };
    const patches: MethodPatch[] = [];
    for (const [name, wrapOperation] of Object.entries(wrappers)) {
      const original = prototype[name];
      if (typeof original !== "function") continue;
      const descriptor = Object.getOwnPropertyDescriptor(prototype, name);
      // Do not mutate accessors or methods that cannot be replaced.
      if (descriptor && (!("value" in descriptor) || !descriptor.writable)) continue;
      const patch: MethodPatch = {
        name,
        descriptor,
        active: true,
        wrapped: function (this: any, ...args: any[]) {
          if (!patch.active || !instrumentation.isEnabled()) {
            return original.apply(this, args);
          }
          // Keep the exact receiver and argument list, including future SDK
          // options; the operation helpers only inspect collection and params.
          return wrapOperation.call(
            instrumentation,
            () => original.apply(this, args),
            this,
            args[0],
            args[1]
          );
        },
      };
      Object.defineProperty(prototype, name, {
        configurable: true,
        enumerable: true,
        writable: true,
        ...descriptor,
        value: patch.wrapped,
      });
      patches.push(patch);
    }
    if (patches.length) {
      this.patches.set(prototype, patches);
      patchOwners.set(prototype, this);
      patchedPrototypeCount++;
    }
  }

  private unpatch(moduleExports: any, _moduleVersion?: string) {
    this.unpatchPrototype(moduleExports?.QdrantClient?.prototype);
  }

  private unpatchPrototype(prototype: any): void {
    const patches = this.patches.get(prototype);
    if (!patches) return;
    for (const patch of patches) {
      // A later wrapper may still call ours. Leave that wrapper alone and make
      // its captured copy inert, even after we enable again.
      patch.active = false;
      if (prototype[patch.name] !== patch.wrapped) continue;
      if (patch.descriptor) Object.defineProperty(prototype, patch.name, patch.descriptor);
      else delete prototype[patch.name];
    }
    this.patches.delete(prototype);
    patchOwners.delete(prototype);
    patchedPrototypeCount--;
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
    params: any,
    operation = "query"
  ): Promise<any> {
    const attributes = this.getCommonAttributes(operation, collectionName);

    if (operation === "query_batch") {
      attributes["db.vector.query.batch_size"] = params?.searches?.length ?? 0;
    } else if (operation === "query_groups") {
      // The limit counts groups, not points. Do not report it as top_k.
      attributes["db.vector.query.group_limit"] = params?.limit ?? 10;
      attributes["db.vector.query.group_size"] = params?.group_size ?? 3;
    } else {
      attributes[VectorDBConventions.DB_VECTOR_QUERY_TOP_K] = params?.limit ?? 10;
    }
    if (params?.filter) {
      attributes[VectorDBConventions.DB_VECTOR_QUERY_FILTER] = JSON.stringify(params.filter);
    }

    const span = this.fiTracer.startSpan(`qdrant ${operation}`, {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.call(instance, collectionName, params)
      );
      let count: number | undefined;
      if (operation === "query_batch" && Array.isArray(result)) {
        count = result.reduce((sum, response) => sum + (response?.points?.length ?? 0), 0);
      } else if (operation === "query_groups" && Array.isArray(result?.groups)) {
        count = result.groups.reduce((sum: number, group: any) => sum + (group?.hits?.length ?? 0), 0);
      } else if (Array.isArray(result?.points)) {
        count = result.points.length;
      } else if (Array.isArray(result)) {
        count = result.length;
      }
      if (count !== undefined) span.setAttribute(VectorDBConventions.DB_VECTOR_RESULTS_COUNT, count);
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

  private wrapQueryPoints(original: Function, instance: any, collectionName: string, params: any) {
    return this.wrapQuery(original, instance, collectionName, params, "query_points");
  }

  private wrapQueryBatch(original: Function, instance: any, collectionName: string, params: any) {
    return this.wrapQuery(original, instance, collectionName, params, "query_batch");
  }

  private wrapQueryGroups(original: Function, instance: any, collectionName: string, params: any) {
    return this.wrapQuery(original, instance, collectionName, params, "query_groups");
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
