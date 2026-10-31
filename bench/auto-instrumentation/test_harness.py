"""Small stdlib self-tests for the harness oracle/OTLP receiver, not agent tests."""
import base64
import copy
import gzip
import http.client
import json
import unittest

from measure import correctness, paired, stats
from mock_servers import MODEL, RESPONSE_MODEL, LocalMocks, decode_otlp, output_for, payload_for


def uint(value):
    data = bytearray()
    while value >= 128:
        data.append((value & 127) | 128); value >>= 7
    data.append(value)
    return bytes(data)


def message(number, value):
    if isinstance(value, str): value = value.encode()
    return uint((number << 3) | 2) + uint(len(value)) + value


def proto_span():
    attrs = b""
    for key, value in expected_attributes().items():
        any_value = uint(3 << 3) + uint(value) if isinstance(value, int) else message(1, value)
        attrs += message(9, message(1, key) + message(2, any_value))
    return (message(1, b"a"*16) + message(2, b"b"*8) + message(5, "chat " + MODEL) + attrs
            + message(15, uint(3 << 3) + uint(1)))


def expected_attributes():
    return {"gen_ai.request.model": MODEL, "gen_ai.response.model": RESPONSE_MODEL,
            "gen_ai.response.id": "chatcmpl-round0-0-0", "gen_ai.usage.input_tokens": 11,
            "gen_ai.usage.output_tokens": 7, "gen_ai.usage.total_tokens": 18,
            "output.value": output_for("round0-0-0"),
            "gen_ai.output.messages.0.message.content": output_for("round0-0-0")}


def json_export():
    attributes = [{"key": key, "value": {"intValue": str(value)} if isinstance(value,int) else {"stringValue": value}}
                  for key,value in expected_attributes().items()]
    return json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [{"name": "chat " + MODEL,
        "traceId": (b"a"*16).hex(), "spanId": (b"b"*8).hex(), "attributes": attributes, "status": {"code": 1}}]}]}]}).encode()


def valid_run():
    return {"exit_code": 0, "timed_out": False, "startup_us": 10, "peak_rss_bytes": 1024, "protocol_errors": [], "stderr_log": "unit-test",
            "records": [{"kind": "ready"}, {"kind": "call", "id": "round0-0-0", "stream": False, "warmup": False,
                         "latency_us": 10, "ttfc_us": 5, "body_b64": base64.b64encode(b"".join(payload_for("round0-0-0", False))).decode()}, {"kind": "done"}],
            "provider_requests": [{"ai": True, "call_id": "round0-0-0", "path": "/v1/chat/completions", "model": MODEL, "stream": False},
                                  {"ai": False, "method": "GET", "path": "/health"},
                                  {"ai": False, "method": "POST", "path": "/v1/chat/completions"}],
            "spans": decode_otlp(json_export(), "application/json"), "collector_errors": [], "first_trace_us": 100}


class HarnessTests(unittest.TestCase):
    def test_json_and_protobuf_have_identical_normalized_spans(self):
        binary = message(1, message(2, message(2, proto_span())))
        self.assertEqual(decode_otlp(binary, "application/x-protobuf"), decode_otlp(json_export(), "application/json"))

    def test_chunked_gzip_otlp_receiver(self):
        with LocalMocks(0) as mocks:
            body = gzip.compress(json_export())
            client = http.client.HTTPConnection("127.0.0.1", mocks.collector.server_port, timeout=3)
            client.request("POST", "/tracer/v1/traces", body=iter([body[:17], body[17:]]),
                           headers={"Content-Type": "application/json", "Content-Encoding": "gzip"}, encode_chunked=True)
            response = client.getresponse()
            self.assertEqual(response.status, 200); response.read(); client.close()
            self.assertEqual(len(mocks.spans), 1)
            self.assertIsNotNone(mocks.first_span_ns)
            self.assertEqual(mocks.collector_errors, [])

    def test_valid_oracle(self):
        self.assertTrue(correctness(valid_run(), True, ["round0-0-0"])["passed"])

    def test_wrong_tokens_fail(self):
        run = valid_run(); run["spans"][0]["attributes"]["gen_ai.usage.output_tokens"] = 8
        self.assertFalse(correctness(run, True, ["round0-0-0"])["passed"])

    def test_wrong_output_fail(self):
        run = valid_run(); run["spans"][0]["attributes"]["output.value"] = "wrong"
        self.assertFalse(correctness(run, True, ["round0-0-0"])["passed"])

    def test_duplicate_export_fails(self):
        run = valid_run(); run["spans"].append(copy.deepcopy(run["spans"][0]))
        self.assertFalse(correctness(run, True, ["round0-0-0"])["passed"])

    def test_duplicate_logical_call_with_different_span_id_fails(self):
        run = valid_run(); extra = copy.deepcopy(run["spans"][0]); extra["span_id"] = "other"
        run["spans"].append(extra)
        self.assertFalse(correctness(run, True, ["round0-0-0"])["passed"])

    def test_non_ai_span_fails(self):
        run = valid_run(); run["spans"].append({"name": "HTTP GET", "span_id": "other", "trace_id": "other", "attributes": {}, "status": {"code":1}})
        self.assertFalse(correctness(run, True, ["round0-0-0"])["passed"])

    def test_missing_span_fails(self):
        run = valid_run(); run["spans"] = []
        self.assertFalse(correctness(run, True, ["round0-0-0"])["passed"])

    def test_byte_corruption_fails(self):
        run = valid_run(); run["records"][1]["body_b64"] = base64.b64encode(b"corrupt").decode()
        self.assertFalse(correctness(run, True, ["round0-0-0"])["passed"])

    def test_off_run_must_not_export(self):
        self.assertFalse(correctness(valid_run(), False, ["round0-0-0"])["passed"])

    def test_empty_startup_must_not_make_http_requests(self):
        run = valid_run(); run["records"] = [{"kind":"ready"}]; run["spans"] = []
        self.assertFalse(correctness(run, True, [], startup=True)["passed"])

    def test_sse_bytes_reassemble_despite_split_unicode(self):
        frames = payload_for("round0-1-0", True)
        self.assertIn(output_for("round0-1-0"), "reply/round0-1-0/café/終")
        self.assertIn("café/終", b"".join(frames).decode())
        with self.assertRaises(UnicodeDecodeError): frames[4].decode()

    def test_negative_paired_deltas_are_retained(self):
        result = paired([10,20],[8,19])
        self.assertEqual(result["overhead"]["p50"], -1.5)
        self.assertAlmostEqual(stats([0,100])["p95"],95)

    def test_truncated_protobuf_rejected(self):
        with self.assertRaises(ValueError): decode_otlp(b"\x0a\xff", "application/x-protobuf")
