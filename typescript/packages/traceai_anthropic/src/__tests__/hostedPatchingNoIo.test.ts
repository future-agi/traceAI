/**
 * Patching the cloud-hosted Anthropic clients does no I/O and reads no credentials: loading the
 * SDKs under the automatic loader must never reach the network, the credential files, or leave a
 * rejected promise behind.
 */
import { describe, it, expect, afterEach, jest } from "@jest/globals";
import * as path from "node:path";
import * as bedrockModule from "@anthropic-ai/bedrock-sdk";
import * as vertexModule from "@anthropic-ai/vertex-sdk";
import { AnthropicInstrumentation } from "../instrumentation";

/* eslint-disable @typescript-eslint/no-require-imports */
// require() gives the real, writable module objects that spies need.
const fs = require("node:fs");
const http = require("node:http");
const https = require("node:https");

const CREDENTIAL_FILE = "/nonexistent/traceai-canary-credentials.json";
const googleAuthPath = require.resolve("google-auth-library", { paths: [path.dirname(require.resolve("@anthropic-ai/vertex-sdk"))] });
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { GoogleAuth } = require(googleAuthPath);

const credentialPath = (value: unknown) =>
  typeof value === "string" && /traceai-canary-credentials|application_default_credentials|\.aws[\\/](credentials|config)|gcloud/.test(value);

let instrumentation: AnthropicInstrumentation | undefined;
afterEach(() => {
  instrumentation?.disable();
  instrumentation = undefined;
  jest.restoreAllMocks();
  delete process.env.GOOGLE_APPLICATION_CREDENTIALS;
});

describe("patching the hosted clients", () => {
  it("makes no request, constructs no GoogleAuth client, reads no credential file and leaves no unhandled rejection", async () => {
    process.env.GOOGLE_APPLICATION_CREDENTIALS = CREDENTIAL_FILE;
    const calls: string[] = [];
    const record = (name: string) => (..._args: unknown[]) => { calls.push(name); throw new Error(`unexpected ${name}`); };
    jest.spyOn(globalThis, "fetch").mockImplementation(record("fetch") as never);
    jest.spyOn(http, "request").mockImplementation(record("http.request") as never);
    jest.spyOn(https, "request").mockImplementation(record("https.request") as never);
    const getClient = jest.spyOn(GoogleAuth.prototype, "getClient");
    const credentialReads: string[] = [];
    for (const name of ["readFileSync", "readFile", "existsSync", "statSync", "openSync", "createReadStream"] as const) {
      const real = (fs as any)[name];
      jest.spyOn(fs as any, name).mockImplementation(function (this: unknown, ...args: unknown[]) {
        if (credentialPath(args[0])) credentialReads.push(`${name}:${String(args[0])}`);
        return real.apply(this, args);
      });
    }
    const rejections: unknown[] = [];
    const onRejection = (reason: unknown) => rejections.push(reason);
    process.on("unhandledRejection", onRejection);
    try {
      instrumentation = new AnthropicInstrumentation({ instrumentationConfig: { enabled: false } });
      instrumentation.enable();
      instrumentation.manuallyInstrument(vertexModule);
      instrumentation.manuallyInstrument(bedrockModule);
      // Let any eagerly started credential discovery settle and surface.
      await new Promise((resolve) => setTimeout(resolve, 100));
      await new Promise((resolve) => setImmediate(resolve));
    } finally {
      process.off("unhandledRejection", onRejection);
    }
    expect(calls).toEqual([]);
    expect(getClient).not.toHaveBeenCalled();
    expect(credentialReads).toEqual([]);
    expect(rejections).toEqual([]);
  });

  it("still patches the Messages class of each hosted client", async () => {
    const { AnthropicBedrock } = bedrockModule;
    const { AnthropicVertex } = vertexModule;
    const bedrock = new AnthropicBedrock({ awsRegion: "us-east-1", awsAccessKey: "a", awsSecretKey: "b" });
    const vertex = new AnthropicVertex({ region: "us-east5", projectId: "p", googleAuth: { getClient: async () => ({}) } as never });
    const messages = Object.getPrototypeOf(bedrock.messages);
    expect(Object.getPrototypeOf(vertex.messages)).toBe(messages);
    const before = messages.create;
    instrumentation = new AnthropicInstrumentation({ instrumentationConfig: { enabled: false } });
    instrumentation.enable();
    instrumentation.manuallyInstrument(bedrockModule);
    expect(messages.create).not.toBe(before);
    instrumentation.disable();
    expect(messages.create).toBe(before);
  });

  it("leaves a hosted client with native OpenTelemetry support unpatched and never constructs it", () => {
    let constructed = 0;
    class AnthropicBedrock {
      constructor() { constructed++; }
      get openTelemetry() { return {}; }
    }
    const create = () => "native";
    const messagesPrototype = { create };
    (AnthropicBedrock.prototype as any).messages = { constructor: { prototype: messagesPrototype } };
    instrumentation = new AnthropicInstrumentation({ instrumentationConfig: { enabled: false } });
    instrumentation.enable();
    instrumentation.manuallyInstrument({ AnthropicBedrock });
    expect(constructed).toBe(0);
    expect(messagesPrototype.create).toBe(create);
  });
});
