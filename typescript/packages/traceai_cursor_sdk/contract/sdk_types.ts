/**
 * Compile-time contract: the real `@cursor/sdk` types (devDependency, 1.2.0)
 * fit the structural types the wrapper reads. `pnpm run typecheck:contract`
 * fails when an SDK release changes a shape this package depends on.
 */
import type { AgentOptions, Run, RunResult, SDKAgent, SDKMessage, SDKUserMessage, SendOptions, TokenUsage } from "@cursor/sdk";
import { Agent } from "@cursor/sdk";
import type {
  AgentClassLike,
  AgentLike,
  AgentOptionsLike,
  CursorSDKModuleLike,
  RunLike,
  RunResultLike,
  SDKMessageLike,
  SendOptionsLike,
  TokenUsageLike,
} from "../src/types";

export const agentClass: AgentClassLike = Agent;
export const sdkModule: CursorSDKModuleLike = { Agent };
export const agent = (value: SDKAgent): AgentLike => value;
export const run = (value: Run): RunLike => value;
export const runResult = (value: RunResult): RunResultLike => value;
export const message = (value: SDKMessage): SDKMessageLike => value;
export const usage = (value: TokenUsage): TokenUsageLike => value;
export const agentOptions = (value: AgentOptions): AgentOptionsLike => value;
export const sendOptions = (value: SendOptions): SendOptionsLike => value;

/** Every SDKMessage `type` the tracer switches on exists in the SDK union. */
type BaseMessages = "system" | "user" | "assistant" | "thinking" | "tool_call" | "status" | "request" | "task" | "usage";
// Summary first appeared in 1.2; allow the older SDK's union without masking any other new type.
type Handled = BaseMessages | Extract<SDKMessage["type"], "summary">;
export const handled: Handled[] = [] as SDKMessage["type"][];
export const known: SDKMessage["type"][] = [] as Handled[];

/** The inline image variant the attachment path reads keeps `{ data, mimeType }` as strings. */
type SDKImageEntry = NonNullable<SDKUserMessage["images"]>[number];
type InlineImage = Extract<SDKImageEntry, { data: unknown }>;
export const inlineImage = (value: InlineImage): { data: string; mimeType: string } => value;
export const inlineImageExists: InlineImage extends never ? never : true = true;
