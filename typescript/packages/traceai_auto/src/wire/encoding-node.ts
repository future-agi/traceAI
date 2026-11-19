import * as zlib from "node:zlib";
import { concatBytes } from "./bytes";
import type { BodyDecoder } from "./encoding";
/** Node codecs, including Brotli/zstd when present, without inflating past the body cap. */
export const decodeNodeBody: BodyDecoder = (encoding, body, max, allowPartial, consume) => new Promise((resolve, reject) => {
  const options = allowPartial ? { finishFlush: zlib.constants.Z_SYNC_FLUSH } : undefined;
  const factories: Record<string, (() => zlib.Gunzip) | undefined> = {
    gzip: () => zlib.createGunzip(options), deflate: () => zlib.createInflate(options) as any,
    br: zlib.createBrotliDecompress as any, zstd: (zlib as any).createZstdDecompress,
  };
  const factory = factories[encoding]; if (!factory) { reject(new Error("Unsupported content encoding")); return; }
  const stream = factory(), chunks: Uint8Array[] = []; let size = 0, finished = false;
  const timer = setTimeout(() => fail(new Error("Decoder deadline exceeded")), 5000); timer.unref?.();
  const fail = (error: Error) => { if (finished) return; finished = true; clearTimeout(timer); stream.destroy(); reject(error); };
  const done = (truncated: boolean) => {
    if (finished) return; finished = true; clearTimeout(timer); stream.destroy();
    resolve(truncated ? { truncated: true } : { body: consume ? undefined : concatBytes(chunks, size) });
  };
  stream.on("data", (chunk: Buffer) => {
    size += chunk.byteLength;
    if (size > max) done(true); else if (consume) consume(chunk); else chunks.push(chunk);
  });
  stream.once("error", error => { if (allowPartial && size > 0) done(false); else fail(error); });
  stream.once("end", () => done(false)); stream.end(body);
});
