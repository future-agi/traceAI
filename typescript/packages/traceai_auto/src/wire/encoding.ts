import { header, ResponseData } from "./core";
import { concatBytes, decodeBytes } from "./bytes";
/** Fetch normally decodes but retains Content-Encoding. Never decode that body twice. */
export function responseEncoding(response: ResponseData): string | undefined {
  const body = response.body, encoding = header(response.headers, "content-encoding");
  if (response.alreadyDecoded || !body?.length || !encoding || encoding === "identity") return undefined;
  if (encoding === "gzip") return body[0] === 0x1f && body[1] === 0x8b ? encoding : undefined;
  if (encoding === "deflate") return body.length > 1 && (body[0] & 15) === 8 && ((body[0] << 8) + body[1]) % 31 === 0 ? encoding : undefined;
  // For codecs without a magic header, recognize the already-decoded formats we parse.
  const prefix = decodeBytes(body.subarray(0, 96)).trimStart();
  return /^(?:[\[{]|data:|event:)/.test(prefix) ? undefined : encoding;
}
export type BodyDecoder = (encoding: string, body: Uint8Array, max: number, allowPartial?: boolean, consume?: (chunk: Uint8Array) => void) => Promise<{ body?: Uint8Array; truncated?: boolean }>;
/** Bounded, asynchronous decoding; runs only after the consumer finished the response. */
export async function decodeResponse(response: ResponseData, max: number, decodeBody?: BodyDecoder, consume?: (chunk: Uint8Array) => void): Promise<void> {
  const encoding = responseEncoding(response);
  if (!encoding || !response.body) return;
  try {
    if (decodeBody) {
      const decoded = await decodeBody(encoding, response.body, max, Boolean(response.error || response.incomplete), consume);
      response.body = decoded.body;
      if (decoded.truncated) response.truncated = true;
      return;
    }
    const decoder = new DecompressionStream((encoding === "br" ? "brotli" : encoding) as CompressionFormat);
    const source = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(response.body!); controller.close(); } });
    const reader = source.pipeThrough(decoder as any).getReader() as ReadableStreamDefaultReader<Uint8Array>;
    const chunks: Uint8Array[] = []; let size = 0;
    try {
      while (true) {
        const { value, done } = await reader.read(); if (done) break;
        size += value.byteLength;
        if (size > max) { response.truncated = true; response.body = undefined; void reader.cancel().catch(() => {}); return; }
        if (consume) consume(value); else chunks.push(value);
      }
      response.body = consume ? undefined : concatBytes(chunks, size);
    } finally { reader.releaseLock(); }
  } catch {
    response.body = undefined; response.undecodedEncoding = encoding;
  }
}
