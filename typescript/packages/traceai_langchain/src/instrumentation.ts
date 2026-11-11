import type * as CallbackManagerModuleV02 from "@langchain/core/callbacks/manager";
import type * as ChatModelModule from "@langchain/core/language_models/chat_models";
import type * as RunnableModule from "@langchain/core/runnables";
import { randomUUID } from "crypto";
import { wrapModelStream } from "./streamLifecycle";
import {
  InstrumentationBase,
  InstrumentationConfig,
  InstrumentationModuleDefinition,
  InstrumentationNodeModuleDefinition,
  isWrapped,
} from "@opentelemetry/instrumentation";
import { VERSION } from "./version";
import { diag, Tracer, TracerProvider } from "@opentelemetry/api";
import { addTracerToHandlers } from "./instrumentationUtils";
import { FITracer, TraceConfigOptions } from "@traceai/fi-core";
import type { FrameworkAttachmentOptions } from "@traceai/framework-attachments";

const MODULE_NAME = "@langchain/core/callbacks";

const INSTRUMENTATION_NAME = "@traceai/langchain";
/**
 * Flag to check if the openai module has been patched
 * Note: This is a fallback in case the module is made immutable (e.x. Deno, webpack, etc.)
 */
let _isFIPatched = false;

/**
 * function to check if instrumentation is enabled / disabled
 */
export function isPatched() {
  return _isFIPatched;
}

/**
 * Resets the internal patched state. Intended for testing only.
 */
export function _resetPatchedStateForTesting(): void {
  _isFIPatched = false;
}

type CallbackManagerModule = typeof CallbackManagerModuleV02;
// The streaming helper is protected in LangChain's declarations, but is the
// shared terminal boundary for Lambda/Map/Assign/Pick streaming transforms.
type RunnableStreamPrototype = Record<string, (...args: unknown[]) => AsyncGenerator<unknown>>;
function runnableStreamMethods(module: typeof RunnableModule) {
  return [
    [module?.Runnable?.prototype, "_transformStreamWithConfig", 2],
    [module?.RunnableSequence?.prototype, "_streamIterator", 1],
  ] as const;
}

/**
 * @param instrumentationConfig The config for the instrumentation @see {@link InstrumentationConfig}
 * @param traceConfig The FI trace configuration. Can be used to mask or redact sensitive information on spans. @see {@link TraceConfigOptions}
 */
export class LangChainInstrumentation extends InstrumentationBase {
  private fiTracer: FITracer;
  private tracerProvider?: TracerProvider;
  private traceConfig?: TraceConfigOptions;
  private readonly media: FrameworkAttachmentOptions;
  private manualModule?: CallbackManagerModule;
  private modelModules?: Set<typeof ChatModelModule>;
  private runnableModules?: Set<typeof RunnableModule>;

  constructor({
    instrumentationConfig,
    traceConfig,
    tracerProvider,
    attachments,
    capture,
  }: {
    /**
     * The config for the instrumentation
     * @see {@link InstrumentationConfig}
     */
    attachments?: FrameworkAttachmentOptions["attachments"];
    capture?: FrameworkAttachmentOptions["capture"];
    instrumentationConfig?: InstrumentationConfig;
    /**
     * @see {@link TraceConfigOptions}
     */
    traceConfig?: TraceConfigOptions;
    /**
     * An optional custom trace provider to be used for tracing. If not provided, a tracer will be created using the global tracer provider.
     * This is useful if you want to use a non-global tracer provider.
     *
     * @see {@link TracerProvider}
     */
    tracerProvider?: TracerProvider;
  } = {}) {
    super(
      INSTRUMENTATION_NAME,
      VERSION,
      Object.assign({}, instrumentationConfig),
    );
    this.tracerProvider = tracerProvider;
    this.traceConfig = traceConfig;
    this.media = { attachments, traceConfig, capture };
    this.fiTracer = new FITracer({
      tracer:
        this.tracerProvider?.getTracer(INSTRUMENTATION_NAME, VERSION) ??
        this.tracer,
      traceConfig,
    });
  }

  /** Pass chatModels and runnables to close model and composed-chain streams on early return. */
  manuallyInstrument(
    module: CallbackManagerModule,
    chatModels?: typeof ChatModelModule,
    runnables?: typeof RunnableModule,
  ) {
    if (module == null) {
      return;
    }
    this.manualModule = module;
    diag.debug(`Manually instrumenting ${MODULE_NAME}`);
    this.patch(module);
    if (chatModels != null) this.patchChatModels(chatModels);
    if (runnables != null) this.patchRunnables(runnables);
  }

  protected init(): InstrumentationModuleDefinition[] {
    return [
      new InstrumentationNodeModuleDefinition(
        "@langchain/core/dist/callbacks/manager.cjs",
        ["^0.1.0", "^0.2.0", "^0.3.0", "^1.0.0"],
        this.patch.bind(this),
        this.unpatch.bind(this),
      ),
      new InstrumentationNodeModuleDefinition(
        "@langchain/core/dist/language_models/chat_models.cjs",
        ["^0.2.0", "^0.3.0", "^1.0.0"],
        this.patchChatModels.bind(this),
        this.unpatchChatModels.bind(this),
      ),
      new InstrumentationNodeModuleDefinition(
        "@langchain/core/dist/runnables/base.cjs",
        ["^0.2.0", "^0.3.0", "^1.0.0"],
        this.patchRunnables.bind(this),
        this.unpatchRunnables.bind(this),
      ),
    ];
  }

  enable(): void {
    super.enable();
    if (this.manualModule != null) this.patch(this.manualModule);
    this.modelModules?.forEach((module) => this.patchChatModels(module));
    this.runnableModules?.forEach((module) => this.patchRunnables(module));
  }

  disable(): void {
    super.disable();
    if (this.manualModule != null) this.unpatch(this.manualModule);
    this.modelModules?.forEach((module) => this.unpatchChatModels(module));
    this.runnableModules?.forEach((module) => this.unpatchRunnables(module));
  }

  private patchChatModels(module: typeof ChatModelModule) {
    const prototype = module?.BaseChatModel?.prototype;
    if (prototype == null || isWrapped(prototype._streamIterator)) return module;
    (this.modelModules ??= new Set()).add(module);
    this._wrap(prototype, "_streamIterator", (original) => {
      return function (
        this: typeof prototype,
        ...args: Parameters<typeof prototype._streamIterator>
      ) {
        // LangChain honors a supplied run id. Clone config because some versions
        // consume/delete it; do not mutate options owned by the application.
        const options = args[1];
        const runId = options?.runId ?? randomUUID();
        return wrapModelStream((signal) => {
          args[1] = { ...options, runId, signal };
          return original.apply(this, args);
        }, runId, options?.signal, options?.timeout);
      };
    });
    return module;
  }

  private unpatchChatModels(module?: typeof ChatModelModule) {
    const prototype = module?.BaseChatModel?.prototype;
    if (prototype != null && isWrapped(prototype._streamIterator)) {
      this._unwrap(prototype, "_streamIterator");
    }
    return module;
  }

  private patchRunnables(module: typeof RunnableModule) {
    (this.runnableModules ??= new Set()).add(module);
    for (const [prototype, method, optionsIndex] of runnableStreamMethods(module)) {
      const target = prototype as unknown as RunnableStreamPrototype | undefined;
      if (typeof target?.[method] !== "function" || isWrapped(target[method])) continue;
      this._wrap(target, method, (original) => {
        return function (this: unknown, ...args: unknown[]) {
          const options = args[optionsIndex] as { runId?: string; signal?: AbortSignal } | undefined;
          const runId = options?.runId ?? randomUUID();
          // Do not inject a signal into chain options: an older core's models
          // interpret a supplied signal as overriding their own request timeout.
          args[optionsIndex] = { ...options, runId };
          return wrapModelStream(() => original.apply(this, args), runId, options?.signal);
        };
      });
    }
    return module;
  }

  private unpatchRunnables(module?: typeof RunnableModule) {
    if (module == null) return module;
    for (const [prototype, method] of runnableStreamMethods(module)) {
      const target = prototype as unknown as RunnableStreamPrototype | undefined;
      if (target != null && isWrapped(target[method])) this._unwrap(target, method);
    }
    return module;
  }

  get tracer(): Tracer {
    if (this.tracerProvider) {
      return this.tracerProvider.getTracer(
        this.instrumentationName,
        this.instrumentationVersion,
      );
    }
    return super.tracer;
  }

  setTracerProvider(tracerProvider: TracerProvider): void {
    super.setTracerProvider(tracerProvider);
    this.tracerProvider = tracerProvider;
    this.fiTracer = new FITracer({
      tracer: this.tracer,
      traceConfig: this.traceConfig,
    });
  }

  private patch(
    module: CallbackManagerModule & {
      fiPatched?: boolean;
    },
    moduleVersion?: string,
  ) {
    diag.debug(
      `Applying patch for ${MODULE_NAME}${
        moduleVersion != null ? `@${moduleVersion}` : ""
      }`,
    );
    if (module?.fiPatched || _isFIPatched) {
      return module;
    }
    if (module?.CallbackManager == null) {
      return module;
    }
    // eslint-disable-next-line @typescript-eslint/no-this-alias
    const instrumentation = this;

    /**
     * Later 0.2.x releases added _configureSync; the 0.2.0 floor only has
     * configure. Patch exactly one entry point, since newer configure methods
     * delegate to _configureSync and wrapping both would duplicate tracing.
     */
    if ("_configureSync" in module.CallbackManager) {
      this._wrap(module.CallbackManager, "_configureSync", (original) => {
        return function (
          this: typeof CallbackManagerModuleV02,
          ...args: Parameters<
            (typeof CallbackManagerModuleV02.CallbackManager)["_configureSync"]
          >
        ) {
          const inheritableHandlers = args[0];
          const newInheritableHandlers = addTracerToHandlers(
            instrumentation.fiTracer,
            inheritableHandlers,
            instrumentation.media,
          );
          args[0] = newInheritableHandlers;

          return original.apply(this, args);
        };
      });
    } else {
      this._wrap(module.CallbackManager, "configure", (original) => {
        return function (
          this: typeof CallbackManagerModuleV02,
          ...args: Parameters<typeof CallbackManagerModuleV02.CallbackManager.configure>
        ) {
          args[0] = addTracerToHandlers(instrumentation.fiTracer, args[0], instrumentation.media);
          return original.apply(this, args);
        };
      });
    }
    _isFIPatched = true;
    try {
      // This can fail if the module is made immutable via the runtime or bundler
      module.fiPatched = true;
    } catch (e) {
      diag.debug(`Failed to set ${MODULE_NAME} patched flag on the module`, e);
    }

    return module;
  }

  private unpatch(
    module?: CallbackManagerModule & {
      fiPatched?: boolean;
    },
    moduleVersion?: string,
  ) {
    if (module == null) {
      return;
    }
    diag.debug(
      `Removing patch for ${MODULE_NAME}${
        moduleVersion != null ? `@${moduleVersion}` : ""
      }`,
    );
    if (isWrapped(module.CallbackManager.configure)) {
      this._unwrap(module.CallbackManager, "configure");
    }
    /**
     * _configureSync is only available in v0.2.0 and above
     * Thus we only want to unwrap it if it's available and has been wrapped
     */
    if (
      "_configureSync" in module.CallbackManager &&
      isWrapped(module.CallbackManager._configureSync)
    ) {
      this._unwrap(module.CallbackManager, "_configureSync");
    }
    _isFIPatched = false;
    try {
      // This can fail if the module is made immutable via the runtime or bundler
      module.fiPatched = false;
    } catch (e) {
      diag.warn(`Failed to unset ${MODULE_NAME} patched flag on the module`, e);
    }
    return module;
  }
}