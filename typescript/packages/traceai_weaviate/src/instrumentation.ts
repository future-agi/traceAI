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

// Weaviate operation types
type WeaviateOperation =
  | "nearVector"
  | "nearText"
  | "hybrid"
  | "bm25"
  | "fetchObjects"
  | "insert"
  | "insertMany"
  | "deleteById"
  | "deleteMany"
  | "aggregate"
  | "query";

// Track patching state
let _isPatched = false;

export function isPatched(): boolean {
  return _isPatched;
}

export interface WeaviateInstrumentationConfig extends InstrumentationConfig {
  captureQueryVectors?: boolean;
  captureResultVectors?: boolean;
  captureDocuments?: boolean;
}

export class WeaviateInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;
  private _weaviateConfig: WeaviateInstrumentationConfig;

  constructor({
    instrumentationConfig,
    traceConfig,
  }: {
    instrumentationConfig?: WeaviateInstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super("@traceai/weaviate", VERSION, Object.assign({}, instrumentationConfig));
    this._traceConfig = traceConfig;
    this._weaviateConfig = instrumentationConfig || {};
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
      "weaviate-client",
      [">=3.0.0"],
      this.patch.bind(this),
      this.unpatch.bind(this)
    );
  }

  public manuallyInstrument(weaviateModule: any): void {
    this.patch(weaviateModule);
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

    // Patch WeaviateClient if it exists
    if (moduleExports.WeaviateClient?.prototype) {
      this._patchClient(moduleExports.WeaviateClient.prototype);
    }

    // Patch collection query methods if they exist
    if (moduleExports.Collection?.prototype) {
      this._patchCollection(moduleExports.Collection.prototype);
    }

    _isPatched = true;
    return moduleExports;
  }

  private unpatch(moduleExports: any, moduleVersion?: string) {
    _isPatched = false;
  }

  private _patchClient(prototype: any): void {
    // Patch client-level methods if needed
  }

  private _patchCollection(prototype: any): void {
    const instrumentation = this;

    // Patch query method (generic queries)
    if (typeof prototype.query === "function") {
      const original = prototype.query;
      prototype.query = function (this: any, ...args: any[]) {
        return instrumentation._wrapCollectionMethod(
          this,
          original,
          "query",
          args
        );
      };
    }

    // Patch nearVector search
    if (prototype.query?.nearVector) {
      const original = prototype.query.nearVector;
      prototype.query.nearVector = function (...args: any[]) {
        return instrumentation._wrapCollectionMethod(
          this,
          original,
          "nearVector",
          args
        );
      };
    }

    // Patch nearText search
    if (prototype.query?.nearText) {
      const original = prototype.query.nearText;
      prototype.query.nearText = function (...args: any[]) {
        return instrumentation._wrapCollectionMethod(
          this,
          original,
          "nearText",
          args
        );
      };
    }

    // Patch hybrid search
    if (prototype.query?.hybrid) {
      const original = prototype.query.hybrid;
      prototype.query.hybrid = function (...args: any[]) {
        return instrumentation._wrapCollectionMethod(
          this,
          original,
          "hybrid",
          args
        );
      };
    }

    // Patch bm25 search
    if (prototype.query?.bm25) {
      const original = prototype.query.bm25;
      prototype.query.bm25 = function (...args: any[]) {
        return instrumentation._wrapCollectionMethod(
          this,
          original,
          "bm25",
          args
        );
      };
    }

    // Patch fetchObjects
    if (prototype.query?.fetchObjects) {
      const original = prototype.query.fetchObjects;
      prototype.query.fetchObjects = function (...args: any[]) {
        return instrumentation._wrapCollectionMethod(
          this,
          original,
          "fetchObjects",
          args
        );
      };
    }

    // Patch data.insert
    if (prototype.data?.insert) {
      const original = prototype.data.insert;
      prototype.data.insert = function (...args: any[]) {
        return instrumentation._wrapCollectionMethod(
          this,
          original,
          "insert",
          args
        );
      };
    }

    // Patch data.insertMany
    if (prototype.data?.insertMany) {
      const original = prototype.data.insertMany;
      prototype.data.insertMany = function (...args: any[]) {
        return instrumentation._wrapCollectionMethod(
          this,
          original,
          "insertMany",
          args
        );
      };
    }

    // Patch data.deleteById
    if (prototype.data?.deleteById) {
      const original = prototype.data.deleteById;
      prototype.data.deleteById = function (...args: any[]) {
        return instrumentation._wrapCollectionMethod(
          this,
          original,
          "deleteById",
          args
        );
      };
    }

    // Patch data.deleteMany
    if (prototype.data?.deleteMany) {
      const original = prototype.data.deleteMany;
      prototype.data.deleteMany = function (...args: any[]) {
        return instrumentation._wrapCollectionMethod(
          this,
          original,
          "deleteMany",
          args
        );
      };
    }

    // Patch aggregate
    if (prototype.aggregate) {
      const original = prototype.aggregate;
      prototype.aggregate = function (...args: any[]) {
        return instrumentation._wrapCollectionMethod(
          this,
          original,
          "aggregate",
          args
        );
      };
    }
  }

  private getCommonAttributes(operation: WeaviateOperation, collectionName: string): Attributes {
    return {
      "db.system": "weaviate",
      "db.operation.name": operation,
      "db.collection.name": collectionName,
      [SemanticConventions.FI_SPAN_KIND]: "VECTOR_DB",
    };
  }

  private _setOperationAttributes(
    attributes: Attributes,
    operation: WeaviateOperation,
    args: any[]
  ): void {
    const params = args[0] || {};

    switch (operation) {
      case "nearVector":
        if (params.vector && !this._weaviateConfig.captureQueryVectors) {
          attributes["db.vector.query.dimensions"] = params.vector.length;
        }
        if (params.limit) {
          attributes["db.vector.query.top_k"] = params.limit;
        }
        if (params.certainty) {
          attributes["db.vector.query.certainty"] = params.certainty;
        }
        if (params.distance) {
          attributes["db.vector.query.distance"] = params.distance;
        }
        break;

      case "nearText":
        if (params.query) {
          attributes["db.vector.query.text"] = params.query;
        }
        if (params.limit) {
          attributes["db.vector.query.top_k"] = params.limit;
        }
        break;

      case "hybrid":
        if (params.query) {
          attributes["db.vector.query.text"] = params.query;
        }
        if (params.alpha !== undefined) {
          attributes["db.vector.query.alpha"] = params.alpha;
        }
        if (params.limit) {
          attributes["db.vector.query.top_k"] = params.limit;
        }
        break;

      case "bm25":
        if (params.query) {
          attributes["db.vector.query.text"] = params.query;
        }
        if (params.limit) {
          attributes["db.vector.query.top_k"] = params.limit;
        }
        break;

      case "fetchObjects":
        if (params.limit) {
          attributes["db.vector.query.top_k"] = params.limit;
        }
        if (params.offset) {
          attributes["db.vector.query.offset"] = params.offset;
        }
        break;

      case "insert":
        attributes["db.operation.batch_size"] = 1;
        break;

      case "insertMany":
        if (Array.isArray(params)) {
          attributes["db.operation.batch_size"] = params.length;
        } else if (params.objects) {
          attributes["db.operation.batch_size"] = params.objects.length;
        }
        break;

      case "deleteById":
        attributes["db.operation.batch_size"] = 1;
        break;

      case "deleteMany":
        if (params.where) {
          attributes["db.operation.has_filter"] = true;
        }
        break;

      case "aggregate":
        // Aggregate-specific attributes
        break;
    }
  }

  private _setResultAttributes(
    span: any,
    operation: WeaviateOperation,
    result: any
  ): void {
    if (!result) return;

    switch (operation) {
      case "nearVector":
      case "nearText":
      case "hybrid":
      case "bm25":
      case "fetchObjects":
        if (result.objects) {
          span.setAttribute("db.response.returned_rows", result.objects.length);
        } else if (Array.isArray(result)) {
          span.setAttribute("db.response.returned_rows", result.length);
        }
        break;

      case "insertMany":
        if (result.uuids) {
          span.setAttribute("db.response.inserted_count", result.uuids.length);
        }
        break;
    }
  }

  private async _wrapCollectionMethod(
    thisArg: any,
    original: Function,
    operation: WeaviateOperation,
    args: any[]
  ): Promise<any> {
    const collectionName = thisArg?.name || "unknown";
    const attributes = this.getCommonAttributes(operation, collectionName);
    this._setOperationAttributes(attributes, operation, args);

    const span = this.fiTracer.startSpan(`weaviate ${operation}`, {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.apply(thisArg, args)
      );

      this._setResultAttributes(span, operation, result);

      span.setStatus({ code: SpanStatusCode.OK });
      return result;
    } catch (error: any) {
      span.recordException(error);
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: error?.message || "Weaviate operation failed",
      });
      throw error;
    } finally {
      span.end();
    }
  }
}

export default WeaviateInstrumentation;
