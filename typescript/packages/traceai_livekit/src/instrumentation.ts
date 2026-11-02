/// <reference lib="es2021.weakref" />
/** OpenTelemetry instrumentation for LiveKit client and Node RTC operations. */
import {
  InstrumentationBase,
  InstrumentationNodeModuleDefinition,
} from "@opentelemetry/instrumentation";
import { Attributes, Span, SpanKind, SpanStatusCode, context, trace } from "@opentelemetry/api";
import { FITracer, TraceConfigOptions } from "@traceai/fi-core";
import { SemanticConventions, FISpanKind } from "@traceai/fi-semantic-conventions";
import { VERSION } from "./version";

export interface LiveKitInstrumentationConfig {
  instrumentationConfig?: Record<string, unknown>;
  traceConfig?: TraceConfigOptions;
}

type Operation = "room.connect" | "room.disconnect" | "track.publish" | "track.unpublish" | "audio.capture" | "video.capture";
type Method = (...args: any[]) => any;
interface Patch {
  target: WeakRef<Record<string, Method>>;
  name: string;
  wrapped: WeakRef<Method>;
  deactivate: () => void;
}
// Ownership belongs to the actual method, not the module namespace (which can be frozen).
const owners = new WeakMap<Method, LiveKitInstrumentation>();
let patchCount = 0;

/** Instruments RTC operations, not LiveKit Agents' native LLM/session spans. */
export class LiveKitInstrumentation extends InstrumentationBase {
  private readonly traceConfig?: TraceConfigOptions;
  private readonly patches = new Set<Patch>();
  // Do not retain disconnected Room instances through their bound methods.
  private readonly collected = new FinalizationRegistry<Patch>(patch => this.removePatch(patch));
  private readonly clientModules = new WeakMap<object, any>();
  private readonly knownRooms = new WeakSet<object>();
  private readonly rooms = new Set<WeakRef<object>>();
  private readonly collectedRooms = new FinalizationRegistry<WeakRef<object>>(room => this.rooms.delete(room));

  constructor(config: LiveKitInstrumentationConfig = {}) {
    // InstrumentationBase may call enable() from its constructor. Initialize our state first.
    super("@traceai/fi-instrumentation-livekit", VERSION, {
      ...config.instrumentationConfig,
      enabled: false,
    });
    this.traceConfig = config.traceConfig;
    this.setConfig({ ...config.instrumentationConfig, enabled: config.instrumentationConfig?.enabled !== false });
    if (this.getConfig().enabled) this.enable();
  }

  protected init() {
    return [
      new InstrumentationNodeModuleDefinition(
        "livekit-client", [">=1.0.0 <3"],
        this.patchClient.bind(this), this.unpatchModule.bind(this)
      ),
      new InstrumentationNodeModuleDefinition(
        "@livekit/rtc-node", [">=0.3.0 <2"],
        this.patchRtcNode.bind(this), this.unpatchModule.bind(this)
      ),
    ];
  }

  /** Use the returned module for client Room constructors, including frozen ESM namespaces. */
  manuallyInstrument<T>(module: T): T {
    if (!this.isEnabled()) return module;
    const sdk = module as any;
    if (sdk?.AudioSource || sdk?.VideoSource) return this.patchRtcNode(sdk);
    if (sdk?.Room) return this.patchClient(sdk);
    return module;
  }

  /** Instrument a client Room created before setup, or constructed from an existing ESM binding. */
  instrumentRoom<T extends object>(room: T): T {
    // Keep only weak references across disable(): applications may retain rooms
    // (or construct them through a saved facade) while tracing is turned off.
    if (!this.knownRooms.has(room)) {
      this.knownRooms.add(room);
      const reference = new WeakRef(room);
      this.rooms.add(reference);
      this.collectedRooms.register(room, reference);
    }
    if (this.isEnabled()) {
      this.wrap(room as Record<string, Method>, "connect", "room.connect");
      this.wrap(room as Record<string, Method>, "disconnect", "room.disconnect");
    }
    return room;
  }

  override enable(): void {
    super.enable();
    for (const reference of this.rooms) {
      const room = reference.deref();
      if (room) this.instrumentRoom(room);
      else this.rooms.delete(reference);
    }
  }

  override disable(): void {
    super.disable();
    // The base class only knows modules seen by its loader, not manual registrations.
    for (const patch of this.patches) this.removePatch(patch);
  }

  private wrap(target: Record<string, Method> | undefined, name: string, operation: Operation): void {
    if (!target || typeof target[name] !== "function" || owners.has(target[name])) return;
    const instrumentation = this;
    let active = true;
    this._wrap(target, name, (original: Method) => function (this: any, ...args: any[]) {
      // A later third-party wrapper may still retain this function after disable().
      if (!active || !instrumentation.isEnabled()) return original.apply(this, args);
      return instrumentation.traceOperation(operation, original, this, args);
    });
    const wrapped = target[name];
    owners.set(wrapped, this);
    const patch = { target: new WeakRef(target), name, wrapped: new WeakRef(wrapped), deactivate: () => { active = false; } };
    this.patches.add(patch);
    this.collected.register(target, patch, patch);
    patchCount++;
  }

  private removePatch(patch: Patch): void {
    if (!this.patches.delete(patch)) return;
    patch.deactivate();
    const target = patch.target.deref();
    const wrapped = patch.wrapped.deref();
    if (target && target[patch.name] === wrapped) this._unwrap(target, patch.name);
    if (wrapped) owners.delete(wrapped);
    this.collected.unregister(patch);
    patchCount--;
  }

  private patchClient(module: any): any {
    this.wrap(module?.Room?.prototype, "connect", "room.connect");
    this.wrap(module?.Room?.prototype, "disconnect", "room.disconnect");
    this.wrap(module?.LocalParticipant?.prototype, "publishTrack", "track.publish");
    this.wrap(module?.LocalParticipant?.prototype, "unpublishTrack", "track.unpublish");
    // livekit-client implements room operations as instance fields, not prototype methods.
    // Return a facade rather than assigning into a possibly immutable ESM namespace.
    if (typeof module?.Room !== "function" ||
        (module.Room.prototype.connect && module.Room.prototype.disconnect)) return module;
    const existing = this.clientModules.get(module);
    if (existing) return existing;
    const instrumentation = this;
    const Room = new Proxy(module.Room, {
      construct(target, args, newTarget) {
        const room = Reflect.construct(target, args, newTarget);
        return instrumentation.instrumentRoom(room);
      },
    });
    const facade = { ...module, Room };
    this.clientModules.set(module, facade);
    return facade;
  }

  private patchRtcNode(module: any): any {
    this.patchClient(module);
    this.wrap(module?.AudioSource?.prototype, "captureFrame", "audio.capture");
    this.wrap(module?.VideoSource?.prototype, "captureFrame", "video.capture");
    return module;
  }

  private unpatchModule(module: any): void {
    const targets = [module?.Room?.prototype, module?.LocalParticipant?.prototype,
      module?.AudioSource?.prototype, module?.VideoSource?.prototype];
    for (const patch of this.patches) {
      if (targets.includes(patch.target.deref())) this.removePatch(patch);
    }
  }

  private traceOperation(operation: Operation, original: Method, instance: any, args: any[]): any {
    const attributes: Attributes = {
      [SemanticConventions.FI_SPAN_KIND]: FISpanKind.CHAIN,
      "livekit.operation": operation,
    };
    let name: string;
    const track = args[0];
    switch (operation) {
      case "room.connect":
      case "room.disconnect": {
        const roomName = safeRead(() => instance.name) || "unknown";
        name = `LiveKit Room ${operation === "room.connect" ? "Connect" : "Disconnect"}: ${roomName}`;
        attributes["livekit.room_name"] = roomName;
        if (operation === "room.connect") {
          const url = sanitizeUrl(args[0]);
          attributes["livekit.server_url"] = url;
          attributes[SemanticConventions.INPUT_VALUE] = safeJsonStringify({ url, roomName });
        }
        break;
      }
      case "track.publish":
      case "track.unpublish": {
        const trackKind = track?.kind ?? "unknown";
        name = `LiveKit ${operation === "track.publish" ? "Publish" : "Unpublish"} Track: ${trackKind}`;
        attributes["livekit.track_kind"] = trackKind;
        attributes["livekit.participant_id"] = instance.identity || "unknown";
        if (operation === "track.publish") {
          const options = args[1] || {};
          const trackName = options.name || track?.name || "unnamed";
          attributes["livekit.track_name"] = trackName;
          attributes[SemanticConventions.INPUT_VALUE] = safeJsonStringify({ trackKind, trackName, options });
        } else {
          // rtc-node takes a SID; livekit-client takes a Track.
          attributes["livekit.track_sid"] = typeof track === "string" ? track : track?.sid || "unknown";
        }
        break;
      }
      case "audio.capture":
        name = "LiveKit Audio Capture";
        attributes["livekit.frame_samples"] = track?.samplesPerChannel ?? track?.samples ?? 0;
        attributes["livekit.sample_rate"] = track?.sampleRate ?? 0;
        attributes["livekit.channels"] = track?.channels ?? track?.numChannels ?? 0;
        break;
      case "video.capture":
        name = "LiveKit Video Capture";
        attributes["livekit.frame_width"] = track?.width ?? 0;
        attributes["livekit.frame_height"] = track?.height ?? 0;
        attributes["livekit.frame_format"] = track?.type ?? track?.format ?? "unknown";
        break;
    }
    // Read the current tracer after setTracerProvider(), and use the constructor's privacy config.
    const span = new FITracer({ tracer: this.tracer, traceConfig: this.traceConfig }).startSpan(name, {
      kind: operation.endsWith(".capture") ? SpanKind.INTERNAL : SpanKind.CLIENT,
      attributes,
    });
    const success = (result: any) => {
      this.setResult(span, operation, instance, result);
      span.setStatus({ code: SpanStatusCode.OK });
      span.end();
      return result;
    };
    const failure = (error: unknown): never => {
      span.recordException(error instanceof Error ? error : String(error));
      span.setStatus({ code: SpanStatusCode.ERROR, message: error instanceof Error ? error.message : String(error) });
      span.end();
      throw error;
    };
    try {
      const result = context.with(trace.setSpan(context.active(), span), () => original.apply(instance, args));
      // Do not turn synchronous client methods into promises, or end async audio spans early.
      return result && typeof result.then === "function"
        ? Promise.resolve(result).then(success, failure)
        : success(result);
    } catch (error) {
      return failure(error);
    }
  }

  private setResult(span: Span, operation: Operation, instance: any, result: any): void {
    switch (operation) {
      case "room.connect": {
        const participantId = safeRead(() => instance.localParticipant?.identity);
        span.setAttributes({
          "livekit.connected": true,
          "livekit.room_name": safeRead(() => instance.name) || "unknown",
          "livekit.participant_id": participantId || "unknown",
          [SemanticConventions.OUTPUT_VALUE]: safeJsonStringify({ connected: true, participantId }),
        });
        // This is a connect operation, not a session root. End when connect settles.
        break;
      }
      case "room.disconnect":
        span.setAttributes({ "livekit.disconnected": true, [SemanticConventions.OUTPUT_VALUE]: safeJsonStringify({ disconnected: true }) });
        break;
      case "track.publish":
        span.setAttributes({
          "livekit.published": true,
          "livekit.track_sid": result?.trackSid || result?.sid || "unknown",
          [SemanticConventions.OUTPUT_VALUE]: safeJsonStringify({ published: true, trackSid: result?.trackSid || result?.sid }),
        });
        break;
      case "track.unpublish":
        span.setAttributes({ "livekit.unpublished": true, [SemanticConventions.OUTPUT_VALUE]: safeJsonStringify({ unpublished: true }) });
        break;
    }
  }
}

/** Whether this package currently owns any patched LiveKit methods. */
export function isPatched(): boolean { return patchCount > 0; }

// Older rtc-node getters throw until the room has connected. Metadata is best-effort.
function safeRead<T>(read: () => T): T | undefined {
  try { return read(); } catch { return undefined; }
}

function sanitizeUrl(url: string): string {
  try {
    const parsed = new URL(url);
    return `${parsed.protocol}//${parsed.host}`;
  } catch { return "unknown"; }
}

function safeJsonStringify(value: unknown): string {
  try { return JSON.stringify(value); } catch { return "[unserializable]"; }
}
