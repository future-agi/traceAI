// Web-platform primitives only: shared by Node and isolate runtimes.
const encoder = new TextEncoder(), decoder = new TextDecoder();
export function encodeBytes(value: string): Uint8Array { return encoder.encode(value); }
export function byteLength(value: string): number {
  let size = 0;
  for (let i = 0; i < value.length; i++) {
    const c = value.charCodeAt(i);
    if (c < 128) size++; else if (c < 2048) size += 2;
    else if (c >= 0xd800 && c <= 0xdbff && i + 1 < value.length && value.charCodeAt(i + 1) >= 0xdc00 && value.charCodeAt(i + 1) <= 0xdfff) { size += 4; i++; }
    else size += 3;
  }
  return size;
}
export function decodeBytes(value: Uint8Array): string { return decoder.decode(value); }
export function concatBytes(chunks: Uint8Array[], size: number): Uint8Array {
  const result = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.byteLength; }
  return result;
}
export function latin1(value: Uint8Array): string {
  let result = "";
  for (let i = 0; i < value.length; i += 8192) result += String.fromCharCode(...value.subarray(i, i + 8192));
  return result;
}
export function fromLatin1(value: string): Uint8Array { return Uint8Array.from(value, c => c.charCodeAt(0)); }
