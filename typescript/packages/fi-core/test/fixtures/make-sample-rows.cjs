// Writes plaintext-rule-SAMPLE.rows.json.gz and SHA256SUMS next to this file. The rows are written by hand from the cross-SDK rules;
// nothing here runs the SDK, so the expectations are not copied from its output. This is a SAMPLE for exercising the row runner
// (fixture-rows.test.cjs); the sealed cross-language rows are vendored separately and run by the same runner.
const fs = require('node:fs');
const path = require('node:path');
const zlib = require('node:zlib');
const crypto = require('node:crypto');
const PROXY = 'http://proxy.internal.example:3128';
// expectation helpers: dialed is host:port as the transport receives it (the URL parser's normalised host, one trailing dot dropped, IPv6 in brackets;
// the scheme's default port when none is written; 443 for a bare gRPC target)
const allow = (dialed, tls, keysOnWire = true) => ({ decision: 'allow', code: null, dialed, tls, keysOnWire });
const refuse = code => ({ decision: 'refuse', code, dialed: '', tls: false, keysOnWire: false });
const H = (id, target, expect, more = {}) => ({ id, input: { target, transport: 'http', keys: 'explicit', optIn: 'none', proxyEnv: {}, dispatcher: 'none', ...more }, expect });
const G = (id, target, expect, more = {}) => ({ id, input: { target, transport: 'grpc', keys: 'explicit', optIn: 'none', proxyEnv: {}, dispatcher: 'none', ...more }, expect });
const rows = [
  // TLS, loopback and Future AGI hosts
  H('http-fi-https', 'https://api.futureagi.com', allow('api.futureagi.com:443', true)),
  H('http-fi-https-trailing-dot', 'https://api.futureagi.com./', allow('api.futureagi.com:443', true)),
  H('http-fi-plaintext', 'http://api.futureagi.com', refuse('FI_PLAINTEXT_FI_HOST')),
  H('http-fi-plaintext-trailing-dot-with-opt-in', 'http://api.futureagi.com.', refuse('FI_PLAINTEXT_FI_HOST'), { optIn: 'env' }),
  H('http-loopback-127-0-0-1', 'http://127.0.0.1:4318', allow('127.0.0.1:4318', false)),
  H('http-loopback-localhost', 'http://localhost:4318', allow('localhost:4318', false)),
  H('http-loopback-ipv6', 'http://[::1]:4318', allow('[::1]:4318', false)),
  H('http-loopback-with-http-proxy-is-direct', 'http://127.0.0.1:4318', allow('127.0.0.1:4318', false), { proxyEnv: { HTTP_PROXY: PROXY } }),
  H('http-no-keys-self-hosted', 'http://collector.example:4318', allow('collector.example:4318', false, false), { keys: 'none' }),
  // loopback is decided on the host as written
  H('http-127-1-is-not-loopback', 'http://127.1:4318', refuse('FI_PLAINTEXT_NOT_ALLOWED')),
  H('http-0x7f-is-not-loopback', 'http://0x7f.0.0.1:4318', refuse('FI_PLAINTEXT_NOT_ALLOWED')),
  H('http-decimal-is-not-loopback', 'http://2130706433:4318', refuse('FI_PLAINTEXT_NOT_ALLOWED')),
  H('http-leading-zero-is-not-loopback', 'http://0177.0.0.1:4318', refuse('FI_PLAINTEXT_NOT_ALLOWED')),
  H('http-trailing-dot-loopback-is-not-loopback', 'http://127.0.0.1.:4318', refuse('FI_PLAINTEXT_NOT_ALLOWED')),
  H('http-127-1-with-opt-in-dials-127-0-0-1', 'http://127.1:4318', allow('127.0.0.1:4318', false), { optIn: 'env' }),
  // plaintext opt-in and proxies
  H('http-self-hosted-no-opt-in', 'http://collector.example:4318', refuse('FI_PLAINTEXT_NOT_ALLOWED')),
  H('http-self-hosted-env-opt-in', 'http://collector.example:4318', allow('collector.example:4318', false), { optIn: 'env' }),
  H('http-self-hosted-option-opt-in', 'http://collector.example:4318', allow('collector.example:4318', false), { optIn: 'option' }),
  H('http-sdk-built-with-http-proxy-is-direct', 'http://collector.example:4318', allow('collector.example:4318', false), { optIn: 'env', proxyEnv: { HTTP_PROXY: PROXY } }),
  H('http-caller-agent-with-http-proxy', 'http://collector.example:4318', refuse('FI_PLAINTEXT_PROXIED'), { optIn: 'env', proxyEnv: { HTTP_PROXY: PROXY }, dispatcher: 'agent' }),
  H('http-caller-agent-with-https-proxy-only', 'http://collector.example:4318', allow('collector.example:4318', false), { optIn: 'env', proxyEnv: { HTTPS_PROXY: PROXY }, dispatcher: 'agent' }),
  H('http-caller-agent-no-proxy-exact-host', 'http://collector.example:4318', allow('collector.example:4318', false), { optIn: 'env', proxyEnv: { HTTP_PROXY: PROXY, NO_PROXY: 'collector.example' }, dispatcher: 'agent' }),
  H('http-caller-agent-no-proxy-leading-dot', 'http://a.example.com:4318', allow('a.example.com:4318', false), { optIn: 'env', proxyEnv: { HTTP_PROXY: PROXY, NO_PROXY: '.example.com' }, dispatcher: 'agent' }),
  H('http-caller-agent-no-proxy-star', 'http://collector.example:4318', allow('collector.example:4318', false), { optIn: 'env', proxyEnv: { HTTP_PROXY: PROXY, NO_PROXY: '*' }, dispatcher: 'agent' }),
  H('http-caller-agent-no-proxy-other-port', 'http://collector.example:4318', refuse('FI_PLAINTEXT_PROXIED'), { optIn: 'env', proxyEnv: { HTTP_PROXY: PROXY, NO_PROXY: 'collector.example:9999' }, dispatcher: 'agent' }),
  H('http-caller-agent-no-proxy-leading-zero-port', 'http://collector.example:4318', refuse('FI_PLAINTEXT_PROXIED'), { optIn: 'env', proxyEnv: { HTTP_PROXY: PROXY, NO_PROXY: 'collector.example:04318' }, dispatcher: 'agent' }),
  H('http-caller-agent-no-proxy-empty-lowercase', 'http://collector.example:4318', refuse('FI_PLAINTEXT_PROXIED'), { optIn: 'env', proxyEnv: { HTTP_PROXY: PROXY, NO_PROXY: 'collector.example', no_proxy: '' }, dispatcher: 'agent' }),
  H('http-caller-agent-trailing-dot-host-not-bypassed', 'http://collector.example.:4318', refuse('FI_PLAINTEXT_PROXIED'), { optIn: 'env', proxyEnv: { HTTP_PROXY: PROXY, NO_PROXY: 'collector.example' }, dispatcher: 'agent' }),
  H('http-caller-proxy-agent-loopback', 'http://127.0.0.1:4318', refuse('FI_PLAINTEXT_PROXIED'), { dispatcher: 'proxy' }),
  H('http-caller-env-proxy-agent-loopback', 'http://localhost:4318', refuse('FI_PLAINTEXT_PROXIED'), { dispatcher: 'envproxy' }),
  H('http-caller-agent-loopback-with-http-proxy', 'http://127.0.0.1:4318', allow('127.0.0.1:4318', false), { proxyEnv: { HTTP_PROXY: PROXY }, dispatcher: 'agent' }),
  // A dispatcher whose dial can be redirected is unknown and refused (plaintext and https alike); an origin-bound pool or client cannot be shown to dial its origin.
  H('http-caller-pool-same-origin-is-opaque', 'http://127.0.0.1:4318', refuse('FI_DISPATCHER_UNKNOWN'), { dispatcher: 'pool-same' }),
  H('http-caller-pool-other-origin', 'http://127.0.0.1:4318', refuse('FI_DISPATCHER_UNKNOWN'), { dispatcher: 'pool-other' }),
  H('http-caller-pool-unknown-origin', 'http://127.0.0.1:4318', refuse('FI_DISPATCHER_UNKNOWN'), { dispatcher: 'pool-unknown' }),
  H('http-caller-agent-with-connect-function', 'http://127.0.0.1:4318', refuse('FI_DISPATCHER_UNKNOWN'), { dispatcher: 'agent-connect' }),
  H('http-caller-agent-with-socket-path', 'http://127.0.0.1:4318', refuse('FI_DISPATCHER_UNKNOWN'), { dispatcher: 'agent-socketpath' }),
  H('http-caller-agent-with-custom-factory', 'http://127.0.0.1:4318', refuse('FI_DISPATCHER_UNKNOWN'), { dispatcher: 'agent-factory' }),
  H('https-caller-pool-is-refused', 'https://api.futureagi.com', refuse('FI_DISPATCHER_UNKNOWN'), { dispatcher: 'pool-other' }),
  H('https-caller-agent-with-connect-function', 'https://api.futureagi.com', refuse('FI_DISPATCHER_UNKNOWN'), { dispatcher: 'agent-connect' }),
  H('https-caller-plain-agent', 'https://api.futureagi.com', allow('api.futureagi.com:443', true), { dispatcher: 'agent' }),
  H('http-https-through-caller-proxy-agent-is-allowed', 'https://collector.example:4318', allow('collector.example:4318', true), { dispatcher: 'proxy' }),
  // disputed hosts are refused whatever the opt-in says
  H('http-backslash-before-at', 'http://collector.example\\@api.futureagi.com', refuse('FI_HOST_MISMATCH'), { optIn: 'env' }),
  H('http-percent-encoded-backslash-before-at', 'http://collector.example%5C@api.futureagi.com', refuse('FI_HOST_MISMATCH'), { optIn: 'env' }),
  H('http-at-in-path', 'http://collector.example/x@api.futureagi.com', refuse('FI_HOST_MISMATCH'), { optIn: 'env' }),
  H('http-empty-authority', 'https:///api.futureagi.com/', refuse('FI_EMPTY_AUTHORITY')),
  // gRPC
  G('grpc-fi-bare', 'grpc.futureagi.com:443', allow('grpc.futureagi.com:443', true)),
  G('grpc-fi-http-prefix-is-tls', 'http://grpc.futureagi.com:4317', allow('grpc.futureagi.com:4317', true), { optIn: 'option' }),
  G('grpc-loopback', 'localhost:4317', allow('localhost:4317', false)),
  G('grpc-loopback-with-every-proxy-variable', 'http://127.0.0.1:4317', allow('127.0.0.1:4317', false), { proxyEnv: { grpc_proxy: PROXY, https_proxy: PROXY, http_proxy: PROXY } }),
  G('grpc-self-hosted-tls', 'collector.example:4317', allow('collector.example:4317', true)),
  G('grpc-self-hosted-plaintext-no-opt-in', 'http://collector.example:4317', refuse('FI_PLAINTEXT_NOT_ALLOWED')),
  G('grpc-self-hosted-plaintext-opt-in-with-grpc-proxy', 'http://collector.example:4317', allow('collector.example:4317', false), { optIn: 'env', proxyEnv: { grpc_proxy: PROXY } }),
  G('grpc-backslash-before-at', 'grpc://grpc.futureagi.com\\@evil.example:4317', refuse('FI_HOST_MISMATCH')),
];
const header = { format: 'fi-plaintext-rule-rows', version: 1, sample: true,
  note: 'SAMPLE rows written by hand from the cross-SDK plaintext rules, to exercise the row runner. They are not the sealed cross-language fixture.',
  rowShape: { input: ['target', 'transport (http|grpc)', 'keys (explicit|env|none)', 'optIn (none|env|option)', 'proxyEnv (object of environment variables)', 'dispatcher (none|agent|agent-connect|agent-socketpath|agent-factory|proxy|envproxy|pool-same|pool-other|pool-unknown)'],
    expect: ['decision (allow|refuse)', 'code (FI_* | null)', 'dialed (host:port)', 'tls (boolean)', 'keysOnWire (boolean)'] } };
const file = path.join(__dirname, 'plaintext-rule-SAMPLE.rows.json.gz');
// Fixed gzip header fields (mtime 0) so the bytes, and the sidecar sum, are reproducible.
const body = zlib.gzipSync(Buffer.from(JSON.stringify({ header, rows }, null, 1)), { level: 9 });
body.writeUInt32LE(0, 4);
fs.writeFileSync(file, body);
const sha = buffer => crypto.createHash('sha256').update(buffer).digest('hex');
fs.writeFileSync(path.join(__dirname, 'SHA256SUMS'), `${sha(body)}  ${path.basename(file)}\n${sha(zlib.gunzipSync(body))}  ${path.basename(file).replace(/\.gz$/, '')}\n`);
console.log(`${rows.length} rows -> ${file}`);
