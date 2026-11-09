import { context, trace, SpanStatusCode } from '@opentelemetry/api';
import { AsyncLocalStorageContextManager } from '@opentelemetry/context-async-hooks';
import { InMemorySpanExporter, SimpleSpanProcessor } from '@opentelemetry/sdk-trace-base';
import { NodeTracerProvider } from '@opentelemetry/sdk-trace-node';
import { LiveKitInstrumentation, isPatched } from '../instrumentation';

const exporter = new InMemorySpanExporter();
const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
const manager = new AsyncLocalStorageContextManager().enable();
context.setGlobalContextManager(manager);
const instances: LiveKitInstrumentation[] = [];
function instrument(module: any, config: ConstructorParameters<typeof LiveKitInstrumentation>[0] = {}) {
  const inst = new LiveKitInstrumentation(config);
  inst.setTracerProvider(provider);
  instances.push(inst);
  inst.manuallyInstrument(module);
  return inst;
}
function client() {
  class Room {
    name = '';
    localParticipant = { identity: 'participant' };
    async connect(_url: string, _token: string) { this.name = 'assigned-room'; return 'connected'; }
    async disconnect() { return 'disconnected'; }
  }
  class LocalParticipant {
    identity = 'participant';
    publishTrack = undefined;
    unpublishTrack(_track: any) { return { sid: 'track-1' }; }
  }
  return { Room, LocalParticipant };
}
afterEach(() => { instances.splice(0).forEach(inst => inst.disable()); exporter.reset(); });
afterAll(async () => { await provider.shutdown(); context.disable(); manager.disable(); });

test('connect completes and exports without requiring disconnect, with the resolved room name', async () => {
  const sdk = client();
  instrument(sdk);
  const room = new sdk.Room();
  expect(await room.connect('wss://user:secret@example.test/path?token=secret', 'synthetic-token')).toBe('connected');
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(exporter.getFinishedSpans()[0].attributes['livekit.room_name']).toBe('assigned-room');
  expect(JSON.stringify(exporter.getFinishedSpans()[0].attributes)).not.toContain('secret');
});

test('unconnected RTC room metadata getters cannot prevent the original operation', async () => {
  class Room {
    get name(): string { throw new TypeError('room info is not initialized'); }
    get localParticipant(): any { throw new TypeError('participant is not initialized'); }
    async connect() { return 'connected'; }
    async disconnect() { return 'disconnected'; }
  }
  instrument({ Room });
  const room = new Room();
  await expect(room.connect()).resolves.toBe('connected');
  await expect(room.disconnect()).resolves.toBe('disconnected');
  expect(exporter.getFinishedSpans()).toHaveLength(2);
});

test('manual RTC module with Room also instruments audio and waits for rejection', async () => {
  let reject!: (error: Error) => void;
  class AudioSource { captureFrame(_frame: any) { return new Promise<void>((_, r) => { reject = r; }); } }
  const sdk = { ...client(), AudioSource };
  instrument(sdk);
  const pending = new sdk.AudioSource().captureFrame({ sampleRate: 48000, samplesPerChannel: 480, channels: 1 });
  expect(exporter.getFinishedSpans()).toHaveLength(0);
  const error = new Error('capture failed');
  reject(error);
  await expect(pending).rejects.toBe(error);
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
});

test('client instance-field methods work through a frozen-module facade and preserve subclasses', async () => {
  class Room {
    name = 'room';
    connect = async () => 'connected';
    disconnect = async () => 'disconnected';
  }
  const sdk = Object.freeze({ Room });
  const inst = instrument(sdk);
  const wrapped = inst.manuallyInstrument(sdk);
  expect(inst.manuallyInstrument(sdk)).toBe(wrapped);
  class CustomRoom extends wrapped.Room {}
  const room = new CustomRoom();
  expect(room).toBeInstanceOf(Room);
  expect(room).toBeInstanceOf(CustomRoom);
  await room.connect();
  await room.disconnect();
  expect(exporter.getFinishedSpans()).toHaveLength(2);
  inst.disable();
  await room.disconnect();
  await new wrapped.Room().disconnect();
  expect(exporter.getFinishedSpans()).toHaveLength(2);
});

test('instrumentRoom supports pre-existing instances without replacing the constructor', async () => {
  class Room { disconnect = async () => 'done'; }
  const room = new Room();
  const original = room.disconnect;
  const inst = instrument(null);
  expect(inst.instrumentRoom(room)).toBe(room);
  inst.instrumentRoom(room);
  await room.disconnect();
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  inst.disable();
  expect(room.disconnect).toBe(original);
});

test('sync client unpublish keeps its return type and synchronous exceptions', () => {
  const sdk = client();
  const expected = { sid: 'publication' };
  sdk.LocalParticipant.prototype.unpublishTrack = () => expected;
  instrument(sdk);
  expect(new sdk.LocalParticipant().unpublishTrack({ sid: 'track-1' })).toBe(expected);
  expect(exporter.getFinishedSpans()).toHaveLength(1);
});

test('disable restores manually wrapped methods and re-instrumentation does not duplicate spans', async () => {
  const sdk = client();
  const original = sdk.Room.prototype.disconnect;
  const inst = instrument(sdk);
  inst.manuallyInstrument(sdk);
  await new sdk.Room().disconnect();
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  inst.disable();
  expect(sdk.Room.prototype.disconnect).toBe(original);
  inst.enable();
  inst.manuallyInstrument(sdk);
  await new sdk.Room().disconnect();
  expect(exporter.getFinishedSpans()).toHaveLength(2);
});

test('multiple module copies and frozen ESM namespaces can be instrumented independently', async () => {
  const first = Object.freeze(client());
  const second = Object.freeze(client());
  const inst = instrument(first);
  inst.manuallyInstrument(second);
  await new first.Room().disconnect();
  await new second.Room().disconnect();
  expect(exporter.getFinishedSpans()).toHaveLength(2);
  expect(isPatched()).toBe(true);
});

test('constructor privacy config applies without calling enable again and provider updates are honored', async () => {
  const sdk = client();
  instrument(sdk, { traceConfig: { hideInputs: true, hideOutputs: true } });
  await new sdk.Room().connect('wss://example.test', 'synthetic-token');
  const spans = exporter.getFinishedSpans();
  expect(spans).toHaveLength(1);
  expect(spans[0].attributes['input.value']).toBe('__REDACTED__');
  expect(spans[0].attributes['output.value']).toBe('__REDACTED__');
});

test('capture spans propagate context and preserve synchronous video returns', () => {
  class VideoSource { captureFrame(_frame: any) { return trace.getActiveSpan()?.spanContext().spanId; } }
  instrument({ VideoSource });
  const result = new VideoSource().captureFrame({ width: 100, height: 200 });
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(result).toBe(exporter.getFinishedSpans()[0].spanContext().spanId);
});
