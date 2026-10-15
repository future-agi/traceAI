/**
 * Compile-time contract: the real `@cursor/sdk` types (devDependency, 1.0.37)
 * fit the structural types the wrapper reads. `pnpm run typecheck:contract`
 * fails when an SDK release changes a shape this package depends on.
 */
import type { AgentOptions, Run, RunResult, SDKAgent, SDKMessage, SendOptions, TokenUsage } from "@cursor/sdk";
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
type Handled = "system" | "user" | "assistant" | "thinking" | "tool_call" | "status" | "request" | "task" | "usage";
export const handled: Handled[] = [] as SDKMessage["type"][];
export const known: SDKMessage["type"][] = [] as Handled[];
