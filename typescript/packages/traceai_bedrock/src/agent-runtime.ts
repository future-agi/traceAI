import {
  Context,
  context,
  diag,
  Span,
  SpanKind,
  SpanStatusCode,
  trace,
} from "@opentelemetry/api";
import { FITracer, safelyJSONStringify } from "@traceai/fi-core";
import {
  FISpanKind,
  MimeType,
  SemanticConventions,
} from "@traceai/fi-semantic-conventions";
import { setSpanAttribute } from "./attributes/attribute-helpers";

/**
 * Amazon Bedrock Agents (`@aws-sdk/client-bedrock-agent-runtime`) tracing.
 *
 * `InvokeAgent` / `InvokeInlineAgent` answer with a `completion` event stream: `chunk` events
 * carry the answer text and, when the caller sets `enableTrace`, `trace` events describe the
 * agent's steps (model invocations, action group calls, knowledge base lookups). The stream is
 * observed as the application pulls it; nothing reads ahead and no background reader runs.
 */

const GEN_AI_AGENT_ID = "gen_ai.agent.id";
const GEN_AI_AGENT_ALIAS_ID = "gen_ai.agent.alias_id";

type Json = Record<string, unknown>;

const isRecord = (value: unknown): value is Json =>
  value != null && typeof value === "object" && !Array.isArray(value);
const asString = (value: unknown): string | undefined =>
  typeof value === "string" && value !== "" ? value : undefined;
const asRecord = (value: unknown): Json | undefined =>
  isRecord(value) ? value : undefined;

/** Request attributes shared by InvokeAgent and InvokeInlineAgent. */
export function agentRequest(commandName: string, input: unknown): {
  name: string;
  attributes: Record<string, string>;
} {
  const request = asRecord(input) ?? {};
  const attributes: Record<string, string> = {
    [SemanticConventions.FI_SPAN_KIND]: FISpanKind.AGENT,
    [SemanticConventions.GEN_AI_OPERATION_NAME]: "invoke_agent",
  };
  const set = (key: string, value: unknown) => {
    const text = asString(value);
    if (text !== undefined) attributes[key] = text;
  };
  set(SemanticConventions.SESSION_ID, request.sessionId);
  set(SemanticConventions.INPUT_VALUE, request.inputText);

  if (commandName === "InvokeInlineAgentCommand") {
    set(SemanticConventions.LLM_MODEL_NAME, request.foundationModel);
    return { name: "invoke_agent inline", attributes };
  }
  set(GEN_AI_AGENT_ID, request.agentId);
  set(GEN_AI_AGENT_ALIAS_ID, request.agentAliasId);
  const target = [asString(request.agentId), asString(request.agentAliasId)]
    .filter(Boolean)
    .join("/");
  return { name: target ? `invoke_agent ${target}` : "invoke_agent", attributes };
}

/** Parses the model prompt Bedrock Agents records as text: usually `{ system, messages }` JSON. */
function promptMessages(text: string): Json[] {
  try {
    const parsed: unknown = JSON.parse(text);
    if (isRecord(parsed)) {
      const messages: Json[] = [];
      const system = contentText(parsed.system);
      if (system) messages.push({ role: "system", content: system });
      if (Array.isArray(parsed.messages)) {
        for (const message of parsed.messages) {
          if (!isRecord(message)) continue;
          const content = contentText(message.content);
          messages.push({ role: asString(message.role) ?? "user", ...(content ? { content } : {}) });
        }
      }
      if (messages.length > 0) return messages;
    }
  } catch {
    // Not JSON: the prompt is plain text.
  }
  return [{ role: "user", content: text }];
}

function contentText(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((part) => (isRecord(part) && typeof part.text === "string" ? part.text : ""))
      .filter(Boolean)
      .join("\n");
  }
  return "";
}

function modelOutputText(output: Json): string | undefined {
  const parsed = asRecord(output.parsedResponse);
  const fromParsed = asString(parsed?.text) ?? asString(parsed?.rationale);
  if (fromParsed) return fromParsed;
  const raw = asRecord(output.rawResponse);
  const content = asString(raw?.content);
  if (!content) return undefined;
  try {
    const decoded = asRecord(JSON.parse(content));
    const fromContent = contentText(decoded?.content);
    return fromContent || content;
  } catch {
    return content;
  }
}

interface StepOptions {
  tracer: FITracer;
  parent: Span;
}

/** Child spans for the steps in a trace event stream, built as the events arrive. */
class AgentTraceBuilder {
  private readonly open = new Map<string, Span>();
  private readonly knowledgeBaseIds = new WeakMap<Span, string | undefined>();
  private readonly parentContext: Context;

  constructor(private readonly options: StepOptions) {
    this.parentContext = trace.setSpan(context.active(), options.parent);
  }

  private start(name: string, attributes: Record<string, string | number>): Span {
    const span = this.options.tracer.startSpan(
      name,
      { kind: SpanKind.INTERNAL, attributes },
      this.parentContext,
    );
    return span;
  }

  /** Registers an open step by `<kind>:<traceId>`, ending any earlier step with the same key. */
  private replace(key: string, span: Span) {
    this.open.get(key)?.end();
    this.open.set(key, span);
  }

  private take(key: string | undefined, prefix: string): Span | undefined {
    if (key !== undefined && this.open.has(`${prefix}:${key}`)) {
      const span = this.open.get(`${prefix}:${key}`);
      this.open.delete(`${prefix}:${key}`);
      return span;
    }
    // An output without a matching id belongs to the most recent open step of its kind.
    const keys = [...this.open.keys()].filter((k) => k.startsWith(`${prefix}:`));
    const last = keys[keys.length - 1];
    if (last === undefined) return undefined;
    const span = this.open.get(last);
    this.open.delete(last);
    return span;
  }

  /** Handles one `trace` event of the completion stream. */
  handle(event: Json) {
    const part = asRecord(event.trace);
    const body = asRecord(part?.trace);
    if (!body) return;
    for (const [kind, value] of Object.entries(body)) {
      const section = asRecord(value);
      if (!section) continue;
      if (kind === "failureTrace") {
        this.failure(section);
        continue;
      }
      if (kind === "guardrailTrace" || kind === "customOrchestrationTrace") continue;
      const modelInput = asRecord(section.modelInvocationInput);
      if (modelInput) this.modelInput(modelInput);
      const modelOutput = asRecord(section.modelInvocationOutput);
      if (modelOutput) this.modelOutput(modelOutput);
      const invocation = asRecord(section.invocationInput);
      if (invocation) this.invocationInput(invocation);
      const observation = asRecord(section.observation);
      if (observation) this.observation(observation);
    }
  }

  private modelInput(input: Json) {
    const model = asString(input.foundationModel);
    const span = this.start(model ? `chat ${model}` : "chat", {
      [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
      [SemanticConventions.GEN_AI_OPERATION_NAME]: "chat",
      [SemanticConventions.LLM_PROVIDER]: "aws",
    });
    setSpanAttribute(span, SemanticConventions.LLM_MODEL_NAME, model);
    const config = asRecord(input.inferenceConfiguration);
    if (config && Object.keys(config).length > 0) {
      setSpanAttribute(
        span,
        SemanticConventions.LLM_INVOCATION_PARAMETERS,
        safelyJSONStringify(config),
      );
    }
    const text = asString(input.text);
    if (text) {
      setSpanAttribute(span, SemanticConventions.INPUT_VALUE, text);
      setSpanAttribute(
        span,
        SemanticConventions.LLM_INPUT_MESSAGES,
        safelyJSONStringify(promptMessages(text)),
      );
    }
    this.replace(`model:${asString(input.traceId) ?? ""}`, span);
  }

  private modelOutput(output: Json) {
    let span = this.take(asString(output.traceId), "model");
    if (!span) {
      span = this.start("chat", {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
        [SemanticConventions.GEN_AI_OPERATION_NAME]: "chat",
        [SemanticConventions.LLM_PROVIDER]: "aws",
      });
    }
    const text = modelOutputText(output);
    if (text) {
      setSpanAttribute(span, SemanticConventions.OUTPUT_VALUE, text);
      setSpanAttribute(
        span,
        SemanticConventions.LLM_OUTPUT_MESSAGES,
        safelyJSONStringify([{ role: "assistant", content: text }]),
      );
    }
    const usage = asRecord(asRecord(output.metadata)?.usage);
    const input = typeof usage?.inputTokens === "number" ? usage.inputTokens : undefined;
    const completion = typeof usage?.outputTokens === "number" ? usage.outputTokens : undefined;
    if (input !== undefined) span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_PROMPT, input);
    if (completion !== undefined) {
      span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION, completion);
    }
    if (input !== undefined && completion !== undefined) {
      span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_TOTAL, input + completion);
    }
    span.setStatus({ code: SpanStatusCode.OK });
    span.end();
  }

  private invocationInput(input: Json) {
    const traceId = asString(input.traceId) ?? "";
    const action = asRecord(input.actionGroupInvocationInput);
    const knowledgeBase = asRecord(input.knowledgeBaseLookupInput);
    const code = asRecord(input.codeInterpreterInvocationInput);
    const collaborator = asRecord(input.agentCollaboratorInvocationInput);

    if (action) {
      const name =
        asString(action.function) ??
        asString(action.apiPath) ??
        asString(action.actionGroupName) ??
        "action_group";
      const span = this.start(`execute_tool ${name}`, {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.TOOL,
        [SemanticConventions.GEN_AI_OPERATION_NAME]: "execute_tool",
      });
      setSpanAttribute(span, SemanticConventions.TOOL_NAME, name);
      setSpanAttribute(span, SemanticConventions.TOOL_DESCRIPTION, asString(action.description));
      const parameters = action.parameters ?? action.requestBody;
      if (parameters !== undefined) {
        const serialized = safelyJSONStringify(parameters);
        setSpanAttribute(span, SemanticConventions.TOOL_PARAMETERS, serialized);
        setSpanAttribute(span, SemanticConventions.INPUT_VALUE, serialized);
        setSpanAttribute(span, SemanticConventions.INPUT_MIME_TYPE, MimeType.JSON);
      }
      setSpanAttribute(
        span,
        SemanticConventions.METADATA,
        safelyJSONStringify(compact({
          action_group_name: asString(action.actionGroupName),
          execution_type: asString(action.executionType),
          invocation_id: asString(action.invocationId),
          verb: asString(action.verb),
          api_path: asString(action.apiPath),
        })),
      );
      this.replace(`tool:${traceId}`, span);
    } else if (code) {
      const span = this.start("execute_tool code_interpreter", {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.TOOL,
        [SemanticConventions.GEN_AI_OPERATION_NAME]: "execute_tool",
      });
      setSpanAttribute(span, SemanticConventions.TOOL_NAME, "code_interpreter");
      setSpanAttribute(span, SemanticConventions.INPUT_VALUE, asString(code.code));
      this.replace(`tool:${traceId}`, span);
    } else if (knowledgeBase) {
      const id = asString(knowledgeBase.knowledgeBaseId);
      const span = this.start(id ? `retrieve ${id}` : "retrieve", {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.RETRIEVER,
        [SemanticConventions.GEN_AI_OPERATION_NAME]: "retrieval",
      });
      setSpanAttribute(span, SemanticConventions.INPUT_VALUE, asString(knowledgeBase.text));
      setSpanAttribute(
        span,
        SemanticConventions.METADATA,
        safelyJSONStringify({ knowledge_base_id: id }),
      );
      this.knowledgeBaseIds.set(span, id);
      this.replace(`kb:${traceId}`, span);
    } else if (collaborator) {
      const name = asString(collaborator.agentCollaboratorName);
      const span = this.start(name ? `invoke_agent ${name}` : "invoke_agent", {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.AGENT,
        [SemanticConventions.GEN_AI_OPERATION_NAME]: "invoke_agent",
      });
      setSpanAttribute(span, SemanticConventions.INPUT_VALUE, asString(asRecord(collaborator.input)?.text));
      this.replace(`agent:${traceId}`, span);
    }
  }

  private observation(observation: Json) {
    const traceId = asString(observation.traceId);
    const action = asRecord(observation.actionGroupInvocationOutput);
    const knowledgeBase = asRecord(observation.knowledgeBaseLookupOutput);
    const code = asRecord(observation.codeInterpreterInvocationOutput);
    const collaborator = asRecord(observation.agentCollaboratorInvocationOutput);

    if (action) {
      const span = this.take(traceId, "tool");
      if (!span) return;
      setSpanAttribute(span, SemanticConventions.OUTPUT_VALUE, asString(action.text));
      this.finish(span);
    } else if (code) {
      const span = this.take(traceId, "tool");
      if (!span) return;
      const error = asString(code.executionError);
      setSpanAttribute(
        span,
        SemanticConventions.OUTPUT_VALUE,
        asString(code.executionOutput) ?? error,
      );
      if (error) span.setStatus({ code: SpanStatusCode.ERROR, message: error });
      else span.setStatus({ code: SpanStatusCode.OK });
      span.end();
    } else if (knowledgeBase) {
      const span = this.take(traceId, "kb");
      if (!span) return;
      const references = Array.isArray(knowledgeBase.retrievedReferences)
        ? knowledgeBase.retrievedReferences
        : [];
      references.forEach((reference, index) => {
        const ref = asRecord(reference) ?? {};
        const base = `${SemanticConventions.RETRIEVAL_DOCUMENTS}.${index}`;
        const chunkId = asString(asRecord(ref.metadata)?.["x-amz-bedrock-kb-chunk-id"]);
        setSpanAttribute(span, `${base}.${SemanticConventions.DOCUMENT_ID}`, chunkId);
        setSpanAttribute(
          span,
          `${base}.${SemanticConventions.DOCUMENT_CONTENT}`,
          asString(asRecord(ref.content)?.text),
        );
        if (typeof ref.score === "number") {
          span.setAttribute(`${base}.${SemanticConventions.DOCUMENT_SCORE}`, ref.score);
        }
      });
      setSpanAttribute(
        span,
        SemanticConventions.METADATA,
        safelyJSONStringify({
          knowledge_base_id: this.knowledgeBaseIds.get(span),
          document_count: references.length,
        }),
      );
      this.finish(span);
    } else if (collaborator) {
      const span = this.take(traceId, "agent");
      if (!span) return;
      setSpanAttribute(
        span,
        SemanticConventions.OUTPUT_VALUE,
        asString(asRecord(collaborator.output)?.text),
      );
      this.finish(span);
    }
  }

  private failure(failure: Json) {
    const reason = asString(failure.failureReason) ?? asString(failure.failureCode) ?? "agent failure";
    const span = this.start("agent_failure", {
      [SemanticConventions.FI_SPAN_KIND]: FISpanKind.CHAIN,
    });
    span.setStatus({ code: SpanStatusCode.ERROR, message: reason });
    span.setAttribute("error.type", asString(failure.failureCode) ?? "AgentFailure");
    span.end();
  }

  private finish(span: Span) {
    span.setStatus({ code: SpanStatusCode.OK });
    span.end();
  }

  /** Ends every step still open (the stream stopped before its output arrived). */
  endAll() {
    for (const span of this.open.values()) {
      try {
        span.end();
      } catch (error) {
        diag.warn("Error ending Bedrock agent step span:", error);
      }
    }
    this.open.clear();
  }
}

function compact(values: Record<string, string | undefined>): Record<string, string> {
  return Object.fromEntries(
    Object.entries(values).filter((entry): entry is [string, string] => entry[1] !== undefined),
  );
}

/**
 * Wraps the `completion` stream of an InvokeAgent response. The agent span ends when the
 * stream is fully consumed, fails, or is abandoned (the caller stops iterating or closes it).
 */
export function observeAgentCompletion({
  stream,
  span,
  tracer,
  onDone,
  onError,
}: {
  stream: AsyncIterable<unknown>;
  span: Span;
  tracer: FITracer;
  onDone: (cancelled: boolean) => void;
  onError: (error: unknown) => void;
}): AsyncIterable<unknown> {
  const steps = new AgentTraceBuilder({ tracer, parent: span });
  const decoder = new TextDecoder();
  let output = "";
  let finished = false;

  const finalize = (result: { error: unknown } | { cancelled: boolean }) => {
    if (finished) return;
    finished = true;
    try {
      output += decoder.decode();
      if (output) {
        setSpanAttribute(span, SemanticConventions.OUTPUT_VALUE, output);
      }
      steps.endAll();
    } catch (error) {
      diag.warn("Error finalizing Bedrock agent spans:", error);
    }
    if ("error" in result) onError(result.error);
    else onDone(result.cancelled);
  };

  const observe = (event: unknown) => {
    try {
      if (!isRecord(event)) return;
      if (isRecord(event.chunk)) {
        const bytes = event.chunk.bytes;
        if (bytes instanceof Uint8Array) output += decoder.decode(bytes, { stream: true });
      } else if (isRecord(event.trace)) {
        steps.handle(event);
      } else if (isRecord(event.returnControl)) {
        span.addEvent("return_control");
      }
    } catch (error) {
      // Instrumentation failures must never reach the application.
      diag.warn("Error processing Bedrock agent event:", error);
    }
  };

  return {
    [Symbol.asyncIterator](): AsyncIterator<unknown> {
      const source = stream[Symbol.asyncIterator]();
      return {
        async next(...args: [] | [undefined]) {
          let result: IteratorResult<unknown>;
          try {
            result = await source.next(...args);
          } catch (error) {
            finalize({ error });
            throw error;
          }
          if (result.done) finalize({ cancelled: false });
          else observe(result.value);
          return result;
        },
        async return(value?: unknown) {
          // Close the source first; the caller's cancellation must reach the SDK stream.
          try {
            return source.return ? await source.return(value) : { done: true as const, value };
          } finally {
            finalize({ cancelled: true });
          }
        },
        async throw(error?: unknown) {
          try {
            if (source.throw) return await source.throw(error);
            throw error;
          } finally {
            finalize({ cancelled: true });
          }
        },
      };
    },
  };
}
