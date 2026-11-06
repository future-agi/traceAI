import { request as httpsRequest } from "node:https";
import { request as httpRequest } from "node:http";
import { endpoint, type Credentials } from "./config.js";
export type DeliveryCode =
  | "ACCEPTED"
  | "AUTH_REJECTED"
  | "RATE_LIMITED"
  | "TOO_LARGE"
  | "PARTIAL_REJECTION"
  | "UNVERIFIED"
  | "TRANSPORT_AMBIGUOUS"
  | "DISABLED";
export interface DeliveryResult {
  code: DeliveryCode;
  status?: number;
  rejectedSpans?: number;
  acceptedAt?: string;
}
export interface PostOptions {
  endpoint: string;
  projectName: string;
  credentials: Credentials;
  body: Uint8Array | string;
  timeoutMs?: number;
  signal?: AbortSignal;
}
/** Exact OTLP JSON destination, verified TLS, no redirects, bounded body and deadline. Never surfaces server text. */
export function postOtlp(options: PostOptions): Promise<DeliveryResult> {
  const url = new URL(endpoint(options.endpoint));
  if (
    !options.projectName ||
    /[\r\n]/.test(options.projectName) ||
    [options.credentials.apiKey, options.credentials.secretKey].some(
      (v) => !v || /[\r\n]/.test(v),
    )
  )
    return Promise.resolve({ code: "UNVERIFIED" });
  return new Promise((resolve) => {
    let done = false;
    const finish = (result: DeliveryResult): void => {
      if (!done) {
        done = true;
        clearTimeout(timer);
        options.signal?.removeEventListener("abort", abort);
        resolve(result);
      }
    };
    const abort = (): void => {
      req.destroy();
      finish({ code: "DISABLED" });
    };
    const req = (url.protocol === "https:" ? httpsRequest : httpRequest)(
      url,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(options.body),
          "X-Api-Key": options.credentials.apiKey,
          "X-Secret-Key": options.credentials.secretKey,
          "X-Project-Name": options.projectName,
        },
      },
      (res) => {
        const status = res.statusCode ?? 0;
        const codes: Record<number, DeliveryCode> = {
          401: "AUTH_REJECTED",
          403: "AUTH_REJECTED",
          429: "RATE_LIMITED",
          413: "TOO_LARGE",
        };
        if (status !== 200) {
          res.destroy();
          finish({ code: codes[status] ?? "UNVERIFIED", status });
          return;
        }
        let length = 0;
        const chunks: Buffer[] = [];
        res.on("data", (chunk: Buffer) => {
          length += chunk.length;
          if (length > 64 * 1024) {
            res.destroy();
            finish({ code: "UNVERIFIED", status });
          } else chunks.push(chunk);
        });
        res.on("error", () => finish({ code: "TRANSPORT_AMBIGUOUS", status }));
        res.on("end", () => {
          try {
            if (
              !(
                (res.headers["content-type"] ?? "")
                  .split(";")[0]
                  .trim()
                  .toLowerCase() === "application/json"
              )
            )
              throw new Error();
            const value: unknown = JSON.parse(Buffer.concat(chunks).toString());
            if (!value || typeof value !== "object" || Array.isArray(value))
              throw new Error();
            const body = value as Record<string, unknown>;
            if (Object.keys(body).some((k) => k !== "partialSuccess"))
              throw new Error();
            if (body.partialSuccess !== undefined) {
              if (
                !body.partialSuccess ||
                typeof body.partialSuccess !== "object" ||
                Array.isArray(body.partialSuccess)
              )
                throw new Error();
              const partial = body.partialSuccess as Record<string, unknown>;
              if (
                Object.keys(partial).some(
                  (k) => !["rejectedSpans", "errorMessage"].includes(k),
                )
              )
                throw new Error();
              const count = Number(partial.rejectedSpans ?? 0);
              if (!Number.isSafeInteger(count) || count < 0) throw new Error();
              if (count || partial.errorMessage) {
                finish({
                  code: "PARTIAL_REJECTION",
                  status,
                  rejectedSpans: count,
                });
                return;
              }
            }
            finish({
              code: "ACCEPTED",
              status,
              acceptedAt: new Date().toISOString(),
            });
          } catch {
            finish({ code: "UNVERIFIED", status });
          }
        });
      },
    );
    const timer = setTimeout(
      () => {
        req.destroy();
        finish({ code: "TRANSPORT_AMBIGUOUS" });
      },
      Math.max(1, Math.min(options.timeoutMs ?? 5000, 5000)),
    );
    req.on("error", () => finish({ code: "TRANSPORT_AMBIGUOUS" }));
    options.signal?.addEventListener("abort", abort, { once: true });
    if (options.signal?.aborted) abort();
    else req.end(options.body);
  });
}
export async function validateCredentials(
  options: Omit<PostOptions, "body">,
): Promise<DeliveryResult> {
  const result = await postOtlp({ ...options, body: '{"resourceSpans":[]}' });
  // An empty-request timeout is unverified, not rejected credentials.
  return result.code === "TRANSPORT_AMBIGUOUS"
    ? { ...result, code: "UNVERIFIED" }
    : result;
}
