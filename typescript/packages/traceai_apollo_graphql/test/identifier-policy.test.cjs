const { test } = require('node:test');
const assert = require('node:assert/strict');
const { sanitizeSpan } = require('@traceai/apollo-graphql');
const fixture = (name, attributes = {}) => ({
  name, attributes, instrumentationScope: { name: '@opentelemetry/instrumentation-graphql' },
  events: [], links: [], status: { code: 0 },
  spanContext() { return { traceId: 'unchanged-trace', spanId: 'unchanged-span' }; },
});
for (const option of ['hideInputs', 'hideInputText', 'hideInputMessages']) {
  test(`${option} normalizes query identifiers without mutating schema metadata or the original`, () => {
    for (const operation of ['query', 'mutation', 'subscription']) {
      const original = fixture(`${operation} PRIVATE_OPERATION`, {
        'graphql.operation.type': operation, 'graphql.operation.name': 'PRIVATE_OPERATION',
      });
      const hidden = sanitizeSpan(original, { [option]: true, captureContent: true });
      assert.equal(hidden.name, `${operation} __REDACTED__`);
      assert.equal(hidden.attributes['graphql.operation.name'], '__REDACTED__');
      assert.equal(hidden.attributes['graphql.operation.type'], operation);
      assert.equal(original.name, `${operation} PRIVATE_OPERATION`);
      assert.equal(original.attributes['graphql.operation.name'], 'PRIVATE_OPERATION');
      assert.deepEqual(hidden.spanContext(), original.spanContext());
    }
    const original = fixture('graphql.resolve PRIVATE_ALIAS.0.PRIVATE_NESTED_ALIAS', {
      'graphql.field.path': 'PRIVATE_ALIAS.0.PRIVATE_NESTED_ALIAS',
      'graphql.field.name': 'name', 'graphql.parent.name': 'Profile', 'graphql.field.type': 'String!',
    });
    const hidden = sanitizeSpan(original, { [option]: true });
    assert.equal(hidden.name, 'graphql.resolve name');
    assert.deepEqual(hidden.attributes, { ...original.attributes, 'graphql.field.path': '__REDACTED__' });
    assert.equal(original.name, 'graphql.resolve PRIVATE_ALIAS.0.PRIVATE_NESTED_ALIAS');
    assert.equal(original.attributes['graphql.field.path'], 'PRIVATE_ALIAS.0.PRIVATE_NESTED_ALIAS');
    assert.equal(sanitizeSpan(original, { [option]: false }).name, original.name);
    for (const name of ['graphql.parse', 'graphql.validate', 'graphql.parseSchema', 'graphql.execute']) {
      assert.equal(sanitizeSpan(fixture(name), { [option]: true }).name, name);
    }
    for (const name of ['PRIVATE_UNKNOWN_NAME', 'graphql.resolve PRIVATE_ALIAS']) {
      assert.equal(sanitizeSpan(fixture(name), { [option]: true }).name, '__REDACTED__');
    }
    const application = { ...original, instrumentationScope: { name: 'application' } };
    assert.equal(sanitizeSpan(application, { [option]: true }), application);
  });
}
