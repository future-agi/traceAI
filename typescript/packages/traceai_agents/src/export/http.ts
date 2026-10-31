import * as https from "node:https";
import * as http from "node:http";
import { validateBinding, type Binding } from "./endpoint.js";
import { decodeRejections } from "./otlp.js";
export type ExportResult = {
  state: "accepted" | "retry" | "held" | "split" | "ambiguous";
  code: string;
  retryAfterMs?: number;
};
export function retryDelay(
  attempt: number,
  retryAfterMs = 0,
  random = Math.random,
): number {
  return Math.min(
    60_000,
    Math.max(
      Math.min(60_000, retryAfterMs),
      Math.min(60_000, 1000 * 2 ** Math.min(16, Math.max(0, attempt))) *
        (0.5 + random() * 0.5),
    ),
  );
}
/** node:http(s) does not follow redirects, consult ambient proxies, or use fetch dispatchers. */
export async function exportBytes(
  binding: Binding,
  body: Buffer,
  credentials: { api_key: string; secret_key: string },
  signal?: AbortSignal,
): Promise<ExportResult> {
  const url = validateBinding(binding);
  if (binding.backendContract !== "immutable-agent-v1")
    return { state: "held", code: "HELD_BACKEND_CONTRACT" };
  if (body.length > 1_048_576)
    return { state: "split", code: "OVERSIZE_BATCH" };
  if (
    ![credentials.api_key, credentials.secret_key].every(
      (k) => typeof k === "string" && /^[\x21-\x7e]{1,4096}$/.test(k),
    )
  )
    return { state: "held", code: "HELD_AUTH" };
  return new Promise((resolve) => {
    let completed = false;
    const finish = (r: ExportResult): void => {
      if (completed) return;
      completed = true;
      clearTimeout(total);
      resolve(r);
    };
    const request = (url.protocol === "https:" ? https : http).request(
      url,
      {
        method: "POST",
        agent: false,
        signal,
        rejectUnauthorized: true,
        headers: {
          "Content-Type": "application/x-protobuf",
          "Content-Length": body.length,
          "X-Api-Key": credentials.api_key,
          "X-Secret-Key": credentials.secret_key,
        },
      },
      (response) => {
        const status = response.statusCode ?? 0;
        const reject = (result: ExportResult): void => {
          finish(result);
          response.destroy();
        };
        if (status >= 300 && status < 400)
          return reject({ state: "held", code: "HELD_REDIRECT" });
        if (status === 401 || status === 403)
          return reject({ state: "held", code: "HELD_AUTH" });
        if (status === 413)
          return reject({ state: "split", code: "PAYLOAD_TOO_LARGE" });
        if (status === 429 || status >= 500) {
          const raw = response.headers["retry-after"];
          const seconds =
            typeof raw === "string" && /^\d{1,8}$/.test(raw) ? Number(raw) : 0;
          return reject({
            state: "retry",
            code: "RETRY_HTTP",
            retryAfterMs: Math.min(60_000, seconds * 1000),
          });
        }
        if (status < 200 || status >= 300)
          return reject({ state: "held", code: "REJECTED_HTTP" });
        let size = 0;
        const chunks: Buffer[] = [];
        response.on("data", (chunk) => {
          size += chunk.length;
          if (size > 65_536)
            reject({ state: "ambiguous", code: "INVALID_RESPONSE" });
          else chunks.push(chunk);
        });
        response.on("error", () =>
          finish({ state: "ambiguous", code: "AMBIGUOUS_RESPONSE" }),
        );
        response.on("end", () => {
          try {
            const bytes = Buffer.concat(chunks);
            if (
              response.headers["content-encoding"] &&
              response.headers["content-encoding"] !== "identity"
            )
              throw new Error();
            const type = response.headers["content-type"]?.split(";")[0];
            let rejected = 0;
            if (bytes.length && type === "application/json") {
              const parsed = JSON.parse(bytes.toString());
              rejected = Number(
                parsed.partialSuccess?.rejectedSpans ??
                  parsed.partial_success?.rejected_spans ??
                  0,
              );
              if (!Number.isSafeInteger(rejected) || rejected < 0)
                throw new Error();
            } else if (bytes.length) {
              if (type !== "application/x-protobuf") throw new Error();
              rejected = decodeRejections(bytes);
            }
            finish(
              rejected > 0
                ? { state: "ambiguous", code: "AMBIGUOUS_PARTIAL" }
                : { state: "accepted", code: "HTTP_ACCEPTED" },
            );
          } catch {
            finish({ state: "ambiguous", code: "INVALID_RESPONSE" });
          }
        });
      },
    );
    const total = setTimeout(() => {
      finish({ state: "retry", code: "TRANSPORT_TIMEOUT" });
      request.destroy();
    }, 10_000);
    request.setTimeout(3000, () => {
      finish({ state: "retry", code: "TRANSPORT_TIMEOUT" });
      request.destroy();
    });
    request.on("error", (error: NodeJS.ErrnoException) => {
      const tls = /CERT|TLS|SSL|SELF_SIGNED|UNABLE_TO_VERIFY/.test(
        error.code ?? "",
      );
      finish({
        state: tls ? "held" : "retry",
        code: tls ? "HELD_TLS" : "TRANSPORT_UNAVAILABLE",
      });
    });
    request.end(body);
  });
}
