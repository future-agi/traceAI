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

type RedisVectorOperation =
  | "ftSearch"
  | "ftCreate"
  | "ftDropIndex"
  | "ftInfo"
  | "ftAggregate"
  | "hSet"
  | "hGet"
  | "hGetAll"
  | "del"
  | "json.set"
  | "json.get";

let _isPatched = false;

export function isPatched(): boolean {
  return _isPatched;
}

export interface RedisInstrumentationConfig extends InstrumentationConfig {
  captureQueryVectors?: boolean;
  captureResultVectors?: boolean;
}

export class RedisInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;
  private _redisConfig: RedisInstrumentationConfig;

  constructor({
    instrumentationConfig,
    traceConfig,
  }: {
    instrumentationConfig?: RedisInstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super("@traceai/redis", VERSION, Object.assign({}, instrumentationConfig));
    this._traceConfig = traceConfig;
    this._redisConfig = instrumentationConfig || {};
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
      "redis",
      [">=4.0.0"],
      this.patch.bind(this),
      this.unpatch.bind(this)
    );
  }

  public manuallyInstrument(redisModule: any): void {
    this.patch(redisModule);
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

    // The redis client is typically created via createClient
    // We'll patch the client prototype methods
    if (moduleExports.createClient) {
      const originalCreateClient = moduleExports.createClient;
      const instrumentation = this;

      moduleExports.createClient = function (...args: any[]) {
        const client = originalCreateClient.apply(this, args);
        instrumentation._patchClient(client);
        return client;
      };
    }

    _isPatched = true;
    return moduleExports;
  }

  private unpatch(moduleExports: any, moduleVersion?: string) {
    _isPatched = false;
  }

  private _patchClient(client: any): void {
    const instrumentation = this;

    // Patch FT.SEARCH (vector search)
    if (typeof client.ft?.search === "function") {
      const original = client.ft.search;
      client.ft.search = function (...args: any[]) {
        return instrumentation._wrapMethod(this, original, "ftSearch", args);
      };
    }

    // Patch FT.CREATE (create index)
    if (typeof client.ft?.create === "function") {
      const original = client.ft.create;
      client.ft.create = function (...args: any[]) {
        return instrumentation._wrapMethod(this, original, "ftCreate", args);
      };
    }

    // Patch FT.DROPINDEX
    if (typeof client.ft?.dropIndex === "function") {
      const original = client.ft.dropIndex;
      client.ft.dropIndex = function (...args: any[]) {
        return instrumentation._wrapMethod(this, original, "ftDropIndex", args);
      };
    }

    // Patch FT.INFO
    if (typeof client.ft?.info === "function") {
      const original = client.ft.info;
      client.ft.info = function (...args: any[]) {
        return instrumentation._wrapMethod(this, original, "ftInfo", args);
      };
    }

    // Patch FT.AGGREGATE
    if (typeof client.ft?.aggregate === "function") {
      const original = client.ft.aggregate;
      client.ft.aggregate = function (...args: any[]) {
        return instrumentation._wrapMethod(this, original, "ftAggregate", args);
      };
    }

    // Patch HSET (used for storing vectors)
    if (typeof client.hSet === "function") {
      const original = client.hSet;
      client.hSet = function (...args: any[]) {
        return instrumentation._wrapMethod(this, original, "hSet", args);
      };
    }

    // Patch HGET
    if (typeof client.hGet === "function") {
      const original = client.hGet;
      client.hGet = function (...args: any[]) {
        return instrumentation._wrapMethod(this, original, "hGet", args);
      };
    }

    // Patch HGETALL
    if (typeof client.hGetAll === "function") {
      const original = client.hGetAll;
      client.hGetAll = function (...args: any[]) {
        return instrumentation._wrapMethod(this, original, "hGetAll", args);
      };
    }

    // Patch DEL
    if (typeof client.del === "function") {
      const original = client.del;
      client.del = function (...args: any[]) {
        return instrumentation._wrapMethod(this, original, "del", args);
      };
    }

    // Patch JSON.SET (for JSON documents with vectors)
    if (typeof client.json?.set === "function") {
      const original = client.json.set;
      client.json.set = function (...args: any[]) {
        return instrumentation._wrapMethod(this, original, "json.set", args);
      };
    }

    // Patch JSON.GET
    if (typeof client.json?.get === "function") {
      const original = client.json.get;
      client.json.get = function (...args: any[]) {
        return instrumentation._wrapMethod(this, original, "json.get", args);
      };
    }
  }

  private getCommonAttributes(operation: RedisVectorOperation): Attributes {
    return {
      "db.system": "redis",
      "db.operation.name": operation,
      [SemanticConventions.FI_SPAN_KIND]: "VECTOR_DB",
    };
  }

  private _setOperationAttributes(attributes: Attributes, operation: RedisVectorOperation, args: any[]): void {
    switch (operation) {
      case "ftSearch":
        // FT.SEARCH index query [options]
        if (args[0]) {
          attributes["db.redis.index_name"] = args[0];
        }
        if (args[1]) {
          // Check for KNN query pattern
          const query = typeof args[1] === "string" ? args[1] : JSON.stringify(args[1]);
          if (query.includes("KNN") || query.includes("knn")) {
            attributes["db.operation.type"] = "vector_search";
          }
        }
        // Look for LIMIT in options
        if (args[2]?.LIMIT) {
          attributes["db.vector.query.top_k"] = args[2].LIMIT.size || args[2].LIMIT;
        }
        break;

      case "ftCreate":
        if (args[0]) {
          attributes["db.redis.index_name"] = args[0];
        }
        break;

      case "ftDropIndex":
      case "ftInfo":
        if (args[0]) {
          attributes["db.redis.index_name"] = args[0];
        }
        break;

      case "hSet":
        if (args[0]) {
          attributes["db.redis.key"] = args[0];
        }
        break;

      case "hGet":
      case "hGetAll":
        if (args[0]) {
          attributes["db.redis.key"] = args[0];
        }
        break;

      case "del":
        if (Array.isArray(args[0])) {
          attributes["db.operation.batch_size"] = args[0].length;
        } else if (args[0]) {
          attributes["db.redis.key"] = args[0];
        }
        break;

      case "json.set":
      case "json.get":
        if (args[0]) {
          attributes["db.redis.key"] = args[0];
        }
        break;
    }
  }

  private _setResultAttributes(span: any, operation: RedisVectorOperation, result: any): void {
    switch (operation) {
      case "ftSearch":
        if (result?.total !== undefined) {
          span.setAttribute("db.response.total_count", result.total);
        }
        if (result?.documents) {
          span.setAttribute("db.response.returned_rows", result.documents.length);
        }
        break;

      case "ftInfo":
        if (result?.numDocs !== undefined) {
          span.setAttribute("db.redis.num_docs", result.numDocs);
        }
        break;
    }
  }

  private async _wrapMethod(
    thisArg: any,
    original: Function,
    operation: RedisVectorOperation,
    args: any[]
  ): Promise<any> {
    const attributes = this.getCommonAttributes(operation);
    this._setOperationAttributes(attributes, operation, args);

    const span = this.fiTracer.startSpan(`redis ${operation}`, {
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
      span.setStatus({ code: SpanStatusCode.ERROR, message: error?.message });
      throw error;
    } finally {
      span.end();
    }
  }
}

export default RedisInstrumentation;
