import { InMemorySpanExporter, SimpleSpanProcessor } from '@opentelemetry/sdk-trace-base';
import { NodeTracerProvider } from '@opentelemetry/sdk-trace-node';
import { LiveKitInstrumentation, isPatched } from '../instrumentation';
import { createRequire } from 'node:module';

const shimmer = createRequire(require.resolve('@opentelemetry/instrumentation'))('shimmer');

const exporter = new InMemorySpanExporter();
const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
let instrumentation: LiveKitInstrumentation;
beforeEach(() => {
  instrumentation = new LiveKitInstrumentation();
  instrumentation.setTracerProvider(provider);
});
afterEach(() => {
  instrumentation.disable();
  expect(isPatched()).toBe(false);
  exporter.reset();
});
afterAll(() => provider.shutdown());

class Room {
  connect = async () => 'connected';
  disconnect = async () => 'disconnected';
}

test.each(['facade', 'existing'] as const)('same %s room resumes tracing across repeated toggles', async mode => {
  const sdk = Object.freeze({ Room });
  const client = instrumentation.manuallyInstrument(sdk);
  const room = mode === 'facade' ? new client.Room() : instrumentation.instrumentRoom(new Room());
  for (let cycle = 0; cycle < 3; cycle++) {
    await expect(room.connect()).resolves.toBe('connected');
    await expect(room.disconnect()).resolves.toBe('disconnected');
    expect(exporter.getFinishedSpans()).toHaveLength((cycle + 1) * 2);
    instrumentation.disable();
    await room.connect();
    await room.disconnect();
    expect(exporter.getFinishedSpans()).toHaveLength((cycle + 1) * 2);
    instrumentation.enable();
    // Enabling is sufficient; repeated registration must also remain idempotent.
    if (cycle > 0) instrumentation.manuallyInstrument(sdk);
    instrumentation.enable();
  }
});

describe.each(['plain', 'shimmer'] as const)('%s third-party wrapper', wrapperKind => {
  test.each(['enable', 'instrumentRoom'] as const)('%s remains idempotent while already enabled', async registration => {
    const room = instrumentation.instrumentRoom(new Room());
    const thirdPartyCalls = jest.fn();
    for (const method of ['connect', 'disconnect'] as const) {
      const wrap = (original: Room[typeof method]) => function (this: Room) {
        thirdPartyCalls(method);
        return original.call(this);
      };
      if (wrapperKind === 'shimmer') shimmer.wrap(room, method, wrap);
      else room[method] = wrap(room[method]);
    }
    const outerConnect = room.connect;
    const outerDisconnect = room.disconnect;
    for (let cycle = 0; cycle < 3; cycle++) {
      await expect(room.connect()).resolves.toBe('connected');
      await expect(room.disconnect()).resolves.toBe('disconnected');
      expect(exporter.getFinishedSpans()).toHaveLength((cycle + 1) * 2);
      expect(thirdPartyCalls).toHaveBeenCalledTimes((cycle + 1) * 2);
      if (registration === 'enable') instrumentation.enable();
      else instrumentation.instrumentRoom(room);
    }
    expect(room.connect).toBe(outerConnect);
    expect(room.disconnect).toBe(outerDisconnect);
    instrumentation.disable();
    await room.disconnect();
    expect(exporter.getFinishedSpans()).toHaveLength(6);
    expect(thirdPartyCalls).toHaveBeenCalledTimes(7);
    instrumentation.enable();
    await room.disconnect();
    expect(exporter.getFinishedSpans()).toHaveLength(7);
    expect(thirdPartyCalls).toHaveBeenCalledTimes(8);
  });
});

test('a room constructed through a retained facade while disabled starts tracing on enable', async () => {
  const client = instrumentation.manuallyInstrument(Object.freeze({ Room }));
  instrumentation.disable();
  const room = new client.Room();
  await room.disconnect();
  expect(exporter.getFinishedSpans()).toHaveLength(0);
  instrumentation.enable();
  await room.disconnect();
  expect(exporter.getFinishedSpans()).toHaveLength(1);
});

test('reactivating a retained room preserves later wrappers without duplicating spans', async () => {
  const room = instrumentation.instrumentRoom(new Room());
  const wrapped = room.disconnect;
  const thirdParty = jest.fn(() => wrapped());
  room.disconnect = thirdParty;
  instrumentation.disable();
  expect(room.disconnect).toBe(thirdParty);
  await room.disconnect();
  expect(exporter.getFinishedSpans()).toHaveLength(0);
  instrumentation.enable();
  await room.disconnect();
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(thirdParty).toHaveBeenCalledTimes(2);
});
