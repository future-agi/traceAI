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
import { AsyncLocalStorage } from "node:async_hooks";
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

// Suppression must also work when the caller uses a BasicTracerProvider without
// installing an OpenTelemetry context manager. Do not change global OTel state.
const activeCollection = new AsyncLocalStorage<object>();
const wrappedFunctions = new WeakSet<Function>();
let patchCount = 0;

type Patch = {
  target: any;
  name: string;
  descriptor: PropertyDescriptor;
  wrapper: Function;
  active: boolean;
};

export function isPatched(): boolean {
  return patchCount > 0;
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
  // Lazy initialization: InstrumentationBase calls enable() from its constructor.
  private patches?: Patch[];
  private modules?: Set<any>;
  private collectionPrototypes?: Set<any>;

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
    this.refreshTracer();
  }

  private refreshTracer(): void {
    this.fiTracer = new FITracer({
      tracer: this.tracer,
      traceConfig: this._traceConfig,
    });
  }

  public override setTracerProvider(provider: TracerProvider): void {
    super.setTracerProvider(provider);
    this.refreshTracer();
  }

  public override enable(): void {
    this.refreshTracer();
    super.enable();
    this.modules?.forEach((module) => this.patch(module));
    this.collectionPrototypes?.forEach((prototype) => this.patchCollection(prototype));
  }

  public override disable(): void {
    super.disable();
    // The base class only unpatches modules discovered by its import hooks.
    // Manual modules and collection implementations discovered by factories
    // must be restored as well.
    this.unpatch();
  }

  protected init() {
    return new InstrumentationNodeModuleDefinition(
      "chromadb",
      ["^1.0.0", "^2.0.0", "^3.0.0"],
      this.patch.bind(this),
      this.unpatch.bind(this)
    );
  }

  /**
   * Manually instruments the ChromaDB module
   */
  manuallyInstrument(module: any): void {
    this.patch(module);
  }

  private patch(moduleExports: any) {
    if (!moduleExports) return moduleExports;
    (this.modules ??= new Set()).add(moduleExports);
    if (!this.isEnabled()) return moduleExports;

    // Collection is a runtime export in v1/v2, but type-only in v3.
    // CollectionHandle and the factories cover v3's unexported implementations.
    for (const name of ["Collection", "CollectionHandle"]) {
      this.patchCollection(moduleExports[name]?.prototype);
    }
    for (const name of ["ChromaClient", "CloudClient"]) {
      const prototype = moduleExports[name]?.prototype;
      for (const factory of [
        "createCollection", "getCollection", "getOrCreateCollection",
        "getCollectionById", "getCollectionByCrn", "getCollections",
        "listCollections", "collection",
      ]) {
        this.patchFactory(prototype, factory);
      }
    }
    return moduleExports;
  }

  private patchCollection(prototype: any): void {
    if (!prototype || prototype === Object.prototype) return;
    (this.collectionPrototypes ??= new Set()).add(prototype);
    const handlers = {
      add: this.wrapAdd, query: this.wrapQuery, get: this.wrapGet,
      update: this.wrapUpdate, upsert: this.wrapUpsert, delete: this.wrapDelete,
      count: this.wrapCount, peek: this.wrapPeek, search: this.wrapSearch,
    };
    const instrumentation = this;
    for (const [name, handler] of Object.entries(handlers)) {
      this.patchMethod(prototype, name, (original) => function (this: any, ...args: any[]) {
        // Handles delegate to their base implementation; peek delegates to get.
        // Keep one database span without suppressing unrelated child spans.
        if (activeCollection.getStore() === this) {
          return original.apply(this, args);
        }
        return activeCollection.run(this, () =>
          handler.call(instrumentation, () => original.apply(this, args), this, args[0])
        );
      });
    }
    this.patchFactory(prototype, "fork");
  }

  private patchFactory(prototype: any, name: string): void {
    const instrumentation = this;
    this.patchMethod(prototype, name, (original) => function (this: any, ...args: any[]) {
      const result = original.apply(this, args);
      const instrumentResult = (value: any) => {
        // A factory may finish after disable(). Never install late wrappers.
        if (instrumentation.isEnabled()) {
          for (const collection of Array.isArray(value) ? value : [value]) {
            if (collection && typeof collection === "object") {
              instrumentation.patchCollection(Object.getPrototypeOf(collection));
            }
          }
        }
        return value;
      };
      // collection(id) is synchronous; do not turn it into a Promise.
      return result && typeof result.then === "function"
        ? result.then(instrumentResult)
        : instrumentResult(result);
    });
  }

  private patchMethod(
    prototype: any,
    name: string,
    wrap: (original: Function) => Function
  ): void {
    // Patch the owner, not an inherited copy. This also instruments unexported
    // base classes without retaining every collection instance in memory.
    let target = prototype;
    while (target && target !== Object.prototype) {
      const descriptor = Object.getOwnPropertyDescriptor(target, name);
      if (!descriptor) {
        target = Object.getPrototypeOf(target);
        continue;
      }
      const original = descriptor.value;
      if (typeof original !== "function" || wrappedFunctions.has(original) ||
          (!descriptor.configurable && !descriptor.writable)) return;
      const wrapped = wrap(original);
      const instrumentation = this;
      const patch: Patch = {
        target, name, descriptor, active: true,
        wrapper: function (this: any, ...args: any[]) {
          return patch.active && instrumentation.isEnabled()
            ? wrapped.apply(this, args)
            : original.apply(this, args);
        },
      };
      Object.defineProperty(target, name, { ...descriptor, value: patch.wrapper });
      wrappedFunctions.add(patch.wrapper);
      (this.patches ??= []).push(patch);
      patchCount++;
      return;
    }
  }

  private unpatch(_moduleExports?: any): void {
    for (const patch of (this.patches ?? []).reverse()) {
      patch.active = false;
      // Do not remove a wrapper installed by another library after ours.
      if (patch.target[patch.name] === patch.wrapper) {
        Object.defineProperty(patch.target, patch.name, patch.descriptor);
      }
      patchCount--;
    }
    this.patches = [];
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

  private async wrapSearch(
    original: Function,
    instance: any,
    searches: any
  ): Promise<any> {
    const attributes = this.getCommonAttributes("search", this.getCollectionName(instance));
    attributes["db.vector.search.count"] = Array.isArray(searches) ? searches.length : 1;
    const span = this.fiTracer.startSpan("chroma search", {
      kind: SpanKind.CLIENT,
      attributes,
    });
    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.call(instance, searches)
      );
      if (Array.isArray(result?.ids)) {
        span.setAttribute(VectorDBConventions.DB_VECTOR_RESULTS_COUNT,
          result.ids.reduce((count: number, ids: unknown) => count + (Array.isArray(ids) ? ids.length : 1), 0));
      }
      span.setStatus({ code: SpanStatusCode.OK });
      return result;
    } catch (error: unknown) {
      const exception = error instanceof Error || typeof error === "string" ? error : { name: "Error" };
      span.recordException(exception);
      span.setStatus({ code: SpanStatusCode.ERROR,
        message: error instanceof Error ? error.message : typeof error === "string" ? error : undefined });
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
