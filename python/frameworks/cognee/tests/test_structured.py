"""Synthetic unit tests for the structural-redaction helper."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / "traceai_cognee" / "_structured.py"
spec = importlib.util.spec_from_file_location("cognee_structured", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class TestStructuralRedaction(unittest.TestCase):
    def test_nested_fields_and_counts(self):
        source = {
            "client": {"config": {"apiKey": "fake-secret-123", "model": "demo"}},
            "headers": {"Authorization": "fake-secret-123", "X-Api-Key": "fake-secret-123"},
            "client.config.refreshToken": "fake-secret-123",
            "input_tokens": 42,
            "token_count": 10,
            "nested": json.dumps({"access_token": "fake-secret-123", "value": "kept"}),
        }
        before = copy.deepcopy(source)
        result = module.redact_structured(source)
        self.assertEqual(source, before)
        self.assertNotIn("fake-secret-123", json.dumps(result))
        self.assertEqual(result["input_tokens"], 42)
        self.assertEqual(result["token_count"], 10)
        self.assertEqual(result["client"]["config"]["model"], "demo")
        self.assertEqual(json.loads(result["nested"])["value"], "kept")

    def test_credential_paths_and_usage_metrics(self):
        source = {
            "http.request.header.authorization.value": "fake-secret-123",
            "client[config][apiKey][value]": "fake-secret-123",
            "client_config_api_key": "fake-secret-123",
            "input_token": 5,
            "input_tokens": 10,
            "token_count": 15,
        }
        result = module.redact_structured(source)
        for key in ("http.request.header.authorization.value", "client[config][apiKey][value]", "client_config_api_key"):
            self.assertEqual(result[key], module.REDACTED)
        for key in ("input_token", "input_tokens", "token_count"):
            self.assertEqual(result[key], source[key])

    def test_malformed_and_large_payloads_fail_closed(self):
        self.assertEqual(module.redact_structured('{"api_key":"fake-secret-123"'), module.REDACTED)
        self.assertEqual(module.redact_structured("x" * (module._MAX_TEXT + 1)), module.REDACTED)
        self.assertEqual(module.redact_structured({"x" * 513: "fake-secret-123"}), module.REDACTED)
        self.assertEqual(module.redact_structured({str(i): i for i in range(11000)}), module.REDACTED)

    def test_cycles_and_opaque_values_do_not_inspect_objects(self):
        class Dangerous:
            def __str__(self):
                raise AssertionError("must not call str")
            def __repr__(self):
                raise AssertionError("must not call repr")
        obj = []
        obj.append(obj)
        self.assertEqual(module.redact_structured(obj), module.REDACTED)
        self.assertEqual(module.redact_structured({"obj": Dangerous()}), module.REDACTED)

    def test_free_text_is_not_a_secret_detector(self):
        self.assertEqual(module.redact_structured("fake-secret-123"), "fake-secret-123")


if __name__ == "__main__":
    unittest.main()
