"""Every row of the shared plain-channel fixture, run through the code, must match the file."""
import hashlib
import json

import pytest

import plaintext_rule_rows as rows_module

ROWS = json.loads(rows_module.ROWS_FILE.read_text(encoding="utf-8"))["rows"]


def test_the_fixture_matches_its_checksum():
    digest = hashlib.sha256(rows_module.ROWS_FILE.read_bytes()).hexdigest()
    assert rows_module.SUMS_FILE.read_text().split() == [digest, "plaintext_rule_rows.json"]


def test_the_fixture_covers_every_transport_host_group_source_opt_in_and_proxy_cell():
    assert {row["transport"] for row in ROWS} == {"grpc", "http", "eval-config"}
    assert {row["host_group"] for row in ROWS} == set(rows_module.HOSTS["grpc"]) | set(rows_module.HOSTS["http"])
    assert {row["source"] for row in ROWS if row["transport"] == "grpc"} == set(rows_module.GRPC_SOURCES)
    assert {row["opt_in"] for row in ROWS} == {None, "1", "true"}
    assert {row["proxy"] for row in ROWS} == set(rows_module.PROXIES)
    assert [row["id"] for row in ROWS] == [spec["id"] for spec in rows_module.row_specs()]


def test_no_row_breaks_the_invariants():
    assert [(row["id"], problem) for row in ROWS for problem in rows_module.check_invariants(row, row["expect"])] == []


@pytest.mark.parametrize("row", ROWS, ids=[row["id"] for row in ROWS])
def test_a_row_runs_to_the_outcome_the_file_names(row):
    if row["transport"] == "grpc":
        pytest.importorskip("grpc")
        pytest.importorskip("opentelemetry.exporter.otlp.proto.grpc.trace_exporter")
    spec = {key: value for key, value in row.items() if key != "expect"}
    assert rows_module.run_row(spec) == row["expect"]
