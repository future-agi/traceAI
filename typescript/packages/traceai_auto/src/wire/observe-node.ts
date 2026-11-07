import { channel } from "node:diagnostics_channel";
import { OutgoingMessage } from "node:http";
import { markSecretDiscoveryFailed, registerSecret } from "@traceai/fi-core";
import { credentialSecrets, discoverSecrets } from "./privacy-runtime";
import { header, isBinary, retainedBodySecrets, type RequestData } from "./core";
import { decodeBytes } from "./bytes";

/** Observation is independent of matching, sampling, OTel suppression and model-span dedup.
 * Never inspect a body here unless fetch already materialized it for wire capture.
 */
export function observeOutgoing(req: RequestData): void {
  observe(() => {
    for (const secret of credentialSecrets(req.headers)) registerSecret(secret);
    const retained = retainedBodySecrets(req);
    if (retained === null) throw new Error("Privacy request discovery incomplete");
    // FormData duplicates and cropped fields have already lost their original
    // values in req.fields. Publish the private discovery before any span can export.
    for (const secret of retained ?? []) registerSecret(secret);
    for (const secret of discoverSecrets([req.url, req.fields, req.fields === undefined && req.body && !isBinary(header(req.headers, "content-type")) ? decodeBytes(req.body) : undefined])) registerSecret(secret);
  });
}
function observe(work: () => void): void {
  try { work(); }
  catch {
    // A transport has seen credentials we cannot safely enumerate. Dropping only
    // its wire span would still leak echoes in unrelated/later application spans.
    try { markSecretDiscoveryFailed(); } catch { /* Never throw or log credential data. */ }
  }
}
function observeHeaders(url: string, headers: unknown): void {
  // Undici versions use either a CRLF block or a flat name/value array. Preserve
  // duplicates rather than coalescing two credential values into an unknown string.
  if (typeof headers === "string") {
    for (const line of headers.split("\r\n")) {
      const i = line.indexOf(":");
      if (i > 0) for (const secret of credentialSecrets({ [line.slice(0, i)]: line.slice(i + 1).trim() })) registerSecret(secret);
    }
  } else if (Array.isArray(headers)) {
    for (let i = 0; i + 1 < headers.length; i += 2) {
      const name = headers[i], value = headers[i + 1];
      if (typeof name === "string" && typeof value === "string") for (const secret of credentialSecrets({ [name]: value })) registerSecret(secret);
    }
  }
  for (const secret of discoverSecrets([url])) registerSecret(secret);
}
/** Diagnostics see cached/direct transports too, without replacing functions, request
 * objects, callbacks, promises or streams. They create no outgoing HTTP wire spans.
 */
export function observeTransports(): () => void {
  const subscriptions: [ReturnType<typeof channel>, (message: any) => void][] = [];
  const subscribe = (name: string, inspect: (message: any) => void) => {
    const topic = channel(name);
    const listener = (message: any) => observe(() => inspect(message));
    topic.subscribe(listener); subscriptions.push([topic, listener]);
  };
  subscribe("http.client.request.start", ({ request }) => {
    const headers = OutgoingMessage.prototype.getHeaders.call(request);
    for (const [name, raw] of Object.entries(headers)) {
      for (const value of Array.isArray(raw) ? raw : [raw]) if (typeof value === "string") {
        for (const secret of credentialSecrets({ [name]: value })) registerSecret(secret);
      }
    }
    observeHeaders(request.path, request._header);
  });
  subscribe("undici:request:create", ({ request }) => observeHeaders(request.path, request.headers));
  // Captures credentials added by dispatchers after request creation (including redirects).
  subscribe("undici:client:sendHeaders", ({ request, headers }) => observeHeaders(request.path, headers));
  return () => { for (const [topic, listener] of subscriptions) topic.unsubscribe(listener); };
}
