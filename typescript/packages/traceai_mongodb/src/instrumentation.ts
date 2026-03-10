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
import { FITracer, TraceConfigOptions } from "@traceai/fi-core";
import { SemanticConventions } from "@traceai/fi-semantic-conventions";

const VERSION = "0.1.0";

type MongoDBVectorOperation =
  | "aggregate"
  | "find"
  | "insertOne"
  | "insertMany"
  | "updateOne"
  | "updateMany"
  | "deleteOne"
  | "deleteMany"
  | "createSearchIndex"
  | "dropSearchIndex";

let _isPatched = false;

export function isPatched(): boolean {
  return _isPatched;
}

export interface MongoDBInstrumentationConfig extends InstrumentationConfig {
  captureQueryVectors?: boolean;
  captureResultVectors?: boolean;
}

export class MongoDBInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;
  private _mongoConfig: MongoDBInstrumentationConfig;

  constructor({
    instrumentationConfig,
    traceConfig,
  }: {
    instrumentationConfig?: MongoDBInstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super("@traceai/mongodb", VERSION, Object.assign({}, instrumentationConfig));
    this._traceConfig = traceConfig;
    this._mongoConfig = instrumentationConfig || {};
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
      "mongodb",
      [">=5.0.0"],
      this.patch.bind(this),
      this.unpatch.bind(this)
    );
  }

  public manuallyInstrument(mongoModule: any): void {
    this.patch(mongoModule);
  }

  private patch(moduleExports: any, moduleVersion?: string) {
    if (_isPatched) {
      return moduleExports;
    }

    if (!this.fiTracer) {
      this.fiTracer = new FITracer({
        tracer: this.tracer,
        traceConfig: this._traceConfig,
      });
    }

    if (moduleExports.Collection?.prototype) {
      this._patchCollection(moduleExports.Collection.prototype);
    }

    _isPatched = true;
    return moduleExports;
  }

  private unpatch(moduleExports: any, moduleVersion?: string) {
    _isPatched = false;
  }

  private _patchCollection(prototype: any): void {
    const instrumentation = this;
    const methodsToPatch: { name: string; operation: MongoDBVectorOperation }[] = [
      { name: "aggregate", operation: "aggregate" },
      { name: "find", operation: "find" },
      { name: "insertOne", operation: "insertOne" },
      { name: "insertMany", operation: "insertMany" },
      { name: "updateOne", operation: "updateOne" },
      { name: "updateMany", operation: "updateMany" },
      { name: "deleteOne", operation: "deleteOne" },
      { name: "deleteMany", operation: "deleteMany" },
      { name: "createSearchIndex", operation: "createSearchIndex" },
      { name: "dropSearchIndex", operation: "dropSearchIndex" },
    ];

    for (const { name, operation } of methodsToPatch) {
      if (typeof prototype[name] === "function") {
        const original = prototype[name];
        prototype[name] = function (this: any, ...args: any[]) {
          return instrumentation._wrapMethod(this, original, operation, args);
        };
      }
    }
  }

  private _isVectorSearchOperation(operation: MongoDBVectorOperation, args: any[]): boolean {
    if (operation !== "aggregate") return false;

    const pipeline = args[0];
    if (!Array.isArray(pipeline)) return false;

    return pipeline.some(
      (stage: any) => stage.$vectorSearch || stage.$search?.knnBeta
    );
  }

  private getCommonAttributes(operation: MongoDBVectorOperation, collectionName: string, isVectorSearch: boolean): Attributes {
    const attrs: Attributes = {
      "db.system": "mongodb",
      "db.operation.name": operation,
      "db.collection.name": collectionName,
      [SemanticConventions.FI_SPAN_KIND]: "VECTOR_DB",
    };
    if (isVectorSearch) {
      attrs["db.operation.type"] = "vector_search";
    }
    return attrs;
  }

  private _setVectorSearchAttributes(attributes: Attributes, args: any[]): void {
    const pipeline = args[0];
    if (!Array.isArray(pipeline)) return;

    for (const stage of pipeline) {
      if (stage.$vectorSearch) {
        if (stage.$vectorSearch.limit) {
          attributes["db.vector.query.top_k"] = stage.$vectorSearch.limit;
        }
        if (stage.$vectorSearch.numCandidates) {
          attributes["db.vector.query.num_candidates"] = stage.$vectorSearch.numCandidates;
        }
        if (stage.$vectorSearch.index) {
          attributes["db.vector.index_name"] = stage.$vectorSearch.index;
        }
        if (stage.$vectorSearch.queryVector?.length) {
          attributes["db.vector.query.dimensions"] = stage.$vectorSearch.queryVector.length;
        }
      }
    }
  }

  private async _wrapMethod(
    thisArg: any,
    original: Function,
    operation: MongoDBVectorOperation,
    args: any[]
  ): Promise<any> {
    const collectionName = thisArg?.collectionName || thisArg?.s?.namespace?.collection || "unknown";
    const isVectorSearch = this._isVectorSearchOperation(operation, args);
    const attributes = this.getCommonAttributes(operation, collectionName, isVectorSearch);

    if (isVectorSearch) {
      this._setVectorSearchAttributes(attributes, args);
    }

    // Set batch size for bulk operations
    if (operation === "insertMany" && Array.isArray(args[0])) {
      attributes["db.operation.batch_size"] = args[0].length;
    }

    const span = this.fiTracer.startSpan(`mongodb ${operation}`, {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        async () => {
          const res = await original.apply(thisArg, args);

          // Handle cursor results
          if (res && typeof res.toArray === "function") {
            const docs = await res.toArray();
            span.setAttribute("db.response.returned_rows", docs.length);
            return docs;
          }

          return res;
        }
      );

      if (result?.insertedCount) {
        span.setAttribute("db.response.inserted_count", result.insertedCount);
      }
      if (result?.modifiedCount) {
        span.setAttribute("db.response.modified_count", result.modifiedCount);
      }
      if (result?.deletedCount) {
        span.setAttribute("db.response.deleted_count", result.deletedCount);
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

export default MongoDBInstrumentation;
