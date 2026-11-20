// Runs rows of a cross-language plaintext-rule fixture through the guard. The rows come from a gzipped JSON file named by FI_FIXTURE_ROWS
// (default: the sample next to this file). The file's sha256 is compared with the entry for its name in the SHA256SUMS file beside it; no
// sum is written in this code. Each row's input goes through the guard in a scoped environment and the outcome must equal the row's expectation:
// decision, error code, dialed target (host:port), TLS or plain, keys on the wire.
//
// File shape: { header: { format: "fi-plaintext-rule-rows", version: 1, ... }, rows: [{ id, input, expect }] }
//   input:  { target, transport: "http"|"grpc", keys: "explicit"|"env"|"none", optIn: "none"|"env"|"option",
//             proxyEnv: { NAME: value }, dispatcher: "none"|"agent"|"proxy"|"envproxy"|"pool-same"|"pool-other"|"pool-unknown" }
//   expect: { decision: "allow"|"refuse", code: "FI_..."|null, dialed: "host:port", tls: boolean, keysOnWire: boolean }
//   dialed: the host as the transport receives it (the URL parser's normalised host, one trailing dot dropped, IPv6 in brackets) and the port
//   (written, else the scheme's default; 443 for a bare gRPC target). A refused row has dialed "", tls false, keysOnWire false.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const zlib = require('node:zlib');
const { createRequire } = require('node:module');
const load = createRequire(process.env.FI_CORE_PACKAGE_JSON || path.resolve(__dirname, '../package.json'));
const { evaluateRow } = load('./dist/src/fi-send-guard');

const rowsPath = path.resolve(process.env.FI_FIXTURE_ROWS || path.join(__dirname, 'fixtures', 'plaintext-rule-SAMPLE.rows.json.gz'));
const sums = fs.readFileSync(path.join(path.dirname(rowsPath), 'SHA256SUMS'), 'utf8').split('\n').filter(Boolean)
  .map(line => /^([0-9a-f]{64}) [ *](.+)$/.exec(line)).filter(Boolean);
const bytes = fs.readFileSync(rowsPath);

test('the fixture file matches its sidecar sum', () => {
  const entry = sums.find(([, , name]) => name === path.basename(rowsPath));
  assert.ok(entry, `SHA256SUMS has no entry for ${path.basename(rowsPath)}`);
  assert.equal(crypto.createHash('sha256').update(bytes).digest('hex'), entry[1]);
});

const document = JSON.parse(zlib.gunzipSync(bytes).toString('utf8'));
test('the header names the format', () => {
  assert.equal(document.header.format, 'fi-plaintext-rule-rows');
  assert.ok(Array.isArray(document.rows) && document.rows.length > 0);
});
for (const row of document.rows) {
  test(row.id, () => { assert.deepEqual(evaluateRow(row.input), row.expect); });
}
