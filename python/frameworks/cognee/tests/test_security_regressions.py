"""Synthetic regressions for the native export projection."""
import json
import math
import unittest

from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_cognee._exporter import project
from traceai_cognee._structured import redact_structured


class TestSecurityRegressions(unittest.TestCase):
    def spans(self, attributes):
        provider = TracerProvider(shutdown_on_exit=False)
        sink = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(sink))
        try:
            for attrs in attributes:
                with provider.get_tracer('synthetic').start_as_current_span(
                    'cognee.observe.acreate_structured_output', attributes=attrs
                ):
                    pass
            return sink.get_finished_spans()
        finally:
            provider.shutdown()

    def test_aws_credential_aliases_and_legitimate_fields(self):
        credential = 'synthetic-aws-credential-should-be-redacted'
        source = {
            'aws_secret_access_key': credential,
            'aws_access_key_id': credential,
            'aws_session_token': credential,
            'awsSecretAccessKey': credential,
            'credentials': {'accessKeyId': credential, 'secretAccessKey': credential},
            'client.aws_secret_access_key.value': credential,
            'client[awsSessionToken][value]': credential,
            'nested': json.dumps({'aws_access_key_id': credential, 'question': 'hello'}),
            'input_token': 7,
            'token_count': 11,
            'request_id': 'request-1',
        }
        before = json.dumps(source)
        sanitized = redact_structured(source)
        self.assertEqual(json.dumps(source), before)
        self.assertNotIn(credential, json.dumps(sanitized))
        self.assertEqual(sanitized['input_token'], 7)
        self.assertEqual(sanitized['token_count'], 11)
        self.assertEqual(sanitized['request_id'], 'request-1')
        self.assertEqual(json.loads(sanitized['nested'])['question'], 'hello')
        (span,) = self.spans([{'langfuse.observation.input': before}])
        projected = project(span, 'synthetic', True)
        self.assertNotIn(credential.encode(), encode_spans([projected]).SerializeToString())
        self.assertEqual(json.loads(projected.attributes['input.value'])['request_id'], 'request-1')

    def test_integer_boundaries_encode_without_raw_value_logs(self):
        for value in (-(2**63), 2**63 - 1, -(2**63) - 1, 2**63, 10**100):
            with self.subTest(value_size=value.bit_length()):
                spans = self.spans([
                    {'gen_ai.usage.input_tokens': value},
                    {'gen_ai.usage.output_tokens': 9},
                ])
                projected = [project(span, 'synthetic', False) for span in spans]
                with self.assertNoLogs('opentelemetry.exporter.otlp.proto.common', level='ERROR'):
                    encoded = encode_spans(projected)
                    encoded.SerializeToString()
                self.assertEqual(sum(len(s.spans) for r in encoded.resource_spans for s in r.scope_spans), 2)
                if -(2**63) <= value < 2**63:
                    self.assertEqual(projected[0].attributes['gen_ai.usage.input_tokens'], value)
                else:
                    self.assertNotIn('gen_ai.usage.input_tokens', projected[0].attributes)
                self.assertEqual(projected[1].attributes['gen_ai.usage.output_tokens'], 9)

    def test_opt_in_numeric_content_is_wire_safe(self):
        for value in (2**63, -(2**63) - 1, math.inf, -math.inf, math.nan):
            with self.subTest(type=type(value).__name__):
                (span,) = self.spans([{'langfuse.observation.output': value}])
                projected = project(span, 'synthetic', True)
                self.assertNotIn('langfuse.observation.output', projected.attributes)
                self.assertNotIn('output.value', projected.attributes)
                with self.assertNoLogs('opentelemetry.exporter.otlp.proto.common', level='ERROR'):
                    encode_spans([projected]).SerializeToString()

    def test_finite_metadata_remains_available(self):
        (span,) = self.spans([{'gen_ai.usage.input_tokens': 13, 'gen_ai.usage.output_tokens': 2.0}])
        projected = project(span, 'synthetic', False)
        self.assertEqual(projected.attributes['gen_ai.usage.input_tokens'], 13)
        self.assertEqual(projected.attributes['gen_ai.usage.output_tokens'], 2.0)


if __name__ == '__main__':
    unittest.main()
