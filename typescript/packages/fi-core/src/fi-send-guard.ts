// Declarations only at this commit: the send-level guard is added in the next commits.
export function grpcProxyFor(_host: string, _env: Readonly<Record<string, string | undefined>>): string | undefined {
  return undefined;
}
export interface FixtureOutcome { decision: "allow" | "refuse"; code: string | null; dialed: string; tls: boolean; keysOnWire: boolean }
export function evaluateRow(_input: unknown): FixtureOutcome {
  return { decision: "refuse", code: "NOT_IMPLEMENTED", dialed: "", tls: false, keysOnWire: false };
}
