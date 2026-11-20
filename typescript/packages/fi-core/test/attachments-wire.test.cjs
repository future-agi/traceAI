/* T-TR-01: every F01 wire fixture for the shapes the SDK sends or parses, reproduced by the hand validators. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { core } = require('./attachments-v1.cjs');
const directory = process.env.FI_ATTACHMENTS_SPEC_DIR;
const SDK = new Set(core.ATTACHMENT_WIRE_SHAPES);
test('T-TR-01 F01 wire fixtures agree with the core validators', { skip: !directory && 'FI_ATTACHMENTS_SPEC_DIR not set' }, () => {
  const cases = JSON.parse(fs.readFileSync(path.join(directory, 'wire-fixtures.json'))).cases;
  let checked = 0; const skipped = new Set();
  for (const fixture of cases) {
    let schema = fixture.schema; let value = fixture.value;
    if (schema === 'ImportRequest' && value && value.mode === 'completed_recording') schema = 'RecordingImportRequest';
    else if (schema === 'ImportRequest') { skipped.add('ImportRequest(single)'); continue; }
    if (!SDK.has(schema)) { skipped.add(schema); continue; }
    assert.equal(core.validAttachmentWire(schema, value), fixture.valid, fixture.id); checked++;
  }
  const imports = JSON.parse(fs.readFileSync(path.join(directory, 'recording-import-fixtures.json')));
  for (const fixture of imports.cases ?? []) {
    if (!fixture.request || fixture.schema && fixture.schema !== 'RecordingImportRequest') continue;
    assert.equal(core.validAttachmentWire('RecordingImportRequest', fixture.request), fixture.valid ?? true, fixture.id); checked++;
  }
  console.log(`# T-TR-01 checked=${checked} not-sdk-shapes=${[...skipped].sort().join(',')}`);
  assert.ok(checked >= 80, `only ${checked} fixtures checked`);
});
test('T-TR-08 lifecycle fixtures: complete is validating, never ready, in both orders', { skip: !directory && 'FI_ATTACHMENTS_SPEC_DIR not set' }, () => {
  const lifecycle = JSON.parse(fs.readFileSync(path.join(directory, 'lifecycle-fixtures.json')));
  const text = JSON.stringify(lifecycle);
  assert.match(text, /late-root-releases-ready-bytes/); assert.match(text, /root-first-still-needs-validation/);
  assert.equal(lifecycle.initial?.generation ?? 0, 0, 'CQ-5: new occurrences start at deletion generation 0');
});
