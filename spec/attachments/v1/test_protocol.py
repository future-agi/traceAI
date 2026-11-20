"""Synthetic loopback HTTP contract integration; not a production service test."""
from __future__ import annotations

import importlib.util
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from jsonschema.exceptions import ValidationError

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("attachment_contract", ROOT / "validate.py")
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)

OBJECTS = {case["schema"]: case["value"] for case in contract.load("wire-fixtures.json")["cases"] if case["valid"]}
MEDIA = contract.load("media-session-fixtures.json")["base"]
EXCHANGE_PATH = "/tracer/v1/attachment-media-sessions"
# Deliberately fake identity; this receiver does not qualify application auth.
MEDIA_HEADERS = {"Authorization": "Bearer synthetic_application_identity", "Origin": MEDIA["origin"], **MEDIA["request"]["headers"]}
PROJECT = OBJECTS["BootstrapResponse"]["scope"]["project_id"]
UPLOAD = OBJECTS["UploadResponse"]["upload_id"]
BASE = "/tracer/v1/projects/" + PROJECT + "/attachments"
ROUTES = {
    "/tracer/v1/attachment-sessions": ("BootstrapRequest", "BootstrapResponse", 200),
    BASE + "/uploads": ("UploadRequest", "UploadResponse", 201),
    BASE + "/uploads/" + UPLOAD + "/complete": ("CompleteRequest", "StatusResponse", 202),
    BASE + "/imports": ("ImportRequest", "ImportResponse", 202),
    BASE + "/resolve": ("ResolveRequest", "ResolveResponse", 200),
}


class FixtureReceiver(BaseHTTPRequestHandler):
    """Validate fixture wire requests, returning only checked-in synthetic data."""
    def log_message(self, *args):
        pass

    def do_POST(self):
        if self.path in {EXCHANGE_PATH, BASE + "/resolve"}:
            # Test-only exact header checks, not production authentication.
            if any(self.headers.get_all(key) != [MEDIA_HEADERS[key]] for key in ("Authorization", "Origin")):
                self.send_response(404)
                self.send_header("Cache-Control", "private, no-store")
                self.end_headers()
                return
        if self.path == EXCHANGE_PATH:
            observed = MEDIA["exchange_response"]
            contract.validate_media_session("exchange", MEDIA)
            self.send_response(observed["status"])
            for key, value in observed["safe_headers"].items():
                self.send_header(key, value)
            self.send_header("Set-Cookie", observed["set_cookie"])
            self.send_header("Cache-Control", observed["cache_control"])
            self.end_headers()
            return
        route = ROUTES.get(self.path)
        code = 404
        response = OBJECTS["Error"]
        if route:
            incoming, outgoing, code = route
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ValueError("bounded request")
                value = contract.strict_json(self.rfile.read(length).decode("utf-8"))
                contract.validate_wire(incoming, value)
                if incoming == "ResolveRequest":
                    handles = self.headers.get_all(contract.MEDIA_HANDLE) or []
                    if len(handles) != 1:
                        raise ValueError("one exact handle required")
                    contract.validate_wire("MediaSessionResolveHeaders", {contract.MEDIA_HANDLE: handles[0]})
                    if handles[0] != MEDIA["session"]["id"]:
                        code = 404
                        raise LookupError("unknown selector")
                    contract.validate_media_session("resolve", MEDIA)
                response = OBJECTS[outgoing]
                if incoming == "ImportRequest" and value.get("mode") == "completed_recording":
                    response = OBJECTS["RecordingImportResponse"]
            except (ValueError, ValidationError):
                code = 400
            except LookupError:
                code = 404
        body = json.dumps(response).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureReceiver)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        cls.origin = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(timeout=2)

    def post(self, path, value, *, headers=None):
        headers = MEDIA_HEADERS if headers is None else headers
        request = Request(self.origin + path, data=json.dumps(value).encode(), headers={"Content-Type": "application/json", **headers}, method="POST")
        try:
            response = urlopen(request, timeout=2)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    def test_bootstrap_upload_validation_resolve_composition(self):
        for path, (incoming, outgoing, status) in ROUTES.items():
            with self.subTest(operation=incoming):
                actual_status, headers, raw = self.post(path, OBJECTS[incoming])
                self.assertEqual(actual_status, status)
                self.assertEqual(headers["Cache-Control"], "private, no-store")
                contract.validate_wire(outgoing, json.loads(raw))
        # The complete response remains validating; it is never a playback URL.
        self.assertEqual(OBJECTS["StatusResponse"]["state"], "validating")

    def test_raw_url_secret_fields_rejected_and_not_reflected(self):
        for path, (incoming, _, _) in ROUTES.items():
            value = {**OBJECTS[incoming], "source_url": "https://example.invalid/SYNTHETIC_SECRET?token=SYNTHETIC_SECRET"}
            status, _, raw = self.post(path, value)
            self.assertEqual(status, 400)
            self.assertNotIn(b"SYNTHETIC_SECRET", raw)
            contract.validate_wire("Error", json.loads(raw))

    def test_completed_recording_handoff_does_not_create_large_upload(self):
        request = OBJECTS["RecordingImportRequest"]
        status, headers, raw = self.post(BASE + "/imports", request)
        self.assertEqual(status, 202)
        self.assertEqual(headers["Cache-Control"], "private, no-store")
        response = json.loads(raw)
        contract.validate_wire("RecordingImportResponse", response)
        self.assertEqual(response["recording_id"], request["recording"]["recording_id"])
        for forbidden in ("upload_id", "write_capability", "occurrence_id", "gateway_path"):
            self.assertNotIn(forbidden, response)
        oversized = {**OBJECTS["UploadRequest"], "declared_bytes": request["recording"]["declared_bytes"]}
        self.assertEqual(self.post(BASE + "/uploads", oversized)[0], 400)
        for field in ("source_url", "provider_credential", "filename"):
            denied = {**request, "source": {**request["source"], field: "SYNTHETIC_SECRET"}}
            status, _, raw = self.post(BASE + "/imports", denied)
            self.assertEqual(status, 400)
            self.assertNotIn(b"SYNTHETIC_SECRET", raw)

    def test_exchange_is_204_headers_only_and_resolve_captures_exact_handle(self):
        status, headers, body = self.post(EXCHANGE_PATH, {})
        observed = {"status": status, "body": body.decode(), "safe_headers": {key: headers[key] for key in (contract.MEDIA_HANDLE, contract.MEDIA_EXPIRY)}, "set_cookie": headers["Set-Cookie"], "cache_control": headers["Cache-Control"]}
        contract.validate_exchange_response(observed, MEDIA["session"], MEDIA["now"])
        self.assertNotIn(MEDIA["gateway"]["cookies"][0]["value"], json.dumps(observed["safe_headers"]))
        # X-CSRFToken is intentionally absent; these adapters are bearer-only.
        status, _, body = self.post(BASE + "/resolve", OBJECTS["ResolveRequest"], headers={**MEDIA_HEADERS, contract.MEDIA_HANDLE: headers[contract.MEDIA_HANDLE]})
        self.assertEqual(status, 200)
        contract.validate_resolve_binding(json.loads(body), observed["safe_headers"][contract.MEDIA_HANDLE], observed["safe_headers"][contract.MEDIA_EXPIRY])

    def test_media_issuance_missing_auth_origin_or_handle_fails_closed(self):
        for path in (EXCHANGE_PATH, BASE + "/resolve"):
            body = {} if path == EXCHANGE_PATH else OBJECTS["ResolveRequest"]
            for key in ("Authorization", "Origin"):
                headers = {k: v for k, v in MEDIA_HEADERS.items() if k != key}
                with self.subTest(path=path, missing=key):
                    self.assertEqual(self.post(path, body, headers=headers)[0], 404)
            for origin in ("null", "https://foreign.invalid"):
                self.assertEqual(self.post(path, body, headers={**MEDIA_HEADERS, "Origin": origin})[0], 404)
        for value in (None, "", "synthetic_media_secret", "00000000-0000-0000-0000-000000000000"):
            headers = {k: v for k, v in MEDIA_HEADERS.items() if k != contract.MEDIA_HANDLE}
            if value is not None:
                headers[contract.MEDIA_HANDLE] = value
            status, _, body = self.post(BASE + "/resolve", OBJECTS["ResolveRequest"], headers=headers)
            self.assertEqual(status, 400)
            self.assertNotIn(b"gateway_path", body)
            self.assertNotIn(b"synthetic_media_secret", body)
        foreign = {**MEDIA_HEADERS, contract.MEDIA_HANDLE: "ffffffff-ffff-ffff-ffff-ffffffffffff"}
        self.assertEqual(self.post(BASE + "/resolve", OBJECTS["ResolveRequest"], headers=foreign)[0], 404)

    def test_unknown_version_and_foreign_route_fail(self):
        value = {**OBJECTS["UploadRequest"], "schema_version": 99}
        self.assertEqual(self.post(BASE + "/uploads", value)[0], 400)
        self.assertEqual(self.post(BASE.replace(PROJECT, "ffffffff-ffff-ffff-ffff-ffffffffffff") + "/uploads", OBJECTS["UploadRequest"])[0], 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
