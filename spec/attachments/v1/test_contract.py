"""Contract tests only: no SDK, backend, provider or storage qualification."""
from __future__ import annotations

import copy
import importlib.util
import hashlib
import io
import json
import tempfile
import unittest
import wave
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

    def test_completed_import_has_separate_admission_without_relaxing_uploads(self):
        scenario = contract.load("recording-import-fixtures.json")["base"]
        recording = scenario["request"]["recording"]
        self.assertEqual(recording["declared_bytes"], 172800044)
        contract.validate_wire("ImportRequest", scenario["request"])
        contract.validate_import_admission(scenario)
        old = next(c["value"] for c in contract.load("wire-fixtures.json")["cases"] if c["id"] == "valid-ImportRequest")
        for size, linked in ((172800044, False), (172800044, True), (14400044, True)):
            candidate = copy.deepcopy(old)
            candidate["upload"]["declared_bytes"] = size
            if linked:
                candidate["upload"].update(recording_id=recording["recording_id"], segment_index=0)
            with self.subTest(size=size, linked=linked), self.assertRaises((ValidationError, ValueError)):
                contract.validate_wire("ImportRequest", candidate)
        contract.validate_wire("ImportRequest", old)  # small import compatibility

    def test_import_budgets_deny_before_any_source_effect(self):
        table = contract.load("recording-import-fixtures.json")
        attempted = []
        for case in table["cases"]:
            if case["stage"] != "admission":
                continue
            scenario = contract.fixture_override(table["base"], case["overrides"])
            before = len(attempted)
            try:
                contract.validate_import_admission(scenario)
                attempted.append(case["id"])  # synthetic source read boundary
            except (ValueError, ValidationError):
                pass
            self.assertEqual(len(attempted) - before, int(case["valid"]), case["id"])
        self.assertGreater(len(attempted), 0)

    def test_import_length_and_segment_negative_controls(self):
        table = contract.load("recording-import-fixtures.json")
        for case in table["cases"]:
            if case["stage"] != "completion":
                continue
            scenario = contract.fixture_override(table["base"], case["overrides"])
            with self.subTest(case=case["id"]):
                if case["valid"]:
                    contract.validate_import_completion(scenario)
                else:
                    with self.assertRaises((ValidationError, ValueError)):
                        contract.validate_import_completion(scenario)

    def test_import_requires_all_reservation_scopes_and_known_resources(self):
        base = contract.load("recording-import-fixtures.json")["base"]
        for scope in ("project", "organization", "global"):
            scenario = copy.deepcopy(base)
            del scenario["headroom"][scope]
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                contract.validate_import_admission(scenario)
        for resource in base["headroom"]["project"]:
            scenario = copy.deepcopy(base)
            del scenario["headroom"]["project"][resource]
            with self.subTest(resource=resource), self.assertRaises(ValueError):
                contract.validate_import_admission(scenario)

    def test_one_hour_wav_produces_independently_playable_bounded_segments(self):
        # A real synthetic PCM file exercises the contract's byte/duration math.
        # This stdlib test double does NOT qualify the production codec/importer.
        scenario = contract.load("recording-import-fixtures.json")["base"]
        contract.validate_import_admission(scenario)
        expected_hash = hashlib.sha256()
        actual_hash = hashlib.sha256()
        one_second = b"\x01\x00" * 24000
        with tempfile.TemporaryFile() as source:
            with wave.open(source, "wb") as output:
                output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                for _ in range(3600):
                    output.writeframesraw(one_second)
                    expected_hash.update(one_second)
            scenario["observation"]["source_bytes"] = source.tell()
            source.seek(0)
            # Read the admitted source as <=8 MiB ranges and <=256 KiB buffers.
            reads = []
            total = scenario["observation"]["source_bytes"]
            offset = 0
            while offset < total:
                count = min(contract.import_window(scenario["limits"]), total - offset)
                remaining = count
                while remaining:
                    chunk = source.read(min(262144, remaining))
                    self.assertTrue(chunk)
                    remaining -= len(chunk)
                reads.append({"offset": offset, "bytes": count, "total_bytes": total})
                offset += count
            self.assertEqual(source.read(1), b"")
            scenario["observation"]["reads"] = reads
            source.seek(0)
            sizes = []
            with wave.open(source, "rb") as decoder:
                self.assertEqual((decoder.getnchannels(), decoder.getsampwidth(), decoder.getframerate()), (1, 2, 24000))
                scenario["observation"]["source_duration_ms"] = (decoder.getnframes() * 1000 + 23999) // 24000
                for segment in scenario["observation"]["manifest"]["segments"]:
                    frames = segment["duration_ms"] * 24
                    buffer = io.BytesIO()
                    with wave.open(buffer, "wb") as encoder:
                        encoder.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                        remaining = frames
                        while remaining:
                            chunk = decoder.readframes(min(131072, remaining))
                            self.assertTrue(chunk)
                            self.assertLessEqual(len(chunk), 262144)
                            encoder.writeframesraw(chunk)
                            remaining -= len(chunk) // 2
                    sizes.append(buffer.tell())
                    buffer.seek(0)
                    with wave.open(buffer, "rb") as independent:
                        self.assertEqual(independent.getnframes(), frames)
                        while chunk := independent.readframes(131072):
                            actual_hash.update(chunk)
                self.assertEqual(decoder.readframes(1), b"")
            scenario["observation"]["segment_bytes"] = sizes
        contract.validate_import_completion(scenario)
        self.assertEqual(actual_hash.digest(), expected_hash.digest())
        self.assertEqual(len(sizes), 120)
        self.assertEqual(set(sizes), {1440044})
        print("PCM_IMPORT " + json.dumps({"source_bytes": total, "segments": len(sizes), "segment_bytes": sizes[0], "output_bytes": sum(sizes), "pcm_sha256": actual_hash.hexdigest()}, sort_keys=True))

    def test_security_fixture_covers_every_request_path(self):
        cases = contract.load("security-fixtures.json")["cases"]
        for action in ("GET", "HEAD", "Range", "conditional", "resolve"):
            for fact in ("generation_current", "viewer_authorized", "live_authorization_checked"):
                self.assertTrue(any(case["context"]["action"] == action and not case["context"][fact] and case["expected"]["decision"] == "deny" for case in cases))


if __name__ == "__main__":
    unittest.main(verbosity=2)
