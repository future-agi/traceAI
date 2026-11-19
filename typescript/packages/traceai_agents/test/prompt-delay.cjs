// Test-only scheduler stall after a visible prompt. The PTY sends synthetic
// credentials immediately, reproducing the echo race deterministically.
const write = process.stderr.write;
process.stderr.write = function (chunk, ...args) {
  const result = write.call(this, chunk, ...args);
  if (/^(API key|Secret key) \(hidden\): /.test(String(chunk))) {
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 150);
  }
  return result;
};
