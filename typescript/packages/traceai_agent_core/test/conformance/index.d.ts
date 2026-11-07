import type { ResolvedConfig } from "../../dist/src/index";
/**
 * Test-only import: @traceai/agent-core/test/conformance. No runtime re-export.
 *
 * A driver supplies native events to the actual adapter, not mocked trace handles.
 * The suite owns isolated HOME/config, loopback credential validation and OTLP
 * collection. prepareHost simulates a native host installer/list API, never the
 * Future AGI CLI. Override cli for out-of-tree consumers.
 *
 * Run after building packages with `pnpm --filter @traceai/agent-core test:conformance`.
 * The repository runner writes checks.json/groups.json and exits nonzero for any
 * failure; it does not carry expected-failure allowlists. CLI hosts are synthetic
 * fixtures, not proof of a real GUI/login, npm publication or Cloud readback.
 * CONFORMANCE_EVIDENCE saves synthetic wire payloads for reproducing failures.
 *
 * Drivers with aggregateUsage exercise a native aggregate-only turn as well.
 * Request-only sources must not invent aggregate counters. captureAgent records
 * an explicitly shared capture route separately from the tested CLI profile.
 */
export interface Scenario {
  id: string;
  session: string;
  calls: number;
  tools: number;
  model: string;
  outcome: "success" | "error" | "interrupted" | "incomplete";
  input: string;
  output: string;
  tool: string;
  error: string;
  secret: string;
  overlap?: boolean;
  lateMs?: number;
  aggregate?: boolean;
  inputTokens?: number;
  outputTokens?: number;
}
export interface Fixture {
  agent: string;
  home: string;
  project: string;
  endpoint: string;
  env: NodeJS.ProcessEnv;
  config: ResolvedConfig;
  options: {
    agent: string;
    home: string;
    env: NodeJS.ProcessEnv;
    projectDir: string;
  };
  batches: unknown[];
  requests: {
    url: string;
    body: unknown;
    authPresent: boolean;
    authMatches: boolean;
  }[];
  core: typeof import("../../dist/src/index");
  cleanup: Array<() => void | Promise<void>>;
  cli(args: string[], env?: NodeJS.ProcessEnv): Promise<CommandResult>;
  setAuthFailure(value: boolean): void;
  close(): Promise<void>;
}
export interface CommandResult {
  code: number | null;
  signal: string | null;
  stdout: string;
  stderr: string;
  timedOut: boolean;
}
export interface Driver {
  agent: string;
  group?: string;
  aggregateUsage?: boolean;
  incompleteUsageUnavailable?: boolean;
  /** Explicit shared native capture route; never changes the CLI lifecycle agent. */
  captureAgent?: string;
  /** Install a synthetic native host fixture, not a fake CoreAdapter. */
  prepareHost?(fixture: Fixture): Promise<void>;
  decodeRequest?(bytes: Buffer): Promise<unknown>;
  /** Optional when the CLI is not in the traceAI workspace next to agent-core. */
  cli?(
    fixture: Fixture,
    args: string[],
    env?: NodeJS.ProcessEnv,
  ): Promise<CommandResult>;
  create(
    fixture: Fixture,
  ): Promise<{
    turn(scenario: Scenario): Promise<void>;
    flush?(): Promise<void>;
    close?(): Promise<void>;
  }>;
}
export interface Check {
  check: string;
  status: "PASS" | "FAIL";
  evidence?: string;
  ms: number;
}
export interface Report {
  group: string;
  adapter: string;
  captureAgent: string;
  checks: Check[];
  failures: string[];
}
export function runConformance(driver: Driver): Promise<Report>;
export function fixture(
  agent: string,
  env?: NodeJS.ProcessEnv,
  decodeRequest?: (bytes: Buffer) => Promise<unknown>,
): Promise<Fixture>;
export function checkShape(
  batches: unknown[],
  expected: {
    calls: number;
    tools: number;
    turns?: number;
    outcome?: string;
    model?: string;
    inputTokens?: number;
    outputTokens?: number;
  },
): void;
export function checkPrivacy(batches: unknown[], policy: string): void;
export function attributes(span: any): Record<string, unknown>;
export function spans(batches: unknown[]): any[];
export function delay(milliseconds: number): Promise<void>;
export function run(
  command: string,
  args: string[],
  options?: {
    env?: NodeJS.ProcessEnv;
    cwd?: string;
    input?: string | Buffer;
    timeout?: number;
  },
): Promise<CommandResult>;
export const MARKERS: Readonly<{
  input: string;
  output: string;
  tool: string;
  error: string;
  secret: string;
}>;
export const FLAGS: readonly string[];
export const packages: string;
