import { describe, it, expect, jest, beforeEach, afterEach } from '@jest/globals';
import { userInfo } from 'node:os';
import { FITracerProvider } from '../otel';

jest.mock('@opentelemetry/exporter-trace-otlp-http', () => ({
  OTLPTraceExporter: jest.fn().mockImplementation(() => ({
    export: jest.fn((_spans: any, callback: any) => callback({ code: 0 })),
    shutdown: jest.fn(() => Promise.resolve()),
    forceFlush: jest.fn(() => Promise.resolve()),
  })),
}));

// A secret passed on the command line, as `node app.js --api-key=...` would.
const SECRET_FLAG = '--api-key=sk-test-0000000000000000';

// The resource every exported span carries.
function exportedResourceAttributes(): Record<string, unknown> {
  const provider = new FITracerProvider();
  const span = provider.getTracer('resource-detection-test').startSpan('probe') as any;
  span.end();
  return { ...span.resource.attributes };
}

describe('FITracerProvider resource detection', () => {
  const originalEnv = process.env;
  const originalArgv = process.argv;

  beforeEach(() => {
    process.env = { ...originalEnv };
    delete process.env.OTEL_NODE_RESOURCE_DETECTORS;
    process.argv = [...originalArgv, SECRET_FLAG];
  });

  afterEach(() => {
    process.env = originalEnv;
    process.argv = originalArgv;
  });

  it('exports no command line, OS user, executable path or host by default', () => {
    const attributes = exportedResourceAttributes();
    for (const key of ['process.command', 'process.command_args', 'process.owner', 'process.executable.path', 'process.executable.name', 'process.pid', 'host.name', 'host.id', 'os.type']) {
      expect(attributes).not.toHaveProperty([key]);
    }
    // service.name keeps OpenTelemetry's own default, unknown_service:<argv0>, so the
    // executable path is checked by key above rather than by value.
    const serialized = JSON.stringify(attributes);
    expect(serialized).not.toContain('sk-test-0000000000000000');
    expect(serialized).not.toContain(JSON.stringify(userInfo().username));
  });

  it('keeps the runtime name and version by default', () => {
    const attributes = exportedResourceAttributes();
    expect(attributes['process.runtime.name']).toBe('nodejs');
    expect(attributes['process.runtime.version']).toBe(process.versions.node);
  });

  it('keeps OTEL_SERVICE_NAME and OTEL_RESOURCE_ATTRIBUTES', () => {
    process.env.OTEL_SERVICE_NAME = 'checkout';
    process.env.OTEL_RESOURCE_ATTRIBUTES = 'deployment.environment.name=staging';
    const attributes = exportedResourceAttributes();
    expect(attributes['service.name']).toBe('checkout');
    expect(attributes['deployment.environment.name']).toBe('staging');
  });

  it('adds full process detection only when OTEL_NODE_RESOURCE_DETECTORS asks for it', () => {
    process.env.OTEL_NODE_RESOURCE_DETECTORS = 'env,process';
    const attributes = exportedResourceAttributes();
    expect(attributes['process.command_args']).toEqual(expect.arrayContaining([SECRET_FLAG]));
    expect(attributes['process.pid']).toBe(process.pid);
    expect(attributes).not.toHaveProperty(['host.name']);
  });

  it('treats all as host, os and process, and none as no detection', () => {
    process.env.OTEL_NODE_RESOURCE_DETECTORS = 'all';
    const all = exportedResourceAttributes();
    expect(all).toHaveProperty(['host.name']);
    expect(all).toHaveProperty(['os.type']);
    expect(all).toHaveProperty(['process.pid']);

    process.env.OTEL_NODE_RESOURCE_DETECTORS = 'none';
    process.env.OTEL_SERVICE_NAME = 'checkout';
    const none = exportedResourceAttributes();
    expect(Object.keys(none).filter((key) => /^(process|host|os)\./.test(key))).toEqual([]);
    expect(none['service.name']).toBe('checkout');
  });
});
