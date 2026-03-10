/**
 * OpenTelemetry instrumentation for LiveKit.
 *
 * LiveKit is a real-time communication platform for WebRTC.
 * This instrumentation captures room events, participant tracking,
 * audio/video track management, and agent interactions.
 */

import {
  InstrumentationBase,
  InstrumentationNodeModuleDefinition,
} from "@opentelemetry/instrumentation";
import {
  Span,
  SpanKind,
  SpanStatusCode,
  context,
  trace,
} from "@opentelemetry/api";
import { FITracer, TraceConfigOptions } from "@traceai/fi-core";
import {
  SemanticConventions,
  FISpanKind,
  MimeType,
} from "@traceai/fi-semantic-conventions";
import { VERSION } from "./version";

export interface LiveKitInstrumentationConfig {
  instrumentationConfig?: Record<string, unknown>;
  traceConfig?: TraceConfigOptions;
}

/**
 * LiveKit Instrumentation class.
 *
 * Provides automatic instrumentation for the LiveKit SDKs:
 * - livekit-client (browser/client SDK)
 * - @livekit/rtc-node (server-side SDK)
 * - livekit-server-sdk (server API SDK)
 */
export class LiveKitInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;
  private activeSpans: Map<string, Span> = new Map();

  constructor(config: LiveKitInstrumentationConfig = {}) {
    super(
      "@traceai/fi-instrumentation-livekit",
      VERSION,
      config.instrumentationConfig || {}
    );
    this._traceConfig = config.traceConfig;
  }

  override enable(): void {
    super.enable();
    this.fiTracer = new FITracer({
      tracer: this.tracer,
      traceConfig: this._traceConfig,
    });
  }

  protected init() {
    const modules = [
      new InstrumentationNodeModuleDefinition(
        "livekit-client",
        ["^1.0.0", "^2.0.0"],
        this.patchClient.bind(this),
        this.unpatchClient.bind(this)
      ),
      new InstrumentationNodeModuleDefinition(
        "@livekit/rtc-node",
        ["^0.3.0", "^0.4.0", "^0.5.0"],
        this.patchRtcNode.bind(this),
        this.unpatchRtcNode.bind(this)
      ),
    ];
    return modules;
  }

  /**
   * Manually instrument the LiveKit modules.
   */
  manuallyInstrument(livekitModule: any): void {
    if (livekitModule?.Room) {
      this.patchClient(livekitModule);
    } else if (livekitModule?.AudioSource || livekitModule?.VideoSource) {
      this.patchRtcNode(livekitModule);
    }
  }

  private patchClient(clientModule: any & { _fiPatched?: boolean }): any {
    if (clientModule?._fiPatched || _isFIPatched) {
      return clientModule;
    }

    const instrumentation = this;

    // Wrap Room.connect
    if (clientModule.Room?.prototype?.connect) {
      this._wrap(
        clientModule.Room.prototype,
        "connect",
        (original: Function) => {
          return function patchedConnect(this: any, ...args: any[]) {
            return instrumentation.traceRoomConnect(original, this, args);
          };
        }
      );
    }

    // Wrap Room.disconnect
    if (clientModule.Room?.prototype?.disconnect) {
      this._wrap(
        clientModule.Room.prototype,
        "disconnect",
        (original: Function) => {
          return function patchedDisconnect(this: any, ...args: any[]) {
            return instrumentation.traceRoomDisconnect(original, this, args);
          };
        }
      );
    }

    // Wrap LocalParticipant.publishTrack
    if (clientModule.LocalParticipant?.prototype?.publishTrack) {
      this._wrap(
        clientModule.LocalParticipant.prototype,
        "publishTrack",
        (original: Function) => {
          return function patchedPublishTrack(this: any, ...args: any[]) {
            return instrumentation.tracePublishTrack(original, this, args);
          };
        }
      );
    }

    // Wrap LocalParticipant.unpublishTrack
    if (clientModule.LocalParticipant?.prototype?.unpublishTrack) {
      this._wrap(
        clientModule.LocalParticipant.prototype,
        "unpublishTrack",
        (original: Function) => {
          return function patchedUnpublishTrack(this: any, ...args: any[]) {
            return instrumentation.traceUnpublishTrack(original, this, args);
          };
        }
      );
    }

    clientModule._fiPatched = true;
    _isFIPatched = true;
    return clientModule;
  }

  private unpatchClient(clientModule: any & { _fiPatched?: boolean }): void {
    if (clientModule?.Room?.prototype?.connect) {
      this._unwrap(clientModule.Room.prototype, "connect");
    }
    if (clientModule?.Room?.prototype?.disconnect) {
      this._unwrap(clientModule.Room.prototype, "disconnect");
    }
    if (clientModule?.LocalParticipant?.prototype?.publishTrack) {
      this._unwrap(clientModule.LocalParticipant.prototype, "publishTrack");
    }
    if (clientModule?.LocalParticipant?.prototype?.unpublishTrack) {
      this._unwrap(clientModule.LocalParticipant.prototype, "unpublishTrack");
    }
    if (clientModule) {
      clientModule._fiPatched = false;
    }
    _isFIPatched = false;
  }

  private patchRtcNode(rtcModule: any & { _fiPatched?: boolean }): any {
    if (rtcModule?._fiPatched) {
      return rtcModule;
    }

    const instrumentation = this;

    // Wrap Room.connect for server-side
    if (rtcModule.Room?.prototype?.connect) {
      this._wrap(
        rtcModule.Room.prototype,
        "connect",
        (original: Function) => {
          return function patchedConnect(this: any, ...args: any[]) {
            return instrumentation.traceRoomConnect(original, this, args);
          };
        }
      );
    }

    // Wrap AudioSource.captureFrame
    if (rtcModule.AudioSource?.prototype?.captureFrame) {
      this._wrap(
        rtcModule.AudioSource.prototype,
        "captureFrame",
        (original: Function) => {
          return function patchedCaptureFrame(this: any, ...args: any[]) {
            return instrumentation.traceAudioCapture(original, this, args);
          };
        }
      );
    }

    // Wrap VideoSource.captureFrame
    if (rtcModule.VideoSource?.prototype?.captureFrame) {
      this._wrap(
        rtcModule.VideoSource.prototype,
        "captureFrame",
        (original: Function) => {
          return function patchedCaptureFrame(this: any, ...args: any[]) {
            return instrumentation.traceVideoCapture(original, this, args);
          };
        }
      );
    }

    rtcModule._fiPatched = true;
    return rtcModule;
  }

  private unpatchRtcNode(rtcModule: any & { _fiPatched?: boolean }): void {
    if (rtcModule?.Room?.prototype?.connect) {
      this._unwrap(rtcModule.Room.prototype, "connect");
    }
    if (rtcModule?.AudioSource?.prototype?.captureFrame) {
      this._unwrap(rtcModule.AudioSource.prototype, "captureFrame");
    }
    if (rtcModule?.VideoSource?.prototype?.captureFrame) {
      this._unwrap(rtcModule.VideoSource.prototype, "captureFrame");
    }
    if (rtcModule) {
      rtcModule._fiPatched = false;
    }
  }

  private async traceRoomConnect(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const url = args[0] || "";
    const token = args[1] || "";
    const roomName = instance.name || this.extractRoomFromToken(token) || "unknown";

    const span = this.fiTracer.startSpan(`LiveKit Room Connect: ${roomName}`, {
      kind: SpanKind.CLIENT,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.CHAIN,
        "livekit.operation": "room.connect",
        "livekit.room_name": roomName,
        "livekit.server_url": this.sanitizeUrl(url),
        [SemanticConventions.INPUT_VALUE]: safeJsonStringify({ url: this.sanitizeUrl(url), roomName }),
      },
    });

    // Store span for room lifecycle tracking
    this.activeSpans.set(`room:${roomName}`, span);
    const execContext = trace.setSpan(context.active(), span);

    try {
      const result = await context.with(execContext, () => {
        return original.apply(instance, args);
      });

      span.setAttributes({
        "livekit.connected": true,
        "livekit.participant_id": instance.localParticipant?.identity || "unknown",
        [SemanticConventions.OUTPUT_VALUE]: safeJsonStringify({
          connected: true,
          participantId: instance.localParticipant?.identity,
        }),
      });

      span.setStatus({ code: SpanStatusCode.OK });
      // Don't end span yet - it will be ended on disconnect

      return result;
    } catch (error) {
      span.recordException(error as Error);
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: (error as Error).message,
      });
      span.end();
      this.activeSpans.delete(`room:${roomName}`);
      throw error;
    }
  }

  private async traceRoomDisconnect(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const roomName = instance.name || "unknown";
    const roomSpan = this.activeSpans.get(`room:${roomName}`);

    const span = this.fiTracer.startSpan(`LiveKit Room Disconnect: ${roomName}`, {
      kind: SpanKind.CLIENT,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.CHAIN,
        "livekit.operation": "room.disconnect",
        "livekit.room_name": roomName,
      },
    });

    const execContext = trace.setSpan(context.active(), span);

    try {
      const result = await context.with(execContext, () => {
        return original.apply(instance, args);
      });

      span.setAttributes({
        "livekit.disconnected": true,
        [SemanticConventions.OUTPUT_VALUE]: safeJsonStringify({ disconnected: true }),
      });

      span.setStatus({ code: SpanStatusCode.OK });
      span.end();

      // End the room connection span
      if (roomSpan) {
        roomSpan.setStatus({ code: SpanStatusCode.OK });
        roomSpan.end();
        this.activeSpans.delete(`room:${roomName}`);
      }

      return result;
    } catch (error) {
      span.recordException(error as Error);
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: (error as Error).message,
      });
      span.end();
      throw error;
    }
  }

  private async tracePublishTrack(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const track = args[0];
    const options = args[1] || {};
    const trackKind = track?.kind || "unknown";
    const trackName = options?.name || track?.name || "unnamed";

    const span = this.fiTracer.startSpan(`LiveKit Publish Track: ${trackKind}`, {
      kind: SpanKind.CLIENT,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.CHAIN,
        "livekit.operation": "track.publish",
        "livekit.track_kind": trackKind,
        "livekit.track_name": trackName,
        "livekit.participant_id": instance.identity || "unknown",
        [SemanticConventions.INPUT_VALUE]: safeJsonStringify({
          trackKind,
          trackName,
          options,
        }),
      },
    });

    const execContext = trace.setSpan(context.active(), span);

    try {
      const result = await context.with(execContext, () => {
        return original.apply(instance, args);
      });

      span.setAttributes({
        "livekit.track_sid": result?.trackSid || result?.sid || "unknown",
        "livekit.published": true,
        [SemanticConventions.OUTPUT_VALUE]: safeJsonStringify({
          trackSid: result?.trackSid || result?.sid,
          published: true,
        }),
      });

      span.setStatus({ code: SpanStatusCode.OK });
      span.end();

      return result;
    } catch (error) {
      span.recordException(error as Error);
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: (error as Error).message,
      });
      span.end();
      throw error;
    }
  }

  private async traceUnpublishTrack(
    original: Function,
    instance: any,
    args: any[]
  ): Promise<any> {
    const track = args[0];
    const trackKind = track?.kind || "unknown";
    const trackSid = track?.sid || "unknown";

    const span = this.fiTracer.startSpan(`LiveKit Unpublish Track: ${trackKind}`, {
      kind: SpanKind.CLIENT,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.CHAIN,
        "livekit.operation": "track.unpublish",
        "livekit.track_kind": trackKind,
        "livekit.track_sid": trackSid,
        "livekit.participant_id": instance.identity || "unknown",
      },
    });

    const execContext = trace.setSpan(context.active(), span);

    try {
      const result = await context.with(execContext, () => {
        return original.apply(instance, args);
      });

      span.setAttributes({
        "livekit.unpublished": true,
        [SemanticConventions.OUTPUT_VALUE]: safeJsonStringify({ unpublished: true }),
      });

      span.setStatus({ code: SpanStatusCode.OK });
      span.end();

      return result;
    } catch (error) {
      span.recordException(error as Error);
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: (error as Error).message,
      });
      span.end();
      throw error;
    }
  }

  private traceAudioCapture(
    original: Function,
    instance: any,
    args: any[]
  ): any {
    const frame = args[0];

    const span = this.fiTracer.startSpan("LiveKit Audio Capture", {
      kind: SpanKind.INTERNAL,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.CHAIN,
        "livekit.operation": "audio.capture",
        "livekit.frame_samples": frame?.samplesPerChannel || frame?.samples || 0,
        "livekit.sample_rate": frame?.sampleRate || 0,
        "livekit.channels": frame?.channels || frame?.numChannels || 0,
      },
    });

    try {
      const result = original.apply(instance, args);

      span.setStatus({ code: SpanStatusCode.OK });
      span.end();

      return result;
    } catch (error) {
      span.recordException(error as Error);
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: (error as Error).message,
      });
      span.end();
      throw error;
    }
  }

  private traceVideoCapture(
    original: Function,
    instance: any,
    args: any[]
  ): any {
    const frame = args[0];

    const span = this.fiTracer.startSpan("LiveKit Video Capture", {
      kind: SpanKind.INTERNAL,
      attributes: {
        [SemanticConventions.FI_SPAN_KIND]: FISpanKind.CHAIN,
        "livekit.operation": "video.capture",
        "livekit.frame_width": frame?.width || 0,
        "livekit.frame_height": frame?.height || 0,
        "livekit.frame_format": frame?.format || "unknown",
      },
    });

    try {
      const result = original.apply(instance, args);

      span.setStatus({ code: SpanStatusCode.OK });
      span.end();

      return result;
    } catch (error) {
      span.recordException(error as Error);
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: (error as Error).message,
      });
      span.end();
      throw error;
    }
  }

  private sanitizeUrl(url: string): string {
    try {
      const parsed = new URL(url);
      return `${parsed.protocol}//${parsed.host}`;
    } catch {
      return "unknown";
    }
  }

  private extractRoomFromToken(token: string): string | null {
    // JWT tokens have 3 parts separated by dots
    // The payload is the second part, base64 encoded
    try {
      const parts = token.split(".");
      if (parts.length !== 3) return null;
      const payload = JSON.parse(atob(parts[1]));
      return payload.video?.room || payload.room || null;
    } catch {
      return null;
    }
  }
}

// Global patched flag
let _isFIPatched = false;

/**
 * Check if the module has been patched.
 */
export function isPatched(): boolean {
  return _isFIPatched;
}

/**
 * Reset the patched state (for testing only).
 */
export function _resetPatchedStateForTesting(): void {
  _isFIPatched = false;
}

// Helper functions

function safeJsonStringify(obj: unknown): string {
  try {
    return JSON.stringify(obj);
  } catch {
    return String(obj);
  }
}
