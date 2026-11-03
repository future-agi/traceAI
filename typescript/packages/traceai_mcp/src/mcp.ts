import { context, diag, propagation } from "@opentelemetry/api";
import {
  InstrumentationBase,
  InstrumentationConfig,
  InstrumentationNodeModuleDefinition,
} from "@opentelemetry/instrumentation";
import { VERSION } from "./version";

// Structural boundaries shared by the monolithic and split SDKs. No runtime SDK
// dependency or private handler maps: handlers registered before and after connect
// both run under the context established at the transport's public onmessage.
type Method = (...args: never[]) => unknown;
export interface MCPTransport {
  start: Method;
  send: Method;
  close: Method;
  onmessage?: Method;
  onclose?: Method;
}
export interface MCPServer {
  connect: Method;
  close: Method;
}
type TransportModule<Name extends string> = {
  [Key in Name]: { prototype: MCPTransport };
};
export interface MCPModules {
  clientSSEModule?: TransportModule<"SSEClientTransport">;
  serverSSEModule?: TransportModule<"SSEServerTransport">;
  clientStdioModule?: TransportModule<"StdioClientTransport">;
  serverStdioModule?: TransportModule<"StdioServerTransport">;
  clientStreamableHTTPModule?: TransportModule<"StreamableHTTPClientTransport">;
  serverStreamableHTTPModule?: TransportModule<"StreamableHTTPServerTransport">;
  serverWebStandardStreamableHTTPModule?: TransportModule<"WebStandardStreamableHTTPServerTransport">;
  /** @modelcontextprotocol/server, or @modelcontextprotocol/sdk/server/index.js. */
  serverModule?: {
    Server: { prototype: MCPServer };
    WebStandardStreamableHTTPServerTransport?: { prototype: MCPTransport };
  };
}

const transports = [
  [
    "clientSSEModule",
    "@modelcontextprotocol/sdk/client/sse",
    "SSEClientTransport",
    ">=1.0.0 <2",
  ],
  [
    "serverSSEModule",
    "@modelcontextprotocol/sdk/server/sse",
    "SSEServerTransport",
    ">=1.0.0 <2",
  ],
  [
    "clientStdioModule",
    "@modelcontextprotocol/sdk/client/stdio",
    "StdioClientTransport",
    ">=1.0.0 <2",
  ],
  [
    "serverStdioModule",
    "@modelcontextprotocol/sdk/server/stdio",
    "StdioServerTransport",
    ">=1.0.0 <2",
  ],
  [
    "clientStreamableHTTPModule",
    "@modelcontextprotocol/sdk/client/streamableHttp",
    "StreamableHTTPClientTransport",
    ">=1.10.0 <2",
  ],
  [
    "serverStreamableHTTPModule",
    "@modelcontextprotocol/sdk/server/streamableHttp",
    "StreamableHTTPServerTransport",
    ">=1.10.0 <2",
  ],
  [
    "serverWebStandardStreamableHTTPModule",
    "@modelcontextprotocol/sdk/server/webStandardStreamableHttp",
    "WebStandardStreamableHTTPServerTransport",
    ">=1.10.2 <2",
  ],
] as const;
type ModuleName =
  | (typeof transports)[number][1]
  | "@modelcontextprotocol/server"
  | "@modelcontextprotocol/sdk/server/index";
const patchedModules = new Map<ModuleName, Map<object, Set<MCPInstrumentation>>>();
interface SharedPatch {
  target: object;
  owners: Set<MCPInstrumentation>;
  restore(): void;
}
const wrappedMethods = new WeakMap<Method, SharedPatch>();

/** Whether at least one copy of the named module is currently instrumented. */
export function isPatched(moduleName: ModuleName): boolean {
  return !!patchedModules.get(moduleName)?.size;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isRequest(value: unknown): value is Record<string, unknown> {
  return isRecord(value) && typeof value.method === "string" && "id" in value;
}

/** Propagates request context; does not create execution spans or capture content. */
export class MCPInstrumentation extends InstrumentationBase<InstrumentationConfig> {
  // Lazily initialized: InstrumentationBase can apply module hooks during super().
  // Weak keys let framework-owned per-request servers be collected even when
  // the framework closes only their transports, while disable can still visit
  // the live targets through weak references.
  private patches?: WeakMap<object, Map<string, SharedPatch>>;
  private patchRefs?: Set<WeakRef<object>>;
  private targetRefs?: WeakMap<object, WeakRef<object>>;
  private moduleTargets?: Map<object, Set<ModuleName>>;

  constructor({
    instrumentationConfig,
  }: { instrumentationConfig?: InstrumentationConfig } = {}) {
    super("@traceai/mcp", VERSION, instrumentationConfig ?? {});
  }

  /** Explicit instrumentation also works with ESM, bundlers and preloaded modules. */
  public manuallyInstrument(modules: MCPModules): void {
    for (const [key, name, exportedClass] of transports) {
      const module = modules[key];
      if (module) this.patchTransportModule(module, name, exportedClass);
    }
    if (modules.serverModule) {
      this.patchServerModule(
        modules.serverModule,
        "@modelcontextprotocol/server",
      );
      this.patchTransportModule(
        modules.serverModule,
        "@modelcontextprotocol/server",
        "WebStandardStreamableHTTPServerTransport",
      );
    }
  }

  /**
   * Instrument a public Server or McpServer before connect(). This also supports
   * framework-owned servers (for example an mcp-handler initialization callback).
   * No handler replacement or SDK-private state is needed.
   */
  public instrumentServer(server: MCPServer): void {
    this.patchServer(server);
    // A Server may connect again after close(); retain its connect patch until
    // disable(), while each connected transport releases its own patches.
    // A low-level Server exposes its connected transport via a public getter.
    const transport: unknown = Reflect.get(server, "transport");
    if (
      isRecord(transport) &&
      typeof transport.start === "function" &&
      typeof transport.send === "function"
    ) {
      this.instrumentTransport(transport as unknown as MCPTransport);
    }
  }

  /** Instrument a transport before connect(), or one with onmessage already set. */
  public instrumentTransport(transport: MCPTransport): void {
    this.patchTransport(transport);
    this.patchReceive(transport);
    this.releaseOnClose(transport);
  }

  /** Restores manual patches as well as auto-instrumentation hooks. */
  public override disable(): void {
    super.disable();
    for (const ref of this.patchRefs ?? []) {
      const target = ref.deref();
      if (target) this.restoreTarget(target);
      else this.patchRefs?.delete(ref);
    }
  }

  protected override init() {
    return [
      ...transports.map(
        ([, name, exportedClass, version]) =>
          new InstrumentationNodeModuleDefinition(
            `${name}.js`,
            [version],
            (module) => this.patchTransportModule(module, name, exportedClass),
            (module) => {
              this.restoreTarget(module[exportedClass].prototype);
              return module;
            },
          ),
      ),
      new InstrumentationNodeModuleDefinition(
        "@modelcontextprotocol/sdk/server/index.js",
        [">=1.10.2 <2"],
        (module) =>
          this.patchServerModule(
            module,
            "@modelcontextprotocol/sdk/server/index",
          ),
        (module) => {
          this.restoreTarget(module.Server.prototype);
          return module;
        },
      ),
      new InstrumentationNodeModuleDefinition(
        "@modelcontextprotocol/server",
        [">=2.4.0 <3"],
        (module) => {
          this.patchServerModule(module, "@modelcontextprotocol/server");
          this.patchTransportModule(
            module,
            "@modelcontextprotocol/server",
            "WebStandardStreamableHTTPServerTransport",
          );
          return module;
        },
        (module) => {
          this.restoreTarget(module.Server.prototype);
          this.restoreTarget(
            module.WebStandardStreamableHTTPServerTransport.prototype,
          );
          return module;
        },
      ),
    ];
  }

  private patchTransportModule<T extends object>(
    module: T,
    name: ModuleName,
    exportedClass: string,
  ): T {
    const constructor = Reflect.get(module as object, exportedClass) as
      | { prototype: MCPTransport }
      | undefined;
    const target = constructor?.prototype;
    if (target) {
      this.patchTransport(target);
      this.markModule(target, name);
    }
    return module;
  }

  private patchServerModule<T extends { Server: { prototype: MCPServer } }>(
    module: T,
    name: ModuleName,
  ): T {
    this.patchServer(module.Server.prototype);
    this.markModule(module.Server.prototype, name);
    return module;
  }

  private patchServer(server: MCPServer): void {
    this.patchMethod(
      server,
      "connect",
      (original, owners) =>
        function (this: MCPServer, ...args: unknown[]) {
          const transport = args[0] as MCPTransport;
          for (const owner of owners) owner.instrumentTransport(transport);
          try {
            const result = Reflect.apply(original, this, args);
            // Keep the SDK's promise identity and rejection, but do not retain
            // a failed connection when the caller never reaches close().
            void Promise.resolve(result).then(undefined, () => {
              for (const owner of [...owners]) owner.restoreTarget(transport);
            });
            return result;
          } catch (error) {
            for (const owner of [...owners]) owner.restoreTarget(transport);
            throw error;
          }
        },
    );
  }

  private patchTransport(transport: MCPTransport): void {
    this.patchMethod(
      transport,
      "send",
      (original) =>
        function (this: MCPTransport, ...args: unknown[]) {
          const message = args[0];
          // Requests only. Notifications (including cancellation/progress) and
          // responses are forwarded unchanged, preserving the existing contract.
          if (isRequest(message)) {
            // Instrumentation must not repair invalid protocol inputs that the
            // SDK would otherwise reject.
            if (message.params !== undefined && !isRecord(message.params))
              return Reflect.apply(original, this, args);
            const params = isRecord(message.params) ? message.params : {};
            if (params._meta !== undefined && !isRecord(params._meta))
              return Reflect.apply(original, this, args);
            const meta = isRecord(params._meta) ? params._meta : {};
            const carrier = { ...meta };
            propagation.inject(context.active(), carrier);
            // Do not mutate reusable/frozen requests or user-owned metadata.
            args[0] = { ...message, params: { ...params, _meta: carrier } };
          }
          return Reflect.apply(original, this, args);
        },
    );
    this.patchMethod(
      transport,
      "start",
      (original, owners) =>
        function (this: MCPTransport, ...args: unknown[]) {
          for (const owner of owners) {
            owner.patchReceive(this);
            owner.releaseOnClose(this);
          }
          return Reflect.apply(original, this, args);
        },
    );
  }

  private patchReceive(transport: MCPTransport): void {
    this.patchMethod(
      transport,
      "onmessage",
      (original) =>
        function (this: MCPTransport, ...args: unknown[]) {
          const message = args[0];
          if (!isRequest(message)) return Reflect.apply(original, this, args);
          const params = isRecord(message.params) ? message.params : undefined;
          const carrier = isRecord(params?._meta) ? params._meta : {};
          const parent = propagation.extract(context.active(), carrier);
          return context.with(parent, () =>
            Reflect.apply(original, this, args),
          );
        },
    );
    // A socket/peer may close without calling our close() method.
    this.releaseOnClose(transport, "onclose");
  }

  private releaseOnClose(target: object, method = "close"): void {
    this.patchMethod(
      target,
      method,
      (original, owners) =>
        function (this: object, ...args: unknown[]) {
          try {
            return Reflect.apply(original, this, args);
          } finally {
            // A prototype patch belongs to the module, not one connection.
            for (const owner of [...owners]) owner.restoreTarget(this);
          }
        },
    );
  }

  private patchMethod(
    target: object,
    key: string,
    wrap: (original: Method, owners: Set<MCPInstrumentation>) => Method,
  ): void {
    const original: unknown = Reflect.get(target, key);
    if (typeof original !== "function") return;
    const descriptor = Object.getOwnPropertyDescriptor(target, key);
    const existing = wrappedMethods.get(original as Method);
    // An inherited wrapper is shared at its prototype rather than wrapped
    // again on this instance (which would inject twice).
    if (existing?.owners.size) {
      this.trackPatch(existing.target, key, existing);
      return;
    }
    let declaredDescriptor = descriptor;
    for (
      let owner = Object.getPrototypeOf(target);
      !declaredDescriptor && owner;
      owner = Object.getPrototypeOf(owner)
    ) {
      declaredDescriptor = Object.getOwnPropertyDescriptor(owner, key);
    }
    // Node's streamable HTTP adapter forwards callbacks through accessors to
    // its web-standard transport. Shadowing the accessor would bypass dispatch.
    const delegated =
      !!declaredDescriptor &&
      ("get" in declaredDescriptor || "set" in declaredDescriptor);
    const owners = new Set<MCPInstrumentation>();
    const replacement = wrap(original as Method, owners);
    let active = true;
    // If another instrumentor wraps us later, disable still makes its retained
    // wrapper inert; restoration never overwrites a customer's replacement.
    const guarded: Method = function (this: object, ...args: unknown[]) {
      return Reflect.apply(active ? replacement : original, this, args);
    };
    try {
      if (delegated) {
        if (!Reflect.set(target, key, guarded)) return;
      } else {
        Object.defineProperty(target, key, {
          configurable: true,
          writable: true,
          enumerable: descriptor?.enumerable ?? false,
          value: guarded,
        });
      }
    } catch {
      diag.debug(`MCP instrumentation could not wrap ${key}`);
      return;
    }
    const shared: SharedPatch = {
      target,
      owners,
      restore: () => {
        active = false;
        if (Reflect.get(target, key) !== guarded) return;
        if (delegated) Reflect.set(target, key, original);
        else if (descriptor) Object.defineProperty(target, key, descriptor);
        else Reflect.deleteProperty(target, key);
      },
    };
    wrappedMethods.set(guarded, shared);
    this.trackPatch(target, key, shared);
  }

  private trackPatch(target: object, key: string, shared: SharedPatch): void {
    this.patches ??= new WeakMap();
    let methods = this.patches.get(target);
    if (!methods) {
      this.patches.set(target, (methods = new Map()));
      const ref = new WeakRef(target);
      this.patchRefs ??= new Set();
      this.targetRefs ??= new WeakMap();
      this.patchRefs.add(ref);
      this.targetRefs.set(target, ref);
    }
    if (methods.get(key) === shared) return;
    // onmessage can be replaced between connections; release our old wrapper.
    const previous = methods.get(key);
    if (previous) {
      previous.owners.delete(this);
      if (!previous.owners.size) previous.restore();
    }
    shared.owners.add(this);
    methods.set(key, shared);
  }

  private markModule(target: object, name: ModuleName): void {
    if (!this.patches?.has(target)) return;
    this.moduleTargets ??= new Map();
    let names = this.moduleTargets.get(target);
    if (!names) this.moduleTargets.set(target, (names = new Set()));
    names.add(name);
    let targets = patchedModules.get(name);
    if (!targets) patchedModules.set(name, (targets = new Map()));
    let owners = targets.get(target);
    if (!owners) targets.set(target, (owners = new Set()));
    owners.add(this);
  }

  private restoreTarget(target: object): void {
    const methods = this.patches?.get(target);
    if (!methods) return;
    for (const patch of methods.values()) {
      patch.owners.delete(this);
      if (!patch.owners.size) patch.restore();
    }
    this.patches?.delete(target);
    const ref = this.targetRefs?.get(target);
    if (ref) this.patchRefs?.delete(ref);
    this.targetRefs?.delete(target);
    for (const name of this.moduleTargets?.get(target) ?? []) {
      const targets = patchedModules.get(name);
      const owners = targets?.get(target);
      owners?.delete(this);
      if (!owners?.size) targets?.delete(target);
    }
    this.moduleTargets?.delete(target);
  }
}
