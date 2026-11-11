// Runs the maintained suites with a fresh, explicitly trusted loopback TLS certificate.
const { execFileSync, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'media-test-tls-'));
try {
  const key = path.join(dir, 'key.pem'), cert = path.join(dir, 'cert.pem');
  execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', key, '-out', cert, '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=IP:127.0.0.1,DNS:localhost'], { stdio: 'ignore' });
  const args = process.argv.slice(2);
  const files = args.length ? args : [path.join(__dirname, 'attachments.test.cjs'), path.resolve(__dirname, '../../traceai_claude_agent_sdk/test/attachments.test.cjs')];
  const result = spawnSync(process.execPath, ['--test', ...files], { stdio: 'inherit', timeout: 240000, env: { ...process.env, ATTACHMENT_TEST_KEY: key, NODE_EXTRA_CA_CERTS: cert } });
  if (result.error) throw result.error;
  process.exitCode = result.status ?? 1;
} finally { fs.rmSync(dir, { recursive: true, force: true }); }
