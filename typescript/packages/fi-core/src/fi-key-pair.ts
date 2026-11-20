/** Whether `headers` carries a complete, non-empty x-api-key / x-secret-key pair (names in any case, string values). */
export function hasCompleteKeyPair(headers: unknown): boolean {
  if (typeof headers !== "object" || headers === null) return false;
  const value = (name: string): unknown => Object.entries(headers).find(([key]) => key.toLowerCase() === name)?.[1];
  const key = value("x-api-key"), secret = value("x-secret-key");
  return typeof key === "string" && key.trim() !== "" && typeof secret === "string" && secret.trim() !== "";
}
