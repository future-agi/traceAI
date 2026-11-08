const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createContentSanitizer } = require('../dist/src/content.js');
const { registerSecret } = require('@traceai/fi-core');

for (const shape of ['text', 'json', 'encoded', 'double']) {
  for (const source of ['configured', 'registered', 'environment']) {
    test(`complete ${source} secret before PII and UTF-8 cut: ${shape}`, () => {
      const secret = `SYNTHETIC_OVERLAP-${source}-${shape}-123-45-6789-PRIVATE_SUFFIX`;
      const old = process.env.TEST_PRIVATE_KEY;
      if (source === 'environment') process.env.TEST_PRIVATE_KEY = secret;
      if (source === 'registered') registerSecret(secret);
      try {
        const sanitize = createContentSanitizer(source === 'configured' ? [secret] : [], { piiRedaction: true });
        let value = 'PUBLIC ' + '😀'.repeat(4980) + secret + ' trailing public';
        if (shape === 'json') value = JSON.stringify({ stdout: value });
        if (shape === 'encoded') value = JSON.stringify({ encoded: JSON.stringify({ stdout: value }) });
        if (shape === 'double') value = JSON.stringify(JSON.stringify({ stdout: value }));
        const clean = sanitize(value);
        assert.ok(Buffer.byteLength(clean) <= 20000);
        assert.match(clean, /PUBLIC/);
        assert.doesNotMatch(clean, /SYNTHETIC_OVERLAP|PRIVATE_SUFFIX|123-45-6|�/);
        if (shape !== 'text') assert.doesNotThrow(() => JSON.parse(clean));
      } finally {
        if (old === undefined) delete process.env.TEST_PRIVATE_KEY;
        else process.env.TEST_PRIVATE_KEY = old;
      }
    });
  }
}

for (const text of ['PUBLIC_TOOL_RESULT file contents\n', 'PUBLIC_TOOL_RESULT 漢字測試 😀\n', 'PUBLIC_TOOL_RESULT "quote" \\slash\n']) {
  for (let layers = 0; layers <= 3; layers++) {
    test(`readable byte-bounded JSON at ${layers} encoded layers: ${text.slice(-10)}`, () => {
      let value = JSON.stringify({ stdout: text.repeat(1100), exitCode: 0 });
      for (let i = 0; i < layers; i++) value = JSON.stringify(value);
      const clean = createContentSanitizer()(value);
      assert.ok(Buffer.byteLength(clean) <= 20000);
      assert.ok(Buffer.byteLength(clean) > 19000);
      assert.match(clean, /PUBLIC_TOOL_RESULT/);
      let decoded = clean;
      for (let i = 0; i <= layers; i++) decoded = JSON.parse(decoded);
      assert.match(decoded.stdout, /PUBLIC_TOOL_RESULT/);
    });
  }
}

test('65 and 100 item public arrays are not replaced with a size marker', () => {
  for (const length of [65, 100]) {
    const value = JSON.stringify({ purpose: 'PUBLIC', files: Array.from({ length }, (_, i) => `src/public-${i}.ts`) });
    assert.equal(createContentSanitizer()(value), value);
  }
});

test('embedding context is inspected before its late discriminator is cut', () => {
  const value = JSON.stringify({ visible: 'PUBLIC', data: [0.314159265, 0.271828182], padding: 'x'.repeat(22000), object: 'embedding' });
  const clean = createContentSanitizer([], { hideEmbeddingVectors: true })(value);
  assert.match(clean, /PUBLIC/);
  assert.doesNotMatch(clean, /0\.314159265|0\.271828182/);
});

test('JSON-encoded configured credential is matched before any structural rewrite', () => {
  const secret = JSON.stringify({ first: 'SYNTHETIC_FIRST', second: 'SYNTHETIC_SECOND' });
  const value = JSON.stringify({ visible: 'PUBLIC', padding: 'x'.repeat(19800), echo: secret, tail: 'y'.repeat(500) });
  const clean = createContentSanitizer([secret], { piiRedaction: true })(value);
  assert.match(clean, /PUBLIC/);
  assert.doesNotMatch(clean, /SYNTHETIC_FIRST|SYNTHETIC_SECOND/);
});

test('short configured keys cannot split longer registered credentials', () => {
  registerSecret('LONG_PRIVATE_abcde_SUFFIX');
  const clean = createContentSanitizer(['abcde'])('PUBLIC LONG_PRIVATE_abcde_SUFFIX and abcde');
  assert.match(clean, /PUBLIC/);
  assert.doesNotMatch(clean, /LONG_PRIVATE|SUFFIX|abcde/);
});

test('processing limits fail closed and short configured keys retain their protection', () => {
  assert.equal(createContentSanitizer()('x'.repeat(1048577)), '[REDACTED_SIZE_LIMIT]');
  const key = 'a"b\\c';
  const clean = createContentSanitizer([key])(JSON.stringify({ public: 'PUBLIC', echo: key }));
  assert.equal(JSON.parse(clean).echo, '[REDACTED]');
});
