import { build, checkRequest, header, isBinary, match, RequestData, ResponseData, retainBodySecrets, SpanResult } from "./core";
import { encodeBytes, byteLength, concatBytes } from "./bytes";
import { BodyDecoder, decodeResponse, responseEncoding } from "./encoding";
import { streamingMilestones } from "./stream-timing";
import type { Span } from "@opentelemetry/api";
export interface FetchMediaObserver {
  bind(span: Span): void;
  request(body: BodyInit | null | undefined): void;
  materialized(value: unknown, method: "arrayBuffer" | "blob" | "json", mimeType: string, successful: boolean): void;
}
export interface FetchContext<T> {
  media?(parent: T, method: string, url: string): FetchMediaObserver | undefined;
  active(): T; skip(parent: T): boolean; run<R>(parent: T, fn: () => R): R;
  observe?<R>(promise: Promise<R>, ok: (value: R) => void, fail: (error: unknown) => void): void;
  observeRequest?(request: RequestData): void;
  /** Eligibility only; ownership commits when build produces an actual observation. */
  capture?(parent: T): boolean;
}
function headersOf(headers?: HeadersInit): Record<string, string> {
  return Object.fromEntries(new Headers(headers).entries());
}
function* textFields(fields: Iterable<[string, unknown]>): Iterable<unknown> {
  for (const [name, value] of fields) if (typeof value === "string") yield { [name]: value };
}
function requestData(input: RequestInfo | URL, init: RequestInit | undefined, max: number): RequestData | undefined {
  const request = input instanceof Request ? input : undefined;
  const req: RequestData = {
    method: init?.method ?? request?.method ?? "GET", url: request?.url ?? String(input),
    headers: headersOf(init?.headers ?? request?.headers),
  };
  const body = init?.body;
  if (body instanceof Blob && body.type && !req.headers["content-type"]) req.headers["content-type"] = body.type;
  if (body instanceof FormData) {
    req.fields = {}; req.files = [];
    // Discover uncropped and duplicate text fields lazily; never inspect file bytes.
    retainBodySecrets(req, textFields(body.entries()));
    for (const [name, value] of body.entries()) {
      // Text fields have their own 64 KiB cap, independent of file/body size.
      if (typeof value === "string") req.fields[name] = new TextDecoder().decode(encodeBytes(value.slice(0, 65536)).subarray(0, 65536));
      else req.files.push({ name, filename: value.name, content_type: value.type, size: value.size });
    }
  } else if (header(req.headers, "content-type").startsWith("multipart/")) {
    // Raw multipart streams may contain arbitrarily large files; inspect FormData only.
    req.unavailable = true;
  } else if (isBinary(header(req.headers, "content-type"))) {
    req.bodySize = body instanceof Blob ? body.size : typeof body === "string" ? byteLength(body) : ArrayBuffer.isView(body) ? body.byteLength : body instanceof ArrayBuffer ? body.byteLength : req.headers["content-length"] ? Number(req.headers["content-length"]) : undefined;
  } else if (typeof body === "string" || body instanceof URLSearchParams || ArrayBuffer.isView(body) || body instanceof ArrayBuffer) {
    // Do not copy a potentially huge input merely to discover that it exceeds the cap.
    if (body instanceof URLSearchParams) retainBodySecrets(req, textFields(body.entries()));
    const materialized = typeof body === "string" ? body : body instanceof URLSearchParams ? body.toString() : body instanceof ArrayBuffer ? new Uint8Array(body) : new Uint8Array(body.buffer, body.byteOffset, body.byteLength);
    const size = typeof materialized === "string" ? byteLength(materialized) : materialized.byteLength;
    if (size > max) { req.truncated = true; retainBodySecrets(req, body instanceof URLSearchParams ? textFields(body.entries()) : materialized); }
    else req.body = typeof materialized === "string" ? encodeBytes(materialized) : materialized;
  } else if (body || request?.body) {
    // Never clone/tee request streams: even a concurrent tee can queue unbounded data.
    req.unavailable = true;
  }
  return req;
}
/** A streaming base64 length scanner: retained state is bounded, even for huge audio. */
class EncodedAudioCounter {
  private tail = "";
  private inValue = false;
  private chars = 0;
  private total = 0;
  private decoder = new TextDecoder();
  add(chunk: Uint8Array): void {
    const text = this.decoder.decode(chunk, { stream: true });
    for (const c of text) {
      if (this.inValue) {
        if (c === '"') { this.total += Math.floor(this.chars * 3 / 4); this.chars = 0; this.inValue = false; this.tail = ""; }
        else if (/[A-Za-z0-9+/]/.test(c)) this.chars++;
      } else {
        this.tail = (this.tail + c).slice(-96);
        if (/"(?:audio_base64|data)"\s*:\s*"$/.test(this.tail)) { this.inValue = true; this.tail = ""; }
      }
    }
  }
  get bytes(): number { return this.total + Math.floor(this.chars * 3 / 4); }
}
/** Tap settlements without replacing the application's promise, result or rejection. */
function tap<T>(promise: Promise<T>, ok: (value: T) => void, fail: (error: unknown) => void): void {
  try { void promise.then(value => { try { ok(value); } catch { /* Observation only. */ } }, error => { try { fail(error); } catch { /* Observation only. */ } }); }
  catch { /* Nonstandard promises are passed through. */ }
}
/** Decorate methods in place; never substitute a Response, stream, reader or iterator. */
function observeMethod(object: any, name: PropertyKey, after: (result: any, args: any[], receiver: any) => void): void {
  try {
    const method = object[name];
    if (typeof method !== "function") return;
    Object.defineProperty(object, name, { configurable: true, writable: true, value: new Proxy(method, {
      apply(target, receiver, args) {
        const result = Reflect.apply(target, receiver, args); // Host errors escape unchanged.
        try { if (receiver === object) after(result, args, receiver); } catch { /* Never replace a successful host result. */ }
        return result;
      },
    }) });
  } catch { /* Frozen/custom runtime objects stay untouched. */ }
}
export interface FetchHook { remove(): void; pending: Set<Promise<void>> }
export function createFetchHook<T>(max: number, emit: (result: SpanResult, parent: T, start: number, end: number, bindMedia?: (span: Span) => void) => void, scope: FetchContext<T>, secrets: string[] = [], decodeBody?: BodyDecoder): FetchHook {
  const original = globalThis.fetch;
  const observe = scope.observe ?? tap;
  const pending = new Set<Promise<void>>();
  let active = true;
  const patched = new Proxy(original, {
    apply(target, receiver, argArray: [RequestInfo | URL, RequestInit?]) {
      if (!active) return Reflect.apply(target, receiver, argArray);
      const observeRequest = (req: RequestData | undefined) => { try { if (req) scope.observeRequest?.(req); } catch { /* The export boundary fails closed on registry errors. */ } };
      const head = () => {
        const [input, init] = argArray, request = input instanceof Request ? input : undefined;
        return { method: init?.method ?? request?.method ?? "GET", url: request?.url ?? String(input), headers: headersOf(init?.headers ?? request?.headers) };
      };
      const passThrough = () => {
        try { return Reflect.apply(target, receiver, argArray); }
        finally { try { if (scope.observeRequest) observeRequest(head()); } catch { /* Never read bodies on suppressed calls. */ } }
      };
      let parent: T;
      try { parent = scope.active(); } catch { return passThrough(); }
      let skip = true;
      try { skip = scope.skip(parent); } catch { /* Broken context still observes headers/URL. */ }
      if (skip) return passThrough();
      const capture = () => { try { return scope.capture?.(parent) !== false; } catch { return false; } };
      let media: FetchMediaObserver | undefined;
      let req: RequestData | undefined, found: ReturnType<typeof match>, initial: ReturnType<typeof checkRequest>;
      const inspect = () => {
        const [input, init] = argArray, info = head();
        observeRequest(info); // Every route, not just catalog-matched AI calls.
        found = match(info.method, info.url);
        try { media = scope.media?.(parent, info.method, info.url); media?.request(init?.body); } catch { /* Independent, fail-open media admission. */ }
        // Covered model operations observe URL/headers only, as before. Selection
        // is delayed until catalog match; unsupported calls cannot choose a route.
        if (found && capture()) {
          req = requestData(input, init, max);
          if (req) { req.secrets = secrets; observeRequest(req); initial = checkRequest(found, req); }
        }
      };
      // The timing boundary is the handoff, before even URL/body validation.
      let result!: Promise<Response>, start = 0, clock = 0, invoked = false, failed = false, hostError: unknown, errorAt = 0;
      try {
        scope.run(parent, () => {
          invoked = true; start = Date.now(); clock = performance.now();
          try { return result = Reflect.apply(target, receiver, argArray); }
          catch (error) { failed = true; hostError = error; errorAt = Date.now(); throw error; }
        });
      } catch { if (!invoked) return Reflect.apply(target, receiver, argArray); }
      if (!invoked) return Reflect.apply(target, receiver, argArray);
      if (failed) {
        try {
          inspect();
          const span = found && req && initial && build(found, req, { headers: {}, size: 0, error: hostError instanceof Error ? hostError : new Error("Transport failure") }, initial);
          if (span) emit(span, parent, start, errorAt);
        } catch { /* Preserve the host error identity. */ }
        throw hostError;
      }
      try { inspect(); } catch { return result; }
      if (media) {
        const observer = media, observed = new WeakSet<Response>();
        const decorate = (response: Response) => {
          if (observed.has(response)) return;
          observed.add(response);
          const mimeType = header(headersOf(response.headers), "content-type");
          for (const method of ["arrayBuffer", "blob", "json"] as const) {
            observeMethod(response, method, promise => observe(promise, value => {
              if (active) observer.materialized(value, method, mimeType, response.ok);
            }, () => {}));
          }
          // Only follow clones explicitly created by the application. Never call clone ourselves.
          observeMethod(response, "clone", clone => decorate(clone));
        };
        observe(result, decorate, () => {});
      }
      if (!found || !req || !initial) return result;
      const matched = found, request = req, parsed = initial;
      let resolve!: () => void;
      const done = new Promise<void>(r => { resolve = r; });
      let finished = false;
      const finish = (response: ResponseData, end: number, audio?: EncodedAudioCounter) => {
        if (finished) return; finished = true;
        const complete = () => {
          if (audio) { response.audioOutputBytes = audio.bytes; response.body = undefined; }
          try { const span = build(matched, request, response, parsed); if (span) emit(span, parent, start, end, media ? value => media!.bind(value) : undefined); } catch { /* Capture must not reach application code. */ }
          finally { resolve(); pending.delete(done); }
        };
        // Only completed responses awaiting bounded decoding are flushable, never unread streams.
        // Compressed JSON/SSE audio must be decoded before its base64 length scanner.
        if (responseEncoding(response)) {
          // Compressed byte offsets cannot be relabelled as decoded event offsets.
          response.arrivalTimes = undefined; response.timingIncomplete = true;
          pending.add(done); void decodeResponse(response, max, decodeBody, audio ? chunk => audio.add(chunk) : undefined).then(complete, complete);
        } else { if (audio && response.body) audio.add(response.body); complete(); }
      };
      observe(result, (response: Response) => {
        const data: ResponseData = { headers: {}, size: 0 };
        try {
          data.status = response.status; data.headers = headersOf(response.headers);
          data.alreadyDecoded = response.type === "basic" || response.type === "cors";
          if (!response.body) { finish(data, Date.now()); return; }
          const binary = isBinary(header(data.headers, "content-type"));
          const encoded = matched.endpoint.operation === "text_to_speech" && response.status < 400 && !binary ? new EncodedAudioCounter() : undefined;
          const deferredAudio = encoded && !data.alreadyDecoded && header(data.headers, "content-encoding") && header(data.headers, "content-encoding") !== "identity";
          const chunks: Uint8Array[] = [], timingChunks: Uint8Array[] = [];
          let buffered = 0, timingBytes = 0, lastByteAt: number | undefined;
          data.arrivalTimes = [];
          const length = data.headers["content-length"];
          const expected = length && /^\d+$/.test(length) && !(data.alreadyDecoded && header(data.headers, "content-encoding")) ? Number(length) : undefined;
          const finalize = (end: number) => {
            if (finished) return;
            if (data.unavailable) { finish(data, end); return; }
            if (encoded && !deferredAudio) {
              data.audioOutputBytes = encoded.bytes;
              const timing = streamingMilestones(concatBytes(timingChunks, timingBytes), true, data.arrivalTimes ?? []);
              data.firstTextSeconds = timing.firstText; data.eventArrivalSeconds = timing.events;
              if (timing.incomplete) data.timingIncomplete = true;
            }
            else if (!binary && !data.truncated) data.body = concatBytes(chunks, buffered);
            finish(data, end, deferredAudio ? encoded : undefined);
          };
          const observed = new WeakSet<object>(), responses = new WeakSet<object>(), consumers = new WeakSet<object>();
          // Each tee branch has its own offset. Capture only the not-yet-seen suffix,
          // so reading either/both clones never duplicates bytes or model spans.
          const observeStream = (stream: ReadableStream<Uint8Array>, offset = 0, metadataOnly = false) => {
            if (observed.has(stream)) return;
            observed.add(stream);
            const read = (item: ReadableStreamReadResult<Uint8Array>) => {
              const end = Date.now(); // Before copying, scanning, concatenation or parsing.
              if (finished) return;
              if (item.done) { finalize(lastByteAt ?? end); return; }
              if (metadataOnly) { data.unavailable = true; return; }
              if (!(item.value instanceof Uint8Array)) return;
              const begin = offset; offset += item.value.byteLength;
              if (offset <= data.size) return;
              const bytes = item.value.subarray(Math.max(0, data.size - begin));
              data.size = offset;
              if (bytes.byteLength) {
                const seconds = (performance.now() - clock) / 1000;
                lastByteAt = end; data.firstChunkSeconds ??= seconds;
                if (data.arrivalTimes!.length < 4096) data.arrivalTimes!.push({ end: offset, seconds });
                else data.timingIncomplete = true;
              }
              if (encoded && !deferredAudio) {
                encoded.add(bytes);
                const retained = Math.min(bytes.byteLength, Math.max(0, 65536 - timingBytes));
                if (retained) { timingChunks.push(new Uint8Array(bytes.subarray(0, retained))); timingBytes += retained; }
                if (retained < bytes.byteLength) data.timingIncomplete = true;
              }
              else if (!binary && !data.truncated) {
                if (buffered + bytes.byteLength > max) { data.truncated = true; chunks.length = 0; buffered = 0; }
                else { chunks.push(new Uint8Array(bytes)); buffered += bytes.byteLength; }
              }
              if (expected !== undefined && data.size === expected) finalize(end);
            };
            const failure = (error: unknown) => {
              const end = Date.now();
              if (finished) return;
              // Invalid BYOB arguments/released readers are consumer API errors,
              // not transport failures. Only an errored source rejects reader.closed.
              data.error = error instanceof Error ? error : new Error("Response stream failure"); finalize(end);
            };
            const cancel = () => { if (!finished) { data.incomplete = expected === undefined || data.size !== expected; finalize(Date.now()); } };
            observeMethod(stream, "getReader", reader => {
              if (consumers.has(reader)) return; consumers.add(reader);
              observeMethod(reader, "read", promise => observe(promise, read, error => {
                // Released readers and invalid/detached BYOB views are consumer API
                // errors. Do not observe reader.closed: Node marks that internal
                // rejection handled even when no application handler is attached.
                if (error instanceof TypeError && /released|not attached|zero.length|detached|invalid.*view/i.test(error.message)) return;
                failure(error);
              }));
              observeMethod(reader, "cancel", () => cancel());
            });
            observeMethod(stream, "cancel", () => { if (!stream.locked) cancel(); });
            const iterator = (it: any, args: any[]) => {
              if (consumers.has(it)) return; consumers.add(it);
              observeMethod(it, "next", promise => observe(promise, read, failure));
              observeMethod(it, "return", () => { if (!args[0]?.preventCancel) cancel(); });
            };
            observeMethod(stream, "values", (it, args) => iterator(it, args));
            observeMethod(stream, Symbol.asyncIterator, (it, args) => iterator(it, args));
            observeMethod(stream, "tee", branches => { observeStream(branches[0], offset, metadataOnly); observeStream(branches[1], offset, metadataOnly); });
            // Native piping bypasses public reader methods. Do not replace streams
            // or sinks to extract bytes: retain a metadata-only completion instead.
            observeMethod(stream, "pipeTo", promise => observe(promise, () => { data.unavailable = true; finalize(Date.now()); }, () => { data.unavailable = true; data.incomplete = true; finalize(Date.now()); }));
            observeMethod(stream, "pipeThrough", output => { data.unavailable = true; observeStream(output, 0, true); });
          };
          const observeResponse = (value: Response) => {
            if (responses.has(value)) return;
            responses.add(value);
            if (value.body) observeStream(value.body);
            observeMethod(value, "clone", (clone, _args, receiver) => { if (receiver.body) observeStream(receiver.body); observeResponse(clone); });
          };
          observeResponse(response);
        } catch { /* Unsupported/frozen responses remain readable and unchanged. */ }
      }, (error: unknown) => {
        const end = Date.now();
        finish({ headers: {}, size: 0, error: error instanceof Error ? error : new Error("Transport failure") }, end);
      });
      return result;
    },
  });
  globalThis.fetch = patched;
  return { pending, remove() { active = false; if (globalThis.fetch === patched) globalThis.fetch = original; } };
}
