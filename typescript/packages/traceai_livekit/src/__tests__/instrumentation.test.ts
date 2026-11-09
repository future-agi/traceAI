import { SpanStatusCode } from '@opentelemetry/api';
import { InMemorySpanExporter, SimpleSpanProcessor } from '@opentelemetry/sdk-trace-base';
import { NodeTracerProvider } from '@opentelemetry/sdk-trace-node';
import { LiveKitInstrumentation, isPatched } from '../instrumentation';
import { VERSION } from '../version';

const exporter = new InMemorySpanExporter();
const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
const instances: LiveKitInstrumentation[] = [];
function create(config: ConstructorParameters<typeof LiveKitInstrumentation>[0] = {}) {
  const inst = new LiveKitInstrumentation(config);
  inst.setTracerProvider(provider);
  instances.push(inst);
  return inst;
}
afterEach(() => {
  instances.splice(0).forEach(inst => inst.disable());
  expect(isPatched()).toBe(false);
  exporter.reset();
});
afterAll(() => provider.shutdown());

test('metadata matches the release manifest package version', () => {
  const inst = create();
  expect(VERSION).toBe(require('../../package.json').version);
  expect(inst.instrumentationVersion).toBe(VERSION);
  expect(inst.instrumentationName).toBe('@traceai/fi-instrumentation-livekit');
});

test('empty modules and disabled manual instrumentation do not patch', () => {
  const inst = create({ instrumentationConfig: { enabled: false } });
  class Room { disconnect() {} }
  const original = Room.prototype.disconnect;
  inst.manuallyInstrument(null);
  inst.manuallyInstrument({ Room });
  expect(Room.prototype.disconnect).toBe(original);
  expect(isPatched()).toBe(false);
  inst.enable();
  inst.manuallyInstrument({ Room });
  expect(Room.prototype.disconnect).not.toBe(original);
});

test('second instrumentation cannot duplicate spans or unwrap the first owner', async () => {
  class Room { async disconnect() {} }
  const first = create();
  const second = create();
  first.manuallyInstrument({ Room });
  second.manuallyInstrument({ Room });
  second.disable();
  await new Room().disconnect();
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(isPatched()).toBe(true);
});

test('later wrappers survive disable and retained old wrappers stay inert after re-enable', () => {
  class Room { disconnect() { return 'result'; } }
  const inst = create();
  inst.manuallyInstrument({ Room });
  const instrumented = Room.prototype.disconnect;
  const thirdParty = function (this: Room) { return instrumented.call(this); };
  Room.prototype.disconnect = thirdParty;
  inst.disable();
  expect(Room.prototype.disconnect).toBe(thirdParty);
  expect(new Room().disconnect()).toBe('result');
  expect(exporter.getFinishedSpans()).toHaveLength(0);
  inst.enable();
  inst.manuallyInstrument({ Room });
  new Room().disconnect();
  expect(exporter.getFinishedSpans()).toHaveLength(1);
});

test('sync throws preserve identity and redact error details using constructor config', () => {
  const error = new Error('private audio payload');
  class VideoSource { captureFrame(_frame: any) { throw error; } }
  const inst = create({ traceConfig: { hideErrorDetails: true } });
  inst.manuallyInstrument({ VideoSource });
  expect(() => new VideoSource().captureFrame({ width: 16, height: 16 })).toThrow(error);
  const [span] = exporter.getFinishedSpans();
  expect(span.status.code).toBe(SpanStatusCode.ERROR);
  expect(JSON.stringify([span.events, span.status])).not.toContain('private audio payload');
});

test('RTC publish and SID-based unpublish are both traced', async () => {
  class Room {}
  class AudioSource {}
  class LocalParticipant {
    identity = 'participant';
    async publishTrack(_track: any, _options: any) { return { sid: 'track-1' }; }
    async unpublishTrack(_sid: string) {}
  }
  const inst = create();
  inst.manuallyInstrument({ Room, AudioSource, LocalParticipant });
  const participant = new LocalParticipant();
  await expect(participant.publishTrack({ kind: 0, name: 'mic' }, {})).resolves.toEqual({ sid: 'track-1' });
  await participant.unpublishTrack('track-1');
  const spans = exporter.getFinishedSpans();
  expect(spans).toHaveLength(2);
  expect(spans[0].attributes['livekit.track_kind']).toBe(0);
  expect(spans[1].attributes['livekit.track_sid']).toBe('track-1');
});

test('concurrent rooms with equal names and repeated connects export each operation once', async () => {
  class Room { name = 'same'; async connect() {} async disconnect() {} }
  const inst = create();
  inst.manuallyInstrument({ Room });
  const rooms = [new Room(), new Room()];
  await Promise.all(rooms.map(room => room.connect()));
  await rooms[0].connect();
  await rooms[0].disconnect();
  expect(exporter.getFinishedSpans()).toHaveLength(4);
});

test('async rejected and successful captures finish with the original result', async () => {
  const failure = { message: 'synthetic failure' };
  class AudioSource { async captureFrame(frame: any) { if (!frame) throw failure; return frame; } }
  const inst = create();
  inst.manuallyInstrument({ AudioSource });
  const source = new AudioSource();
  const frame = { samplesPerChannel: 480, sampleRate: 48000, channels: 1 };
  await expect(source.captureFrame(null)).rejects.toBe(failure);
  await expect(source.captureFrame(frame)).resolves.toBe(frame);
  expect(exporter.getFinishedSpans().map(span => span.status.code)).toEqual([SpanStatusCode.ERROR, SpanStatusCode.OK]);
});
