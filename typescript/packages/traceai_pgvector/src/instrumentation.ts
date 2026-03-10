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

type PgVectorOperation = "query" | "insert" | "update" | "delete" | "vectorSearch";

let _isPatched = false;

export function isPatched(): boolean {
  return _isPatched;
}

export interface PgVectorInstrumentationConfig extends InstrumentationConfig {
  captureQueryVectors?: boolean;
  captureResultVectors?: boolean;
  captureQueries?: boolean;
}

export class PgVectorInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;
  private _pgConfig: PgVectorInstrumentationConfig;

  constructor({
    instrumentationConfig,
    traceConfig,
  }: {
    instrumentationConfig?: PgVectorInstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super("@traceai/pgvector", VERSION, Object.assign({}, instrumentationConfig));
    this._traceConfig = traceConfig;
    this._pgConfig = instrumentationConfig || {};
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
      "pg",
      [">=8.0.0"],
      this.patch.bind(this),
      this.unpatch.bind(this)
    );
  }

  public manuallyInstrument(pgModule: any): void {
    this.patch(pgModule);
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

    // Patch Client
    if (moduleExports.Client?.prototype?.query) {
      this._patchQuery(moduleExports.Client.prototype);
    }

    // Patch Pool
    if (moduleExports.Pool?.prototype?.query) {
      this._patchQuery(moduleExports.Pool.prototype);
    }

    _isPatched = true;
    return moduleExports;
  }

  private unpatch(moduleExports: any, moduleVersion?: string) {
    _isPatched = false;
  }

  private _patchQuery(prototype: any): void {
    const instrumentation = this;
    const originalQuery = prototype.query;

    prototype.query = function (this: any, ...args: any[]) {
      return instrumentation._wrapQuery(this, originalQuery, args);
    };
  }

  private _isVectorSearchQuery(queryText: string): boolean {
    if (!queryText) return false;
    const lowerQuery = queryText.toLowerCase();
    return (
      lowerQuery.includes("<->") ||  // L2 distance
      lowerQuery.includes("<#>") ||  // Inner product
      lowerQuery.includes("<=>") ||  // Cosine distance
      lowerQuery.includes("vector(") ||
      lowerQuery.includes("::vector")
    );
  }

  private _detectOperation(queryText: string): PgVectorOperation {
    if (!queryText) return "query";
    const lowerQuery = queryText.toLowerCase().trim();

    if (lowerQuery.startsWith("select")) return this._isVectorSearchQuery(queryText) ? "vectorSearch" : "query";
    if (lowerQuery.startsWith("insert")) return "insert";
    if (lowerQuery.startsWith("update")) return "update";
    if (lowerQuery.startsWith("delete")) return "delete";
    return "query";
  }

  private _extractTableName(queryText: string): string | null {
    if (!queryText) return null;

    // Match FROM table_name or INTO table_name
    const fromMatch = queryText.match(/(?:from|into|update)\s+["']?(\w+)["']?/i);
    return fromMatch ? fromMatch[1] : null;
  }

  private _truncateQuery(query: string, maxLength: number = 1000): string {
    if (query.length <= maxLength) return query;
    return query.substring(0, maxLength) + "...";
  }

  private getCommonAttributes(operation: PgVectorOperation, isVectorSearch: boolean): Attributes {
    const attrs: Attributes = {
      "db.system": "postgresql",
      "db.operation.name": operation,
      [SemanticConventions.FI_SPAN_KIND]: "VECTOR_DB",
    };
    if (isVectorSearch) {
      attrs["db.operation.type"] = "vector_search";
    }
    return attrs;
  }

  private _setVectorSearchAttributes(attributes: Attributes, queryText: string, args: any[]): void {
    // Try to extract LIMIT for top_k
    const limitMatch = queryText.match(/limit\s+(\d+)/i);
    if (limitMatch) {
      attributes["db.vector.query.top_k"] = parseInt(limitMatch[1], 10);
    }

    // Detect distance metric
    if (queryText.includes("<->")) {
      attributes["db.vector.distance_metric"] = "l2";
    } else if (queryText.includes("<#>")) {
      attributes["db.vector.distance_metric"] = "inner_product";
    } else if (queryText.includes("<=>")) {
      attributes["db.vector.distance_metric"] = "cosine";
    }

    // Try to detect vector dimensions from parameters
    const params = args[1];
    if (Array.isArray(params)) {
      for (const param of params) {
        if (Array.isArray(param) && param.every((v) => typeof v === "number")) {
          attributes["db.vector.query.dimensions"] = param.length;
          break;
        }
      }
    }
  }

  private async _wrapQuery(
    thisArg: any,
    original: Function,
    args: any[]
  ): Promise<any> {
    const queryText = typeof args[0] === "string" ? args[0] : args[0]?.text;
    const isVectorSearch = this._isVectorSearchQuery(queryText);
    const operation = this._detectOperation(queryText);
    const attributes = this.getCommonAttributes(operation, isVectorSearch);

    if (isVectorSearch) {
      this._setVectorSearchAttributes(attributes, queryText, args);
    }

    if (this._pgConfig.captureQueries && queryText) {
      attributes["db.statement"] = this._truncateQuery(queryText);
    }

    // Extract table name if possible
    const tableName = this._extractTableName(queryText);
    if (tableName) {
      attributes["db.collection.name"] = tableName;
    }

    const spanName = isVectorSearch ? "pgvector vectorSearch" : `pgvector ${operation}`;
    const span = this.fiTracer.startSpan(spanName, {
      kind: SpanKind.CLIENT,
      attributes,
    });

    try {
      const result = await context.with(
        trace.setSpan(context.active(), span),
        () => original.apply(thisArg, args)
      );

      if (result?.rows) {
        span.setAttribute("db.response.returned_rows", result.rows.length);
      }
      if (result?.rowCount !== undefined) {
        span.setAttribute("db.response.affected_rows", result.rowCount);
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

export default PgVectorInstrumentation;
