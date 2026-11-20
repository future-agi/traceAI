// The classification must agree with the host the installed OTLP gRPC exporter dials for the same input.
import { convertLegacyOtlpGrpcOptions } from "@opentelemetry/otlp-grpc-exporter-base";
import { endpointTarget, isLoopbackTarget } from "../fi-endpoint";

const GRPC_INPUTS = ["grpc.futureagi.com:443", "https://grpc.futureagi.com", "http://grpc.futureagi.com:4317", "GRPC.FUTUREAGI.COM", "https://user@grpc.futureagi.com:443",
  "evil.example://grpc.futureagi.com", "grpc.futureagi.com.evil.example:443", "https://grpc.futureagi.com@evil.example", "unix:///tmp/collector.sock", "dns:///grpc.futureagi.com",
  "xxxxxxxxx://grpc.futureagi.com", "grpc.futureagi.com./", "127.0.0.1:4317", "localhost:4317", "localhost.evil.example:4317", "http://127.0.0.1:4317", "127.0.0.1.evil.example:4317",
  "evil.example/localhost", "evil.example?x=127.0.0.1", "user@localhost:4317", "[::1]:4317", "https://[::1]:4317"];
describe("gRPC target parsing agrees with the installed exporter", () => {
  test.each(GRPC_INPUTS)("%p", input => {
    const dialled = (() => { try { return (convertLegacyOtlpGrpcOptions({ url: input } as any, "TRACES") as { url: string }).url; } catch { return undefined; } })();
    const mine = endpointTarget(input, "grpc");
    if (!dialled || dialled.startsWith("unix:")) { // an empty host dials nothing
       expect(mine).toBeUndefined(); return; }
    expect(mine).toBeDefined();
    expect(new URL(`https://${dialled}`).hostname).toBe(mine!.hostname);
  });
});
describe("loopback targets", () => {
  test.each(["localhost:4317", "127.0.0.1:4317", "http://localhost:4317", "[::1]:4317", "LOCALHOST"])("%p is loopback", e => expect(isLoopbackTarget(e)).toBe(true));
  test.each(["localhost.evil.example:4317", "127.0.0.1.evil.example:4317", "evil.example/localhost", "evil.example?x=127.0.0.1", "user@evil.example:1/localhost", "", "unix:///tmp/localhost.sock"])("%p is not loopback", e => expect(isLoopbackTarget(e)).toBe(false));
});
