"""Runs every shared golden fixture through the wire-layer core."""

import pytest

from auto_fixture_loader import assert_expected, load_fixtures, request_body, response_chunks, response_arrivals
from fi_instrumentation.auto.wire import _core

FIXTURES = load_fixtures()


def test_fixtures_exist():
    # All 100 current goldens are mandatory; additive spec fixtures run automatically.
    assert len(FIXTURES) >= 100
    assert len({fixture["name"] for fixture in FIXTURES}) == len(FIXTURES)


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f["name"] for f in FIXTURES])
def test_wire_fixture(fixture):
    spec_req, spec_resp = fixture["request"], fixture["response"]
    headers, body = request_body(spec_req)
    req = _core.RequestData(spec_req["method"], spec_req["url"], headers, body, unavailable=spec_req.get("unavailable", False))
    resp_headers, chunks = response_chunks(spec_resp)
    resp = _core.ResponseData(status=spec_resp["status"], headers=resp_headers, size=sum(map(len, chunks)))
    resp.chunk_arrivals = response_arrivals(spec_resp, chunks)
    if resp.chunk_arrivals:
        resp.first_chunk_seconds = resp.chunk_arrivals[0][1]
    if "error_after_chunks" in spec_resp:
        resp.error = TimeoutError("stream timed out")
    if not _core.is_binary_content_type(_core._header(resp_headers, "content-type")):
        resp.body = b"".join(chunks)

    found = _core.match(req.method, req.url)
    result = _core.build(found, req, resp) if found else None
    if not fixture["expect"]["captured"]:
        assert result is None, f"{fixture['name']}: captured {result and result.name}"
        return
    assert result is not None, f"{fixture['name']}: not captured"
    assert_expected(fixture, result.name, result.attributes, result.ok, result.description)
