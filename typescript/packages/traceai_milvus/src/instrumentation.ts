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

type MilvusOperation =
  | "search"
  | "query"
  | "insert"
  | "upsert"
  | "delete"
  | "get"
  | "createCollection"
  | "dropCollection"
  | "hasCollection"
  | "describeCollection"
  | "createIndex"
  | "dropIndex";

let _isPatched = false;

export function isPatched(): boolean {
  return _isPatched;
}

export interface MilvusInstrumentationConfig extends InstrumentationConfig {
  captureQueryVectors?: boolean;
  captureResultVectors?: boolean;
}

export class MilvusInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;
  private _milvusConfig: MilvusInstrumentationConfig;

  constructor({
    instrumentationConfig,
    traceConfig,
  }: {
    instrumentationConfig?: MilvusInstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super("@traceai/milvus", VERSION, Object.assign({}, instrumentationConfig));
    this._traceConfig = traceConfig;
    this._milvusConfig = instrumentationConfig || {};
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
      "@zilliz/milvus2-sdk-node",
      [">=2.0.0"],
      this.patch.bind(this),
      this.unpatch.bind(this)
    );
  }

  public manuallyInstrument(milvusModule: any): void {
    this.patch(milvusModule);
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

    if (moduleExports.MilvusClient?.prototype) {
      this._patchClient(moduleExports.MilvusClient.prototype);
    }

    _isPatched = true;
    return moduleExports;
  }

  private unpatch(moduleExports: any, moduleVersion?: string) {
    _isPatched = false;
  }

  private _patchClient(prototype: any): void {
    const instrumentation = this;
    const methodsToPatch: { name: string; operation: MilvusOperation }[] = [
      { name: "search", operation: "search" },
      { name: "query", operation: "query" },
      { name: "insert", operation: "insert" },
      { name: "upsert", operation: "upsert" },
      { name: "delete", operation: "delete" },
      { name: "get", operation: "get" },
      { name: "createCollection", operation: "createCollection" },
      { name: "dropCollection", operation: "dropCollection" },
      { name: "hasCollection", operation: "hasCollection" },
      { name: "describeCollection", operation: "describeCollection" },
      { name: "createIndex", operation: "createIndex" },
      { name: "dropIndex", operation: "dropIndex" },
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

  private getCommonAttributes(operation: MilvusOperation, collectionName?: string): Attributes {
    const attrs: Attributes = {
      "db.system": "milvus",
      "db.operation.name": operation,
      [SemanticConventions.FI_SPAN_KIND]: "VECTOR_DB",
    };
    if (collectionName) {
      attrs["db.collection.name"] = collectionName;
    }
    return attrs;
  }

  private async _wrapMethod(
    thisArg: any,
    original: Function,
    operation: MilvusOperation,
    args: any[]
  ): Promise<any> {
    const params = args[0] || {};
    const collectionName = params.collection_name;
    const attributes = this.getCommonAttributes(operation, collectionName);

    // Operation-specific attributes
    if (operation === "search" || operation === "query") {
      if (params.limit) attributes["db.vector.query.top_k"] = params.limit;
      if (params.topk) attributes["db.vector.query.top_k"] = params.topk;
      if (params.vectors?.length) {
        attributes["db.vector.query.dimensions"] = params.vectors[0]?.length || 0;
      }
    }

    if (operation === "insert" || operation === "upsert") {
      if (params.data) attributes["db.operation.batch_size"] = params.data.length;
      if (params.fields_data) attributes["db.operation.batch_size"] = params.fields_data.length;
    }

    const span = this.fiTracer.startSpan(`milvus ${operation}`, {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.apply(thisArg, args)
      );

      if (result?.results) {
        span.setAttribute("db.response.returned_rows", result.results.length);
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

export default MilvusInstrumentation;
