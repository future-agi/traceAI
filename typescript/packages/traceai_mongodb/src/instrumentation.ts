import { basename, dirname, join } from "node:path";
import {
  InstrumentationBase,
  InstrumentationConfig,
  InstrumentationNodeModuleDefinition,
  InstrumentationNodeModuleFile,
} from "@opentelemetry/instrumentation";
import { Attributes, context, Span, SpanKind, SpanStatusCode } from "@opentelemetry/api";
import { isTracingSuppressed } from "@opentelemetry/core";
import { FITracer, TraceConfigOptions } from "@traceai/fi-core";
import { SemanticConventions } from "@traceai/fi-semantic-conventions";
import type {
  MongoClient,
  CommandStartedEvent,
  CommandSucceededEvent,
  CommandFailedEvent,
} from "mongodb";

const VERSION = "0.1.1"; // x-release-please-version

// Only application commands: never authentication, handshakes or session payloads.
const COMMANDS = new Set([
  "aggregate", "find", "getMore", "killCursors", "insert", "update", "delete",
  "findAndModify", "count", "distinct", "createSearchIndexes", "updateSearchIndex",
  "dropSearchIndex",
]);
const owners = new Map<object, MongoDBInstrumentation>();
const poolOwners = new Map<object, MongoDBInstrumentation>();
const clientOwners = new WeakMap<MongoClient, MongoDBInstrumentation>();

export function isPatched(): boolean {
  return owners.size > 0;
}

export interface MongoDBInstrumentationConfig extends InstrumentationConfig {
  /** @deprecated Vectors are never captured. Retained for source compatibility. */
  captureQueryVectors?: boolean;
  /** @deprecated Result documents/vectors are never captured. */
  captureResultVectors?: boolean;
}

interface ClientState {
  ref: WeakRef<MongoClient>;
  monitorCommands: boolean;
  spans: Map<number, Span>;
  started: (event: CommandStartedEvent) => void;
  succeeded: (event: CommandSucceededEvent) => void;
  failed: (event: CommandFailedEvent) => void;
  closed: () => void;
}

type PoolPrototype = { checkOut: (callback: (...args: unknown[]) => void) => void };
type PoolModule = { ConnectionPool: { prototype: PoolPrototype } };

type MongoModule = {
  MongoClient: { prototype: { connect: (...args: never[]) => Promise<unknown> } };
};
// The driver's APM accessor is present in 5.x–7.x but stripped from its .d.ts.
type MonitoredClient = MongoClient & { monitorCommands: boolean };
type CommandReply = {
  cursor?: { firstBatch?: unknown; nextBatch?: unknown };
  n?: number;
  nModified?: number;
  writeErrors?: unknown[];
  writeConcernError?: unknown;
};

/** Trace executed commands, not cursor construction or consumption. */
export class MongoDBInstrumentation extends InstrumentationBase {
  private readonly traceConfig?: TraceConfigOptions;
  private readonly pools = new Set<PoolPrototype>();
  private readonly modules = new Set<MongoModule>();
  private readonly clients = new Map<MongoClient, ClientState>();
  private readonly knownClients = new Set<WeakRef<MongoClient>>();
  private readonly clientRefs = new WeakMap<MongoClient, WeakRef<MongoClient>>();

  constructor({
    instrumentationConfig,
    traceConfig,
  }: {
    instrumentationConfig?: MongoDBInstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    // Base construction must not enable hooks before our state is initialized.
    super("@traceai/mongodb", VERSION, { ...instrumentationConfig, enabled: false });
    this.traceConfig = traceConfig;
    this.setConfig({ enabled: true, ...instrumentationConfig });
    if (this.getConfig().enabled) this.enable();
  }

  protected init() {
    return new InstrumentationNodeModuleDefinition(
      "mongodb",
      [">=5.0.0 <8"],
      this.patch.bind(this),
      this.unpatch.bind(this),
      [new InstrumentationNodeModuleFile(
        "mongodb/lib/cmap/connection_pool.js",
        [">=5.0.0 <6.4"],
        (module: PoolModule) => { this.patchPool(module.ConnectionPool.prototype); return module; },
        (module?: PoolModule) => { if (module) this.unpatchPool(module.ConnectionPool.prototype); },
      )],
    );
  }

  public override enable(): void {
    super.enable();
    for (const module of this.modules) this.patch(module);
    for (const pool of this.pools) this.patchPool(pool);
    for (const ref of this.knownClients) {
      const client = ref.deref();
      if (client) this.attachClient(client, ref);
      else this.knownClients.delete(ref);
    }
  }

  public override disable(): void {
    super.disable();
    for (const module of this.modules) this.unpatch(module);
    for (const pool of this.pools) this.unpatchPool(pool);
    for (const [client, state] of this.clients) this.detachClient(client, state);
  }

  /** For modules loaded before instrumentation (including native ESM imports). */
  public manuallyInstrument(mongoModule: MongoModule): void {
    this.modules.add(mongoModule);
    if (this.isEnabled()) this.patch(mongoModule);
    // Preloaded CJS modules (also the source of Node's ESM namespace) bypass
    // require-in-the-middle. Locate only this exact driver's cached module.
    for (const [filename, cached] of Object.entries(require.cache)) {
      if (basename(filename) !== "mongo_client.js" || cached?.exports?.MongoClient !== mongoModule.MongoClient) continue;
      const directory = dirname(filename);
      const [major, minor] = (require(join(directory, "../package.json")).version as string).split(".").map(Number);
      if (major === 5 || (major === 6 && minor < 4)) {
        const pool = require(join(directory, "cmap/connection_pool.js")) as PoolModule;
        this.pools.add(pool.ConnectionPool.prototype);
        if (this.isEnabled()) this.patchPool(pool.ConnectionPool.prototype);
      }
      break;
    }
  }

  /** Attach to an existing client created with { monitorCommands: true }. */
  public instrumentClient(client: MongoClient): void {
    if (this.isEnabled()) this.attachClient(client);
  }

  private patch(moduleExports: MongoModule): MongoModule {
    const prototype = moduleExports.MongoClient?.prototype;
    if (!prototype || owners.has(prototype)) return moduleExports;
    this.modules.add(moduleExports);
    const instrumentation = this;
    this._wrap(prototype, "connect", (original) => function (this: MongoClient, ...args) {
      instrumentation.safe(() => instrumentation.attachClient(this));
      // Return the driver's own promise, preserving its identity and rejection.
      try {
        const result = original.apply(this, args);
        void result.catch(() => instrumentation.safe(() => instrumentation.forgetClient(this)));
        return result;
      } catch (error) {
        instrumentation.safe(() => instrumentation.forgetClient(this));
        throw error;
      }
    });
    owners.set(prototype, this);
    return moduleExports;
  }

  // The callback-based pool used before driver 6.4 can run a queued request in
  // another request's async context. Bind at checkout without touching commands.
  private patchPool(prototype: PoolPrototype): void {
    if (poolOwners.has(prototype)) return;
    this.pools.add(prototype);
    this._wrap(prototype, "checkOut", (original) => function (this: PoolPrototype, callback) {
      return original.call(this, context.bind(context.active(), callback));
    });
    poolOwners.set(prototype, this);
  }

  private unpatchPool(prototype: PoolPrototype): void {
    if (poolOwners.get(prototype) !== this) return;
    this._unwrap(prototype, "checkOut");
    poolOwners.delete(prototype);
  }

  private unpatch(moduleExports: MongoModule): void {
    const prototype = moduleExports.MongoClient?.prototype;
    if (owners.get(prototype) !== this) return;
    this._unwrap(prototype, "connect");
    owners.delete(prototype);
  }

  private attachClient(client: MongoClient, ref?: WeakRef<MongoClient>): void {
    if (this.clients.has(client) || clientOwners.has(client)) return;
    const monitored = client as MonitoredClient;
    const state: ClientState = {
      ref: ref ?? this.clientRefs.get(client) ?? new WeakRef(client),
      monitorCommands: monitored.monitorCommands,
      spans: new Map(),
      started: (event) => this.safe(() => this.startCommand(state, event)),
      succeeded: (event) => this.safe(() => this.endCommand(state, event)),
      failed: (event) => this.safe(() => this.endCommand(state, event)),
      closed: () => this.forgetClient(client),
    };
    // MongoClient's APM setter affects connections created after this point.
    // Already-connected clients must have opted in at construction time.
    monitored.monitorCommands = true;
    client.on("commandStarted", state.started);
    client.on("commandSucceeded", state.succeeded);
    client.on("commandFailed", state.failed);
    client.on("topologyClosed", state.closed);
    this.clients.set(client, state);
    this.knownClients.add(state.ref);
    this.clientRefs.set(client, state.ref);
    clientOwners.set(client, this);
  }

  private detachClient(client: MongoClient, state: ClientState): void {
    client.off("commandStarted", state.started);
    client.off("commandSucceeded", state.succeeded);
    client.off("commandFailed", state.failed);
    client.off("topologyClosed", state.closed);
    const monitored = client as MonitoredClient;
    if (monitored.monitorCommands) monitored.monitorCommands = state.monitorCommands;
    // Disabling/closing is not a successful database response.
    for (const span of state.spans.values()) span.end();
    state.spans.clear();
    this.clients.delete(client);
    clientOwners.delete(client);
  }

  private forgetClient(client: MongoClient): void {
    const state = this.clients.get(client);
    if (!state) return;
    this.detachClient(client, state);
    this.knownClients.delete(state.ref);
  }

  private startCommand(state: ClientState, event: CommandStartedEvent): void {
    if (!this.isEnabled() || !COMMANDS.has(event.commandName) || isTracingSuppressed(context.active())) return;
    const command = event.command;
    const collection = command[event.commandName] ?? command.collection;
    const attributes: Attributes = {
      "db.system": "mongodb",
      "db.system.name": "mongodb",
      "db.namespace": event.databaseName,
      "db.operation.name": event.commandName,
      [SemanticConventions.FI_SPAN_KIND]: "VECTOR_DB",
    };
    if (typeof collection === "string") attributes["db.collection.name"] = collection;
    else if (typeof command.collection === "string") attributes["db.collection.name"] = command.collection;
    if (event.commandName === "aggregate") this.setVectorAttributes(attributes, command.pipeline);
    if (event.commandName === "insert" && Array.isArray(command.documents)) {
      attributes["db.operation.batch_size"] = command.documents.length;
    }
    // Build from the current provider, including a provider set after enable().
    const tracer = new FITracer({
      tracer: this.tracer,
      traceConfig: { hideErrorDetails: true, ...this.traceConfig },
    });
    const span = tracer.startSpan(`mongodb ${event.commandName}`, { kind: SpanKind.CLIENT, attributes });
    state.spans.set(event.requestId, span);
  }

  private setVectorAttributes(attributes: Attributes, pipeline: unknown): void {
    if (!Array.isArray(pipeline)) return;
    for (const stage of pipeline) {
      const vector = stage?.$vectorSearch ?? stage?.$search?.knnBeta;
      if (!vector || typeof vector !== "object") continue;
      attributes["db.operation.type"] = "vector_search";
      const limit = vector.limit ?? vector.k;
      if (typeof limit === "number") attributes["db.vector.query.top_k"] = limit;
      if (typeof vector.numCandidates === "number") attributes["db.vector.query.num_candidates"] = vector.numCandidates;
      const index = vector.index ?? stage.$search?.index;
      if (typeof index === "string") attributes["db.vector.index_name"] = index;
      const queryVector = vector.queryVector ?? vector.vector;
      if (Array.isArray(queryVector)) attributes["db.vector.query.dimensions"] = queryVector.length;
      break;
    }
  }

  private endCommand(state: ClientState, event: CommandSucceededEvent | CommandFailedEvent): void {
    const span = state.spans.get(event.requestId);
    if (!span) return;
    state.spans.delete(event.requestId);
    try {
      if ("failure" in event) {
        span.recordException(event.failure);
        span.setStatus({ code: SpanStatusCode.ERROR, message: event.failure.message });
      } else {
        const reply = event.reply as CommandReply | undefined;
        const batch = reply?.cursor?.firstBatch ?? reply?.cursor?.nextBatch;
        if (Array.isArray(batch)) span.setAttribute("db.response.returned_rows", batch.length);
        if (typeof reply?.n === "number") {
          if (event.commandName === "insert") span.setAttribute("db.response.inserted_count", reply.n);
          if (event.commandName === "delete") span.setAttribute("db.response.deleted_count", reply.n);
        }
        if (typeof reply?.nModified === "number") span.setAttribute("db.response.modified_count", reply.nModified);
        // MongoDB reports write failures in an otherwise successful command reply.
        if (reply?.writeErrors?.length || reply?.writeConcernError) {
          span.setStatus({ code: SpanStatusCode.ERROR });
        } else {
          span.setStatus({ code: SpanStatusCode.OK });
        }
      }
    } finally {
      span.end();
    }
  }

  private safe(callback: () => void): void {
    try {
      callback();
    } catch {
      // Never let telemetry break the driver, or log database payloads/errors.
      this._diag.debug("MongoDB telemetry callback failed");
    }
  }
}

export default MongoDBInstrumentation;
