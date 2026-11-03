import { InstrumentationBase, InstrumentationNodeModuleDefinition } from '@opentelemetry/instrumentation';
import { context, trace, Context, Span, SpanKind, SpanStatusCode } from '@opentelemetry/api';
import { isTracingSuppressed, suppressTracing } from '@opentelemetry/core';
import { FITracer, generateTraceConfig, TraceConfigOptions, safelyJSONStringify } from '@traceai/fi-core';
import { SemanticConventions as S, FISpanKind, MimeType } from '@traceai/fi-semantic-conventions';
import { VERSION } from './version';

export interface PortkeyInstrumentationConfig {
  instrumentationConfig?: Record<string, unknown>;
  traceConfig?: TraceConfigOptions;
}

type Operation = 'chat' | 'text_completion' | 'embeddings' | 'responses';
type Patch = { target: any; key: string; original: any; wrapped: any };
const owners = new WeakMap<Function, PortkeyInstrumentation>();
const active = new Set<PortkeyInstrumentation>();
const json = (value: unknown): string => safelyJSONStringify(value) ?? '';

/** Instruments actual SDK resources, including resources created after registration. */
export class PortkeyInstrumentation extends InstrumentationBase {
  // Lazily initialized: InstrumentationBase calls enable() during super().
  private patches?: Patch[];
  private traceConfig?: TraceConfigOptions;

  constructor(config: PortkeyInstrumentationConfig = {}) {
    super('@traceai/fi-instrumentation-portkey', VERSION, config.instrumentationConfig || {});
    this.traceConfig = config.traceConfig;
  }

  protected init() {
    return new InstrumentationNodeModuleDefinition(
      'portkey-ai', ['>=1.0.0 <4'],
      (sdk: any) => this.patch(sdk),
      () => this.restore(),
    );
  }

  override enable(): void {
    super.enable();
    for (const patch of this.patches ?? []) {
      if (patch.target[patch.key] === patch.original) patch.target[patch.key] = patch.wrapped;
    }
    if (this.patches?.length) active.add(this);
  }

  override disable(): void {
    super.disable();
    this.restore();
  }

  /** Pass the SDK module before constructing clients, or an already constructed client. */
  manuallyInstrument(sdkOrClient: any): void {
    if (sdkOrClient != null && this.isEnabled()) this.patch(sdkOrClient);
  }

  private replace(target: any, key: string, wrapped: any): void {
    const descriptor = Object.getOwnPropertyDescriptor(target, key);
    if (!descriptor?.writable || owners.has(target[key])) return;
    const original = target[key];
    const previous = this.patches?.find(patch => patch.target === target && patch.key === key && patch.original === original);
    if (previous) {
      target[key] = previous.wrapped;
      active.add(this);
      return;
    }
    target[key] = wrapped;
    owners.set(wrapped, this);
    (this.patches ??= []).push({ target, key, original, wrapped });
    active.add(this);
  }

  private restore(): void {
    for (const patch of this.patches ?? []) {
      if (patch.target[patch.key] === patch.wrapped) patch.target[patch.key] = patch.original;
    }
    active.delete(this);
  }

  private patch(sdk: any): any {
    if (sdk.chat?.completions || sdk.embeddings) {
      this.patchResources(sdk);
      return sdk;
    }
    // Native ESM namespaces are immutable. Their default export is the CJS module.
    if (sdk.default?.Portkey && sdk.default !== sdk) this.patch(sdk.default);
    const Constructor = sdk.Portkey;
    if (typeof Constructor !== 'function' || owners.has(Constructor)) return sdk;
    const instrumentation = this;
    const wrapped = new Proxy(Constructor, {
      construct(target, args, newTarget) {
        const client = Reflect.construct(target, args, newTarget);
        if (instrumentation.isEnabled()) instrumentation.patchResources(client);
        return client;
      },
    });
    this.replace(sdk, 'Portkey', wrapped);
    if (sdk.default === Constructor) this.replace(sdk, 'default', wrapped);
    return sdk;
  }

  private patchResources(client: any): void {
    const resources: [any, Operation][] = [
      [client.chat?.completions, 'chat'], [client.completions, 'text_completion'],
      [client.embeddings, 'embeddings'], [client.responses, 'responses'],
    ];
    for (const [resource, operation] of resources) {
      if (typeof resource?.create !== 'function' || owners.has(resource.create)) continue;
      // Resource methods live on resource prototypes, not Portkey.prototype. Keeping
      // prototypes here avoids retaining every client (and its credentials) in memory.
      const target = Object.hasOwn(resource, 'create') ? resource : Object.getPrototypeOf(resource);
      const original = resource.create;
      const instrumentation = this;
      this.replace(target, 'create', function (this: any, ...args: any[]) {
        if (!instrumentation.isEnabled() || isTracingSuppressed(context.active())) {
          return original.apply(this, args);
        }
        return instrumentation.traceCall(original, this, args, operation);
      });
    }
  }

  private traceCall(original: Function, instance: any, args: any[], operation: Operation): any {
    const params = args[0] ?? {};
    const config = generateTraceConfig(this.traceConfig);
    // Partial content-hiding must not be bypassed by duplicate raw/JSON attributes
    // (including tool arguments, which are not just message.content).
    const input = !(config.hideInputs || config.hideInputMessages || config.hideInputText || config.hideInputImages);
    const output = !(config.hideOutputs || config.hideOutputMessages || config.hideOutputText);
    const span = new FITracer({ tracer: this.tracer, traceConfig: this.traceConfig }).startSpan(
      `Portkey ${operation === 'chat' ? 'Chat Completions' : operation === 'text_completion' ? 'Completions' : operation === 'embeddings' ? 'Embeddings' : 'Responses'}`,
      { kind: SpanKind.CLIENT, attributes: {
        [S.FI_SPAN_KIND]: operation === 'embeddings' ? FISpanKind.EMBEDDING : FISpanKind.LLM,
        [S.LLM_PROVIDER]: 'portkey',
        [S.LLM_MODEL_NAME]: params.model ?? 'unknown',
        [S.GEN_AI_OPERATION_NAME]: operation === 'responses' ? 'generate_content' : operation,
        ...(operation === 'embeddings' ? { [S.EMBEDDING_MODEL_NAME]: params.model ?? 'unknown' } : {}),
        ...(input ? { [S.INPUT_VALUE]: json(params), [S.INPUT_MIME_TYPE]: MimeType.JSON,
          [S.RAW_INPUT]: json(params),
          ...(params.messages ? { [S.LLM_INPUT_MESSAGES]: json(params.messages) } : operation === 'responses' && params.input != null
            ? { [S.LLM_INPUT_MESSAGES]: json(typeof params.input === 'string' ? [{ role: 'user', content: params.input }] : responseMessages(params.input)) } : {}),
        } : {}),
      } },
    );
    // Portkey Responses delegates to OpenAI internally. Suppress a second provider
    // span, while preserving the Portkey parent across request and stream work.
    const exec = suppressTracing(trace.setSpan(context.active(), span));
    const finish = (result: any) => {
      if (result && typeof result[Symbol.asyncIterator] === 'function') {
        return this.wrapStream(result, span, exec, operation, output);
      }
      this.response(span, result, operation, output);
      if (result?.status === 'failed') span.setStatus({ code: SpanStatusCode.ERROR });
      else span.setStatus({ code: SpanStatusCode.OK });
      span.end();
      return result;
    };
    const fail = (error: unknown): never => {
      recordError(span, error);
      span.end();
      throw error;
    };
    try {
      const promise = context.with(exec, () => original.apply(instance, args));
      // Keep APIPromise identity/prototype and SDK-specific methods. OpenAI's
      // asResponse() must not eagerly parse/consume its response body.
      return observePromise(promise, finish, fail);
    } catch (error) {
      return fail(error);
    }
  }

  private response(span: Span, result: any, operation: Operation, output: boolean): void {
    if (!result) return;
    if (result.model) span.setAttributes({ [S.LLM_MODEL_NAME]: result.model, [S.GEN_AI_RESPONSE_MODEL]: result.model });
    if (result.id) span.setAttribute(S.GEN_AI_RESPONSE_ID, result.id);
    const usage = result.usage;
    if (usage) {
      for (const [key, value] of [
        [S.LLM_TOKEN_COUNT_PROMPT, usage.prompt_tokens ?? usage.input_tokens],
        [S.LLM_TOKEN_COUNT_COMPLETION, usage.completion_tokens ?? usage.output_tokens],
        [S.LLM_TOKEN_COUNT_TOTAL, usage.total_tokens],
      ] as const) if (typeof value === 'number') span.setAttribute(key, value);
    }
    if (operation === 'embeddings') {
      if (Array.isArray(result.data)) span.setAttribute('embedding.vector_count', result.data.length);
      return; // Do not serialize vectors or response headers into raw output.
    }
    if (Array.isArray(result.choices)) {
      const reasons = result.choices.map((choice: any) => choice?.finish_reason).filter((reason: any) => typeof reason === 'string');
      if (reasons.length) span.setAttribute(S.GEN_AI_RESPONSE_FINISH_REASONS, json(reasons));
    }
    if (!output) return;
    const messages = Array.isArray(result.choices)
      ? result.choices.map((choice: any) => choice?.message ?? { role: 'assistant', content: choice?.text ?? '' })
      : (Array.isArray(result.output) ? responseMessages(result.output) : undefined);
    if (messages) {
      span.setAttribute(S.LLM_OUTPUT_MESSAGES, json(messages));
      span.setAttribute(S.OUTPUT_VALUE, typeof messages[0]?.content === 'string' ? messages[0].content : json(messages));
    }
    span.setAttribute(S.RAW_OUTPUT, json(result));
  }

  private wrapStream(stream: any, span: Span, exec: Context, operation: Operation, output: boolean): any {
    const instrumentation = this;
    let ended = false;
    const choices = new Map<number, any>();
    const responseItems = new Map<number, any>();
    let responseCompleted = false;
    let remaining = 65536;
    const append = (previous: string = '', part: unknown = ''): string => {
      if (!output || typeof part !== 'string') return previous;
      const piece = part.slice(0, remaining);
      remaining -= piece.length;
      if (piece.length < part.length) span.setAttribute('portkey.output_truncated', true);
      return previous + piece;
    };
    const observe = (chunk: any) => {
      instrumentation.response(span, chunk.response ?? { id: chunk.id, model: chunk.model, usage: chunk.usage }, operation, false);
      if (chunk.type === 'response.failed' || chunk.type === 'error') span.setStatus({ code: SpanStatusCode.ERROR });
      if (chunk.type === 'response.completed') {
        responseCompleted = true;
        // Completed Responses events contain the canonical output including tools.
        if (output) instrumentation.response(span, chunk.response, operation, true);
      }
      if (output && operation === 'responses' && Number.isInteger(chunk.output_index) && chunk.output_index >= 0 && chunk.output_index < 128) {
        const index = chunk.output_index;
        if (chunk.type === 'response.output_item.added' && chunk.item?.type === 'function_call') {
          responseItems.set(index, { type: 'function_call', call_id: append('', chunk.item.call_id), name: append('', chunk.item.name), arguments: '' });
        } else if (chunk.type === 'response.function_call_arguments.delta') {
          const item = responseItems.get(index);
          if (item) item.arguments = append(item.arguments, chunk.delta);
        } else if (chunk.type === 'response.output_text.delta') {
          const item = responseItems.get(index) ?? { type: 'message', role: 'assistant', content: '' };
          item.content = append(item.content, chunk.delta);
          responseItems.set(index, item);
        }
      }
      for (const choice of chunk.choices ?? []) {
        const index = choice.index ?? 0;
        if (!choices.has(index) && choices.size >= 16) continue;
        const state = choices.get(index) ?? { index, message: { role: 'assistant', content: '' } };
        state.finish_reason = choice.finish_reason ?? state.finish_reason;
        const delta = choice.delta ?? {};
        state.message.content = append(state.message.content, delta.content ?? choice.text);
        if (output && delta.tool_calls) {
          state.message.tool_calls ??= [];
          for (const tool of delta.tool_calls) {
            const i = tool.index ?? 0;
            if (!Number.isInteger(i) || i < 0 || i >= 128) continue;
            const saved = state.message.tool_calls[i] ?? { type: 'function', function: { name: '', arguments: '' } };
            if (tool.id) saved.id = append('', tool.id);
            saved.function.name = append(saved.function.name, tool.function?.name);
            saved.function.arguments = append(saved.function.arguments, tool.function?.arguments);
            state.message.tool_calls[i] = saved;
          }
        }
        choices.set(index, state);
      }
    };
    const end = () => {
      if (ended) return;
      ended = true;
      if (choices.size) instrumentation.response(span, { choices: [...choices.values()].sort((a, b) => a.index - b.index) }, operation, output);
      if (responseItems.size && !responseCompleted) instrumentation.response(span, { output: [...responseItems.entries()].sort(([a], [b]) => a - b).map(([, item]) => item) }, operation, output);
      span.end();
    };
    return new Proxy(stream, {
      get(target, key, receiver) {
        if (key === Symbol.asyncIterator) return () => {
          const iterator = context.with(exec, () => target[Symbol.asyncIterator]());
          return {
            async next(...args: [] | [any]) {
              try {
                const item = await context.with(exec, () => iterator.next(...args));
                if (item.done) end(); else observe(item.value);
                return item;
              } catch (error) { recordError(span, error); end(); throw error; }
            },
            async return(value?: any) {
              try { return await context.with(exec, () => iterator.return?.(value) ?? { done: true, value }); }
              catch (error) { recordError(span, error); throw error; }
              finally { end(); }
            },
            async throw(error?: any) {
              try {
                recordError(span, error);
                if (iterator.throw) return await context.with(exec, () => iterator.throw(error));
                throw error;
              } finally { end(); }
            },
            [Symbol.asyncIterator]() { return this; },
          };
        };
        const value = Reflect.get(target, key, target);
        // SDK tee()/toReadableStream() call this[Symbol.asyncIterator](). Keep
        // them on the observed stream rather than bypassing its finalizer.
        return typeof value === 'function' ? value.bind(receiver) : value;
      },
    });
  }
}

function responseMessages(items: any[]): Record<string, unknown>[] {
  if (!Array.isArray(items)) return [];
  return items.flatMap<Record<string, unknown>>(item => {
    if (!item || typeof item !== 'object') return [];
    if (item.type === 'function_call') return [{ role: 'assistant', tool_calls: [{
      id: item.call_id, type: 'function', function: { name: item.name, arguments: item.arguments },
    }] }];
    if (item.type === 'function_call_output') return [{ role: 'tool', tool_call_id: item.call_id, content: item.output }];
    if (item.role) return [{ role: item.role, content: Array.isArray(item.content)
      ? item.content.map((part: any) => part.type === 'output_text' || part.type === 'input_text' ? { type: 'text', text: part.text } : part)
      : item.content }];
    return [];
  });
}

function recordError(span: Span, error: unknown): void {
  span.recordException(error instanceof Error ? error : new Error(String(error)));
  span.setStatus({ code: SpanStatusCode.ERROR, message: error instanceof Error ? error.message : String(error) });
}

/** Preserve then/catch/finally, withResponse(), and asResponse() without a second request. */
function observePromise(promise: any, finish: (result: any) => any, fail: (error: unknown) => never): any {
  let parsed: Promise<any> | undefined;
  let mode: 'parsed' | 'raw' | undefined;
  const getParsed = () => {
    if (!parsed) {
      const alreadyFinished = mode === 'raw';
      mode = 'parsed';
      parsed = alreadyFinished ? Promise.resolve(promise) : Promise.resolve(promise).then(finish, fail);
    }
    return parsed;
  };
  return new Proxy(promise, {
    get(target, key) {
      if (key === 'then' || key === 'catch' || key === 'finally') return (...args: any[]) => (getParsed()[key] as Function)(...args);
      if (key === 'withResponse' && typeof target.withResponse === 'function') return async () => {
        const data = await getParsed();
        const response = await target.asResponse();
        return { data, response, request_id: response.headers.get('x-request-id') };
      };
      if (key === 'asResponse' && typeof target.asResponse === 'function') return async () => {
        try {
          const response = await target.asResponse();
          if (!mode) { mode = 'raw'; finish(undefined); }
          return response;
        } catch (error) { if (!mode) { mode = 'raw'; return fail(error); } throw error; }
      };
      const value = Reflect.get(target, key, target);
      return typeof value === 'function' ? value.bind(target) : value;
    },
  });
}

export function isPatched(): boolean { return active.size > 0; }
/** Restore active patches as well as the diagnostic state; intended only for tests. */
export function _resetPatchedStateForTesting(): void {
  for (const instrumentation of active) instrumentation.disable();
}
