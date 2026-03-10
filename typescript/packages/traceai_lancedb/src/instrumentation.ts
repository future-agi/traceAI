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

type LanceDBOperation =
  | "search"
  | "add"
  | "update"
  | "delete"
  | "createTable"
  | "dropTable"
  | "openTable"
  | "tableNames"
  | "countRows";

let _isPatched = false;

export function isPatched(): boolean {
  return _isPatched;
}

export interface LanceDBInstrumentationConfig extends InstrumentationConfig {
  captureQueryVectors?: boolean;
  captureResultVectors?: boolean;
}

export class LanceDBInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;
  private _lanceConfig: LanceDBInstrumentationConfig;

  constructor({
    instrumentationConfig,
    traceConfig,
  }: {
    instrumentationConfig?: LanceDBInstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super("@traceai/lancedb", VERSION, Object.assign({}, instrumentationConfig));
    this._traceConfig = traceConfig;
    this._lanceConfig = instrumentationConfig || {};
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
      "@lancedb/lancedb",
      [">=0.1.0"],
      this.patch.bind(this),
      this.unpatch.bind(this)
    );
  }

  public manuallyInstrument(lanceModule: any): void {
    this.patch(lanceModule);
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

    // Patch Connection class
    if (moduleExports.Connection?.prototype) {
      this._patchConnection(moduleExports.Connection.prototype);
    }

    // Patch Table class
    if (moduleExports.Table?.prototype) {
      this._patchTable(moduleExports.Table.prototype);
    }

    _isPatched = true;
    return moduleExports;
  }

  private unpatch(moduleExports: any, moduleVersion?: string) {
    _isPatched = false;
  }

  private _patchConnection(prototype: any): void {
    const instrumentation = this;
    const methods: LanceDBOperation[] = ["createTable", "dropTable", "openTable", "tableNames"];

    for (const method of methods) {
      if (typeof prototype[method] === "function") {
        const original = prototype[method];
        prototype[method] = function (this: any, ...args: any[]) {
          return instrumentation._wrapMethod(this, original, method, args);
        };
      }
    }
  }

  private _patchTable(prototype: any): void {
    const instrumentation = this;
    const methods: LanceDBOperation[] = ["search", "add", "update", "delete", "countRows"];

    for (const method of methods) {
      if (typeof prototype[method] === "function") {
        const original = prototype[method];
        prototype[method] = function (this: any, ...args: any[]) {
          return instrumentation._wrapMethod(this, original, method, args, this.name);
        };
      }
    }
  }

  private getCommonAttributes(operation: LanceDBOperation, tableName?: string): Attributes {
    const attrs: Attributes = {
      "db.system": "lancedb",
      "db.operation.name": operation,
      [SemanticConventions.FI_SPAN_KIND]: "VECTOR_DB",
    };
    if (tableName) {
      attrs["db.collection.name"] = tableName;
    }
    return attrs;
  }

  private async _wrapMethod(
    thisArg: any,
    original: Function,
    operation: LanceDBOperation,
    args: any[],
    tableName?: string
  ): Promise<any> {
    const attributes = this.getCommonAttributes(operation, tableName);
    const params = args[0];

    // Operation-specific attributes
    if (operation === "search") {
      if (params?.limit) attributes["db.vector.query.top_k"] = params.limit;
      if (Array.isArray(params) && params.length > 0) {
        attributes["db.vector.query.dimensions"] = params.length;
      }
    }

    if (operation === "add") {
      if (Array.isArray(params)) {
        attributes["db.operation.batch_size"] = params.length;
      }
    }

    if (operation === "createTable" && typeof params === "string") {
      attributes["db.collection.name"] = params;
    }

    const span = this.fiTracer.startSpan(`lancedb ${operation}`, {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.apply(thisArg, args)
      );

      if (Array.isArray(result)) {
        span.setAttribute("db.response.returned_rows", result.length);
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

export default LanceDBInstrumentation;
