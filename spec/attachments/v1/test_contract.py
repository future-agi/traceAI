"""Contract tests only: no SDK, backend, provider or storage qualification."""
from __future__ import annotations

import copy
import importlib.util
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from openapi_spec_validator import validate as validate_openapi

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("attachment_fixture_validator", ROOT / "validate.py")
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)


class ContractTests(unittest.TestCase):
    def test_all_shared_fixture_expectations(self):
        counts = contract.validate_all()
        self.assertGreater(counts["privacy"], 70)
        self.assertGreater(counts["security"], 150)
        print("FIXTURE_COUNTS " + json.dumps(counts, sort_keys=True))

    def test_openapi_resolves_all_local_shapes(self):
        validate_openapi(contract.load("openapi.json"), base_uri=(ROOT / "openapi.json").as_uri())

    def test_all_definitions_are_valid_schemas(self):
        for name, shape in contract.SCHEMA["$defs"].items():
            with self.subTest(name=name):
                Draft202012Validator.check_schema(shape)

    def test_every_wire_object_rejects_unknown_fields_and_missing_required_fields(self):
        for case in contract.load("wire-fixtures.json")["cases"]:
            if not case["valid"] or not isinstance(case["value"], dict):
                continue
            shape = contract.SCHEMA["$defs"][case["schema"]]
            with self.subTest(schema=case["schema"], change="unknown"):
                with self.assertRaises((ValidationError, ValueError)):
                    contract.validate_wire(case["schema"], {**case["value"], "secret": "SYNTHETIC_CANARY"})
            for field in shape.get("required", []):
                value = {key: item for key, item in case["value"].items() if key != field}
                with self.subTest(schema=case["schema"], missing=field):
                    with self.assertRaises((ValidationError, ValueError)):
                        contract.validate_wire(case["schema"], value)

    def test_every_privacy_and_security_rule_has_a_sensitive_negative_control(self):
        # Deleting each rule MUST change an expected denial, otherwise its fixtures
        # are vacuous or shadowed and do not protect that normative requirement.
        for prefix in ("privacy", "security"):
            table = contract.load(prefix + "-table.json")
            cases = contract.load(prefix + "-fixtures.json")["cases"]
            for index, rule in enumerate(table["rules"]):
                mutant = {**table, "rules": table["rules"][:index] + table["rules"][index + 1:]}
                with self.subTest(table=prefix, rule=rule["id"]):
                    self.assertTrue(any(contract.decide(mutant, case["context"]) != case["expected"] for case in cases))

    def test_denied_contract_fixture_has_zero_source_and_sink_effects(self):
        # This is a contract double, not evidence that a production transport
        # enforces these boundaries. Every SDK/backend lane must reuse the cases.
        table = contract.load("privacy-table.json")
        cases = contract.load("privacy-fixtures.json")["cases"]
        attempted = []
        for case in cases:
            result = contract.decide(table, case["context"])
            effects = ["SYNTHETIC_CANARY:" + effect for effect in result["effects"]]
            if case["expected"]["decision"] == "deny":
                self.assertEqual(effects, [], case["id"])
            else:
                attempted.extend(effects)
        self.assertGreater(len(attempted), 0)  # negative control: sink is observable

    def test_unknown_and_nonboolean_policy_facts_fail_closed(self):
        table = contract.load("privacy-table.json")
        allowed = contract.load("privacy-fixtures.json")["cases"][0]["context"]
        for key in allowed:
            context = dict(allowed)
            del context[key]
            self.assertEqual(contract.decide(table, context)["decision"], "deny", key)
        for value in (None, "false", 0, 1):
            self.assertEqual(contract.decide(table, {**allowed, "enabled": value})["decision"], "deny")
        self.assertEqual(contract.decide(table, {**allowed, "new_unknown_policy": True})["decision"], "deny")

    def test_policy_tightening_discards_and_reenable_does_not_restore(self):
        table = contract.load("privacy-table.json")
        context = contract.load("privacy-fixtures.json")["cases"][0]["context"]
        owned = None
        # Test-only queue oracle. A later allow is not permission to resurrect
        # bytes discarded by a prior tightening; only new explicit capture can.
        for phase, allowed in (("admission", True), ("retry", False), ("retry", True)):
            decision = contract.decide(table, {**context, "server_allowed": allowed})
            if decision["decision"] == "deny":
                owned = None
            elif phase == "admission":
                owned = b"synthetic"
        self.assertIsNone(owned)

    def test_span_projection_validates_inner_json_not_only_content_annotation(self):
        attrs = next(case["value"] for case in contract.load("wire-fixtures.json")["cases"] if case["schema"] == "SpanAttributes" and case["valid"])
        for inner in ("not-json", "{}", '[{"source_url":"SYNTHETIC_CANARY"}]', "null"):
            with self.subTest(inner=inner):
                with self.assertRaises((ValueError, ValidationError)):
                    contract.validate_wire("SpanAttributes", {**attrs, "fi.attachment.refs": inner})
        duplicate = json.loads(attrs["fi.attachment.refs"]) * 2
        with self.assertRaises((ValueError, ValidationError)):
            contract.validate_wire("SpanAttributes", {**attrs, "fi.attachment.refs": json.dumps(duplicate)})
        with self.assertRaises((ValueError, ValidationError)):
            contract.validate_wire("SpanAttributes", {**attrs, "fi.attachment.refs": " " * 16385})

    def test_strict_json_rejects_duplicate_members_and_non_json_numbers(self):
        for raw in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                contract.strict_json(raw)
        self.assertEqual(contract.strict_json('{"x":1}'), {"x": 1})

    def test_reference_and_mime_byte_limits(self):
        value = next(case["value"] for case in contract.load("wire-fixtures.json")["cases"] if case["schema"] == "UploadRequest" and case["valid"])
        for changes in ({"modality": "image"}, {"modality": "image", "mime_type": "image/png", "declared_bytes": 20971521}, {"recording_id": value["attachment_id"], "segment_index": 0, "declared_bytes": 8388609}):
            with self.subTest(changes=changes):
                with self.assertRaises((ValueError, ValidationError)):
                    contract.validate_wire("UploadRequest", {**value, **changes})

    def test_trace_bits_and_scoped_hex_uuid_equivalence(self):
        for value in ("f" * 32, "1" * 32, "0123456789abcdef0123456789abcdef"):
            result = contract.normalize_trace(value)
            self.assertEqual(result.replace("-", ""), value)
        table = contract.load("scope-fixtures.json")
        canonical = {**table["base"], "trace_id": contract.normalize_trace(table["base"]["trace_id"])}
        self.assertEqual(contract.scoped_key(table["base"], table["key_fields"]), contract.scoped_key(canonical, table["key_fields"]))
        for invalid in ("0" * 16, "A" * 16, "1" * 17, "1" * 16 + "\n"):
            self.assertFalse(contract.validator("SpanID").is_valid(invalid))

    def test_recording_truth_and_channel_mapping(self):
        value = next(case["value"] for case in contract.load("wire-fixtures.json")["cases"] if case["schema"] == "RecordingManifest" and case["valid"])
        invalid = [
            {**value, "capture_complete": True},
            {**value, "layout": "stereo"},
            {**value, "duration_ms": 500},
            {**value, "segments": value["segments"] * 2},
            {**value, "tracks": [{**value["tracks"][0], "direction": "output"}]},
            {**value, "tracks": [{**value["tracks"][0], "channels": [1]}]},
        ]
        for index, item in enumerate(invalid):
            with self.subTest(index=index), self.assertRaises(ValueError):
                contract.validate_wire("RecordingManifest", item)
        stereo = copy.deepcopy(value)
        stereo.update(layout="stereo", channels=2)
        stereo["tracks"] = [
            {**value["tracks"][0], "channels": [0], "direction": "output", "role": "assistant"},
            {**value["tracks"][0], "track_id": "ffffffff-ffff-ffff-ffff-ffffffffffff", "channels": [1], "direction": "input", "role": "user"},
        ]
        contract.validate_wire("RecordingManifest", stereo)  # left is not assumed user

    def test_write_capabilities_never_gain_read_operations(self):
        cases = contract.load("wire-fixtures.json")["cases"]
        denied = [case for case in cases if case["id"].startswith("write-capability-forbids-")]
        self.assertEqual(len(denied), 6)
        for case in denied:
            with self.assertRaises(ValidationError):
                contract.validate_wire(case["schema"], case["value"])

    def test_protocol_composition_uses_same_occurrence_not_bare_blob(self):
        objects = {case["schema"]: case["value"] for case in contract.load("wire-fixtures.json")["cases"] if case["valid"]}
        occurrence_id = objects["UploadRequest"]["occurrence_id"]
        self.assertEqual(objects["UploadResponse"]["occurrence_id"], occurrence_id)
        self.assertEqual(objects["Occurrence"]["reference"]["occurrence_id"], occurrence_id)
        self.assertEqual(objects["MessageURI"]["uri"], "fi-attachment:" + occurrence_id)
        self.assertEqual(objects["ResolveRequest"]["items"][0]["occurrence_id"], occurrence_id)
        self.assertIn('/' + occurrence_id + '/', objects["ResolveResponse"]["items"][0]["gateway_path"])
        self.assertNotEqual(occurrence_id, objects["UploadRequest"]["attachment_id"])

    def test_security_fixture_covers_every_request_path(self):
        cases = contract.load("security-fixtures.json")["cases"]
        for action in ("GET", "HEAD", "Range", "conditional", "resolve"):
            for fact in ("generation_current", "viewer_authorized", "live_authorization_checked"):
                self.assertTrue(any(case["context"]["action"] == action and not case["context"][fact] and case["expected"]["decision"] == "deny" for case in cases))


if __name__ == "__main__":
    unittest.main(verbosity=2)
