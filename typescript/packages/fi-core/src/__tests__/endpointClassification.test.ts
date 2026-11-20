import { isFiEndpoint } from "../fi-endpoint";

// Whether an endpoint names a Future AGI host is decided on the host the exporter will dial, parsed the way
// the exporter parses it. Only the host decides: no environment is consulted.
const HTTP_FI = [
  "https://api.futureagi.com/tracer/v1/traces", "https://API.FutureAGI.com", "http://api.futureagi.com:8080/x",
  "https://user:pw@api.futureagi.com/", "https://evil.example@api.futureagi.com/", "https://grpc.futureagi.com",
];
const HTTP_OTHER = [
  "evil.example://api.futureagi.com", "ftp://api.futureagi.com", "javascript://api.futureagi.com", "https://api.futureagi.com.evil.example",
  "https://evil.example/api.futureagi.com", "https://evil.example?x=api.futureagi.com", "https://api.futureagi.com@evil.example/",
  "https://[::1]/", "https://api.futureagi.com./x", "https://evilfutureagi.com", "//api.futureagi.com", "api.futureagi.com", "", "not a url",
  "https://127.0.0.1:4318/v1/traces",
];
const GRPC_FI = ["grpc.futureagi.com:443", "https://grpc.futureagi.com", "http://grpc.futureagi.com:4317", "GRPC.FUTUREAGI.COM", "https://user@grpc.futureagi.com:443", "api.futureagi.com"];
const GRPC_OTHER = [
  "evil.example://grpc.futureagi.com", "grpc.futureagi.com.evil.example:443", "https://grpc.futureagi.com@evil.example", "unix:///tmp/collector.sock",
  "dns:///grpc.futureagi.com", "[::1]:4317", "xxxxxxxxx://grpc.futureagi.com", "grpc.futureagi.com./", "127.0.0.1:4317", "", "evilfutureagi.com:443",
];

test.each(HTTP_FI)("HTTP %s is Future AGI", endpoint => expect(isFiEndpoint(endpoint, "http")).toBe(true));
test.each(HTTP_OTHER)("HTTP %p is not Future AGI", endpoint => expect(isFiEndpoint(endpoint, "http")).toBe(false));
test.each(GRPC_FI)("gRPC %s is Future AGI", endpoint => expect(isFiEndpoint(endpoint, "grpc")).toBe(true));
test.each(GRPC_OTHER)("gRPC %p is not Future AGI", endpoint => expect(isFiEndpoint(endpoint, "grpc")).toBe(false));
