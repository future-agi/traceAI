import { generateTraceConfig } from "@traceai/fi-core";
import { SemanticConventions as S } from "@traceai/fi-semantic-conventions";
import { inputAttributes } from "../attributes";

it.each(["userAccessToken", "user_access_token"])("omits %s from telemetry without changing any connector", (field) => {
  const authenticated = Object.freeze({ id: "private-connector", [field]: "synthetic-token" });
  const publicConnector = Object.freeze({ id: "web-search", options: { site: "example.test" } });
  const request = Object.freeze({ message: "question", connectors: Object.freeze([authenticated, publicConnector]) });
  const attrs = inputAttributes("chat", request, generateTraceConfig());
  expect(JSON.stringify(attrs)).not.toContain("synthetic-token");
  expect(JSON.parse(attrs[S.RAW_INPUT] as string).connectors).toEqual([
    { id: "private-connector" }, publicConnector,
  ]);
  expect(request.connectors[0]).toBe(authenticated);
  expect(authenticated[field]).toBe("synthetic-token");
});
