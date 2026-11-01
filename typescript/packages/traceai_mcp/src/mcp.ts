import { context, diag, propagation } from "@opentelemetry/api";
import {
  InstrumentationBase,
  InstrumentationConfig,
  InstrumentationNodeModuleDefinition,
} from "@opentelemetry/instrumentation";
import { VERSION } from "./version";
import { patchedModules, wrappedMethods } from "./state";
import { FastMCPOptions, Operations, transportOperations } from "./operations";
export type { FastMCPOptions } from "./operations";

/** Structural module contract; fastmcp remains an optional application dependency. */
export interface FastMCPModule {
  FastMCPSession: {
    prototype: {
      connect: (...args: never[]) => unknown;
      readonly server: MCPServer;
    };
  };
}

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

interface Patch {
  restore(): void;
}

/** Propagation-only by default, with explicit opt-in FastMCP operation tracing. */
export class MCPInstrumentation extends InstrumentationBase<InstrumentationConfig> {
  // Lazily initialized: InstrumentationBase can apply module hooks during super().
  private patches?: Map<object, Map<string, Patch>>;
  private moduleTargets?: Map<object, Set<ModuleName>>;
  private connectedServers?: WeakMap<object, Set<MCPServer>>;
  private operations?: Set<Operations>;

  constructor({
    instrumentationConfig,
  }: { instrumentationConfig?: InstrumentationConfig } = {}) {
    super("@traceai/mcp", VERSION, instrumentationConfig ?? {});
  }

  /**
   * Opt in to npm FastMCP server-operation spans before starting any servers.
   * Pass the installed version explicitly; unsupported versions/shapes are a no-op.
   * Generic MCP instrumentation stays propagation-only. No provider is installed.
   */
  public instrumentFastMCP(
    module: FastMCPModule,
    options: FastMCPOptions,
  ): boolean {
    const version = /^(\d+)\.(\d+)\.(\d+)$/.exec(options.version);
    if (
      !version ||
      !(+version[1] === 4 || (+version[1] === 3 && +version[2] === 35))
    )
      return false;
    const target = module?.FastMCPSession?.prototype;
    if (
      !target ||
      typeof target.connect !== "function" ||
      !Object.getOwnPropertyDescriptor(target, "server")?.get
    )
      return false;
    // Snapshot configuration so later mutations cannot silently enable capture.
    const config = {
      ...options,
      captureContent: { ...options.captureContent },
    };
    const instrumentation = this;
    this.patchMethod(
      target,
      "connect",
      (original) =>
        function (
          this: FastMCPModule["FastMCPSession"]["prototype"],
          ...args: unknown[]
        ) {
          const transport = args[0] as MCPTransport;
          if (
            !transport ||
            typeof transport.start !== "function" ||
            typeof transport.send !== "function"
          )
            return Reflect.apply(original, this, args);
          if (!transportOperations.has(transport)) {
            const operations = new Operations(
              instrumentation.tracer,
              config,
              () => Reflect.get(transport, "sessionId"),
              () => {
                if (transportOperations.get(transport) === operations)
                  transportOperations.delete(transport);
                instrumentation.operations?.delete(operations);
              },
            );
            (instrumentation.operations ??= new Set()).add(operations);
            transportOperations.set(transport, operations);
          }
          instrumentation.instrumentTransport(transport);
          try {
            const result = Reflect.apply(original, this, args);
            void Promise.resolve(result).then(undefined, () =>
              instrumentation.restoreTarget(transport),
            );
            return result;
          } catch (error) {
            instrumentation.restoreTarget(transport);
            throw error;
          }
        },
    );
    return wrappedMethods.has(target.connect);
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
    this.releaseOnClose(server);
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
    for (const operations of this.operations ?? []) operations.close();
    for (const target of this.patches?.keys() ?? []) this.restoreTarget(target);
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
    const instrumentation = this;
    this.patchMethod(
      server,
      "connect",
      (original) =>
        function (this: MCPServer, ...args: unknown[]) {
          const transport = args[0] as MCPTransport;
          instrumentation.instrumentTransport(transport);
          // Per-request HTTP factories may close only the transport, not the
          // high-level server. Release explicitly instrumented instances too.
          if (instrumentation.patches?.has(this)) {
            instrumentation.connectedServers ??= new WeakMap();
            let servers = instrumentation.connectedServers.get(transport);
            if (!servers)
              instrumentation.connectedServers.set(
                transport,
                (servers = new Set()),
              );
            servers.add(this);
          }
          try {
            const result = Reflect.apply(original, this, args);
            // Keep the SDK's promise identity and rejection, but do not retain
            // a failed connection when the caller never reaches close().
            void Promise.resolve(result).then(undefined, () =>
              instrumentation.restoreTarget(transport),
            );
            return result;
          } catch (error) {
            instrumentation.restoreTarget(transport);
            throw error;
          }
        },
    );
  }

  private patchTransport(transport: MCPTransport): void {
    const instrumentation = this;
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
          const send = () => Reflect.apply(original, this, args);
          const operations = transportOperations.get(this);
          return operations ? operations.send(message, send) : send();
        },
    );
    this.patchMethod(
      transport,
      "start",
      (original) =>
        function (this: MCPTransport, ...args: unknown[]) {
          instrumentation.patchReceive(this);
          instrumentation.releaseOnClose(this);
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
          const dispatch = () => Reflect.apply(original, this, args);
          const operations = transportOperations.get(transport);
          const receive = () =>
            operations ? operations.receive(message, dispatch) : dispatch();
          if (!isRequest(message)) return receive();
          const params = isRecord(message.params) ? message.params : undefined;
          const carrier = isRecord(params?._meta) ? params._meta : {};
          const parent = propagation.extract(context.active(), carrier);
          return context.with(parent, receive);
        },
    );
    // A socket/peer may close without calling our close() method.
    this.releaseOnClose(transport, "onclose");
  }

  private releaseOnClose(target: object, method = "close"): void {
    const instrumentation = this;
    this.patchMethod(
      target,
      method,
      (original) =>
        function (this: object, ...args: unknown[]) {
          try {
            return Reflect.apply(original, this, args);
          } finally {
            instrumentation.restoreTarget(target);
          }
        },
    );
  }

  private patchMethod(
    target: object,
    key: string,
    wrap: (original: Method) => Method,
  ): void {
    const original: unknown = Reflect.get(target, key);
    if (
      typeof original !== "function" ||
      wrappedMethods.has(original as Method)
    )
      return;
    const descriptor = Object.getOwnPropertyDescriptor(target, key);
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
    const replacement = wrap(original as Method);
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
    wrappedMethods.add(guarded);
    this.patches ??= new Map();
    let methods = this.patches.get(target);
    if (!methods) this.patches.set(target, (methods = new Map()));
    // onmessage may have been replaced between connections.
    methods.get(key)?.restore();
    methods.set(key, {
      restore: () => {
        active = false;
        if (Reflect.get(target, key) !== guarded) return;
        if (delegated) Reflect.set(target, key, original);
        else if (descriptor) Object.defineProperty(target, key, descriptor);
        else Reflect.deleteProperty(target, key);
      },
    });
  }

  private markModule(target: object, name: ModuleName): void {
    if (!this.patches?.has(target)) return;
    this.moduleTargets ??= new Map();
    let names = this.moduleTargets.get(target);
    if (!names) this.moduleTargets.set(target, (names = new Set()));
    names.add(name);
    let targets = patchedModules.get(name);
    if (!targets) patchedModules.set(name, (targets = new Set()));
    targets.add(target);
  }

  private restoreTarget(target: object): void {
    transportOperations.get(target)?.close();
    transportOperations.delete(target);
    const methods = this.patches?.get(target);
    if (!methods) return;
    for (const patch of methods.values()) patch.restore();
    this.patches?.delete(target);
    for (const name of this.moduleTargets?.get(target) ?? [])
      patchedModules.get(name)?.delete(target);
    this.moduleTargets?.delete(target);
    const servers = this.connectedServers?.get(target);
    this.connectedServers?.delete(target);
    for (const server of servers ?? []) this.restoreTarget(server);
  }
}
