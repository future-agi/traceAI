"""Offline stdlib-only regression tests, run by compare/run.py --self-test."""
import base64
import gzip
import json
import tempfile
import time
import unittest
from pathlib import Path
import urllib.request

from mocks import AUDIO, INPUT, SECRET, TEXT, CompareMocks, encode, otlp_with_times
from run import (SCHEMA_VERSION, SCRATCH, TOOLS, counterbalanced, execution_orders, finalize_scenario,
                 harness_hashes, summarize_repetitions, validate_append, variant)
from scoring import coverage, evidence, merge_bt, privacy, safe_ratio, stream_timing


def post(url, body, headers=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, data=body, headers=headers or {"Content-Type": "application/json"})
    with opener.open(request, timeout=5) as response:
        return response.read()


class OracleTests(unittest.TestCase):
    def test_value_checks_no_output_as_input(self):
        span = {"attributes": {"gen_ai.request.model": "wrong", "output.value": TEXT,
                               "gen_ai.usage.input_tokens": 12, "gen_ai.usage.output_tokens": 7},
                "status": {"code": 1}, "start_ns": 1, "end_ns": 2}
        ev = evidence(span, "traceai", "openai_chat")
        self.assertFalse(ev["model"])
        self.assertFalse(ev["input"])
        self.assertFalse(ev["token_usage"])
        self.assertFalse(ev["provider"])
        self.assertTrue(ev["output"])

    def test_json_otlp_timing_and_native_merge(self):
        body = encode({"resourceSpans": [{"scopeSpans": [{"spans": [{"name": "chat", "spanId": "123", "traceId": "456",
            "startTimeUnixNano": "100", "endTimeUnixNano": "200", "attributes": [{"key": "gen_ai.request.model", "value": {"stringValue": "gpt-4o-mini"}}]}]}]}]})
        decoded = otlp_with_times(body, "application/json")
        self.assertEqual((decoded[0]["start_ns"], decoded[0]["end_ns"]), (100, 200))
        rows = [{"id": "a", "span_id": "b", "metrics": {"start": 1}},
                {"id": "a", "_is_merge": True, "metrics": {"end": 2}, "output": TEXT}]
        merged = merge_bt(rows)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["metrics"], {"start": 1, "end": 2})

    def test_missing_duplicate_and_unmapped_spans(self):
        calls = [{"i": 0, "start_unix_ns": 10, "end_unix_ns": 20}]
        span = {"attributes": {}, "status": {}, "start_ns": 11, "end_ns": 15}
        cov = coverage("traceai", "openai_chat", calls, [span, span, {**span, "start_ns": 1}], [], [])
        self.assertEqual(cov["spans"], 3)
        self.assertEqual(cov["unmapped_spans"], 1)
        self.assertLess(cov["score"], 1)
        empty = coverage("traceai", "openai_chat", calls, [], [], [])
        self.assertEqual(empty["score"], 0)
        self.assertEqual(empty["privacy"]["observed_exports"], 0)

    def test_millisecond_export_clock_is_not_previous_call(self):
        calls = [{"i": 0, "start_unix_ns": 10100000, "end_unix_ns": 11900000, "span_clock_tolerance_ns": 1000000},
                 {"i": 1, "start_unix_ns": 16100000, "end_unix_ns": 17900000, "span_clock_tolerance_ns": 1000000}]
        spans = [{"attributes": {}, "status": {}, "start_ns": t, "end_ns": t + 1000000} for t in (10000000, 16000000)]
        cov = coverage("traceai", "openai_chat", calls, spans, [], [])
        self.assertEqual([c["span_count"] for c in cov["per_call"]], [1, 1])
        self.assertEqual(cov["calls_with_spans"], 2)

    def test_embedding_summary_and_error_status(self):
        bt = {"metadata": {"model": "text-embedding-3-small", "provider": "openai"}, "input": INPUT,
              "output": {"embedding_length": 3}, "metrics": {"start": 1, "end": 2, "prompt_tokens": 11}}
        self.assertTrue(evidence(bt, "braintrust", "embeddings")["output"])
        bt["output"]["embedding_length"] = 4
        self.assertFalse(evidence(bt, "braintrust", "embeddings")["output"])
        self.assertFalse(evidence(bt, "braintrust", "openai_error")["error_status"])
        bt["error"] = "deliberate 400"
        self.assertTrue(evidence(bt, "braintrust", "openai_error")["error_status"])

    def test_semantic_ttft_uses_events_not_product_or_field_names(self):
        call = {"i": 0, "start_unix_ns": 1_000_000_000, "end_unix_ns": 1_200_000_000,
                "first_chunk_unix_ns": 1_010_000_000, "first_text_unix_ns": 1_040_000_000}
        for tool in ("traceai", "braintrust"):
            for field in ("time_to_first_chunk", "time_to_first_token"):
                for elapsed, classification in ((.010, "first_chunk_or_item"), (.040, "semantic_ttft"), (.025, "unresolved")):
                    with self.subTest(tool=tool, field=field, elapsed=elapsed):
                        if tool == "traceai":
                            span = {"start_ns": 1_000_000_000, "end_ns": 1_200_000_000,
                                    "attributes": {"gen_ai.response." + field: elapsed}}
                        else:
                            # Only documented native seconds fields establish units.
                            if field == "time_to_first_chunk":
                                continue
                            span = {"id": "a", "span_id": "a", "metrics": {"start": 1, "end": 1.2, field: elapsed}}
                        key = ("gen_ai.response." if tool == "traceai" else "metrics.") + field
                        # Synthetic reviewed contracts let this test swap field names
                        # without granting meaning from either product's spelling.
                        trigger = "first_chunk_or_item" if elapsed == .010 else "semantic_text"
                        contracts = {key: {"trigger": trigger, "review_basis": "synthetic test trigger"}}
                        timing = stream_timing(span, tool, call, contracts)
                        self.assertEqual(timing["candidates"][0]["classification"], classification)
                        self.assertEqual(timing["ttft"], classification == "semantic_ttft")
                        self.assertFalse(evidence(span, tool, "openai_stream")["ttft"])
                        args = ([span], []) if tool == "traceai" else ([], [span])
                        cov = coverage(tool, "openai_stream", [call], *args, [], timing_contracts=contracts)
                        self.assertEqual(cov["field_hits"]["ttft"], int(classification == "semantic_ttft"))

    def test_buffered_first_chunk_coincidence_is_never_semantic_ttft(self):
        call = {"first_chunk_unix_ns": 1_010_000_000, "first_text_unix_ns": 1_040_000_000}
        for tool, key in (("traceai", "gen_ai.response.time_to_first_chunk"),
                          ("braintrust", "metrics.time_to_first_token")):
            span = ({"start_ns": 1_000_000_000, "end_ns": 1_200_000_000, "attributes": {key: .040}}
                    if tool == "traceai" else {"metrics": {"start": 1, "end": 1.2, "time_to_first_token": .040}})
            observed = stream_timing(span, tool, call, {key: {"trigger": "first_chunk_or_item"}})
            self.assertEqual(observed["candidates"][0]["alignment"], "semantic_text")
            self.assertTrue(observed["ttfc"])
            self.assertFalse(observed["ttft"])
            # Even perfect numerical alignment is insufficient without a reviewed
            # trigger contract; unknown implementations fail closed on semantics.
            self.assertFalse(stream_timing(span, tool, call)["ttft"])

    def test_ttft_abstains_for_missing_buffered_unknown_or_invalid_timings(self):
        span = {"start_ns": 1_000_000_000, "end_ns": 1_200_000_000,
                "attributes": {"gen_ai.response.time_to_first_token": .040}}
        contracts = {"gen_ai.response.time_to_first_token": {"trigger": "semantic_text"}}
        for call in (None, {}, {"first_chunk_unix_ns": 1_039_000_000, "first_text_unix_ns": 1_040_000_000}):
            self.assertFalse(stream_timing(span, "traceai", call, contracts)["ttft"])
        call = {"first_chunk_unix_ns": 1_010_000_000, "first_text_unix_ns": 1_040_000_000}
        for attrs in ({"ttft": 40}, {"gen_ai.response.time_to_first_token": -1},
                      {"gen_ai.response.time_to_first_token": float("nan")},
                      {"gen_ai.response.time_to_first_token": .4}):
            self.assertFalse(stream_timing({**span, "attributes": attrs}, "traceai", call, contracts)["ttft"])

    def test_six_permutation_schedule_balances_positions_and_precedence(self):
        for cycles in (1, 2):
            orders = execution_orders(cycles)
            self.assertTrue(counterbalanced(orders))
            self.assertEqual(len({tuple(o) for o in orders}), 6)
            for i in range(0, len(orders), 2):
                self.assertEqual(orders[i], orders[i + 1][::-1])
        self.assertFalse(counterbalanced([list(TOOLS)]))
        self.assertFalse(counterbalanced([list(TOOLS), list(reversed(TOOLS))] * 3))
        self.assertFalse(counterbalanced([]))

    def test_repetition_aggregation_pairs_only_within_matching_baseline(self):
        repetitions = []
        for i, order in enumerate(execution_orders()):
            repeat = {"index": i, "order": order}
            for tool, extra in zip(TOOLS, (0, 10, 30)):
                calls = [{"i": 0, "start_unix_ns": 10, "end_unix_ns": 20, "warmup": False,
                          "latency_us": 100 + i * 100 + extra, "ttft_us": None, "ttfc_us": None, "response": "same"}]
                repeat[tool] = {"calls": calls, "errors": [], "status": "measured", "startup_us": 1000 + i + extra,
                                "peak_rss_bytes": 10000 + i + extra, "exports": [],
                                "setup": variant("python", tool, "openai_chat")[1],
                                "coverage": coverage(tool, "openai_chat", calls, [], [], [])}
            finalize_scenario(repeat)
            repetitions.append(repeat)
        summary = summarize_repetitions(repetitions)
        self.assertTrue(summary["performance_eligible"])
        for tool, extra in zip(TOOLS, (0, 10, 30)):
            self.assertEqual(summary[tool]["overhead"]["latency_us"]["overhead"]["p50"], extra)
            self.assertEqual(summary[tool]["overhead"]["latency_us"]["overhead"]["p99"], extra)
            self.assertEqual(summary[tool]["overhead"]["startup_us"]["p50"], extra)
            self.assertEqual(summary[tool]["coverage"]["score"], None if tool == "baseline" else 0)
        self.assertEqual(summary["ratios"]["positive_added_latency_p50"], 3)
        incomplete = summarize_repetitions(repetitions[:2])
        self.assertFalse(incomplete["performance_eligible"])
        self.assertIsNone(incomplete["ratios"]["latency_p50"])
        self.assertIsNone(incomplete["ratios"]["positive_added_latency_p50"])

    def test_append_fingerprint_includes_all_shared_helpers(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
            parent = Path(temp); compare = parent / "compare"; compare.mkdir()
            for name in ("run.py", "app.mjs", "requirements-python.txt", "package.json", "package-lock.json"):
                (compare / name).write_text("fixture")
            for name in ("measure.py", "mock_servers.py", "safety.py"):
                (parent / name).write_text("original")
            hashes = harness_hashes(compare, parent)
            previous = {"schema_version": SCHEMA_VERSION, "config": {}, "host": {}, "source_hashes": hashes}
            validate_append(previous, {}, {}, hashes)
            for name in ("measure.py", "mock_servers.py", "safety.py"):
                self.assertIn(name, hashes)
                (parent / name).write_text("changed")
                with self.assertRaisesRegex(ValueError, "shared helpers"):
                    validate_append(previous, {}, {}, harness_hashes(compare, parent))
                (parent / name).write_text("original")

    def test_mock_role_and_header_items_are_separated_from_semantic_text(self):
        with CompareMocks(delay_ms=1) as mocks:
            for endpoint in ("chat/completions", "messages"):
                body = encode({"model": "gpt-4o-mini", "messages": [{"role": "user", "content": INPUT}], "stream": True})
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                request = urllib.request.Request(mocks.provider_url + "/v1/" + endpoint, body,
                                                 {"Content-Type": "application/json"})
                first_item = first_text = None
                with opener.open(request, timeout=5) as response:
                    for line in response:
                        if not line.startswith(b"data: {"):
                            continue
                        event = json.loads(line[6:])
                        now = time.perf_counter_ns()
                        if first_item is None:
                            first_item = now
                        text = event.get("delta", {}).get("text") or next((
                            c.get("delta", {}).get("content") for c in event.get("choices", []) if c.get("delta", {}).get("content")), None)
                        if text and first_text is None:
                            first_text = now
                self.assertIsNotNone(first_item)
                self.assertIsNotNone(first_text)
                self.assertGreater(first_text - first_item, 10_000_000)

    def test_privacy_and_ratios(self):
        p = privacy([{"body_b64": base64.b64encode(SECRET.encode() + AUDIO).decode()}])
        self.assertTrue(p["credentials_in_payload"])
        self.assertTrue(p["raw_media_in_payload"])
        self.assertIsNone(safe_ratio(10, 0))
        self.assertIsNone(safe_ratio(10, None))
        self.assertEqual(safe_ratio(10, 2), 5)

    def test_setup_cost_actual_diff(self):
        for language in ("python", "node"):
            self.assertEqual(variant(language, "traceai", "openai_chat")[1]["application_lines_changed"], 0)
            native = variant(language, "braintrust", "openai_chat")[1]
            manual = variant(language, "braintrust", "raw_http")[1]
            self.assertEqual(native["application_lines_changed"], 4 if language == "python" else 3)
            self.assertEqual(manual["application_lines_changed"], native["application_lines_changed"] + 1)
            self.assertIn("metadata", manual["setup_code"])
            if language == "node":
                self.assertIn("event:{metadata:", manual["setup_code"])

    def test_loopback_provider_and_gzip_log_receiver(self):
        with CompareMocks(delay_ms=0) as mocks:
            value = json.loads(post(mocks.provider_url + "/v1/chat/completions", encode({"model": "gpt-4o-mini", "messages": [{"role": "user", "content": INPUT}]})))
            self.assertEqual(value["choices"][0]["message"]["content"], TEXT)
            body = encode({"rows": [{"id": "a", "span_id": "b", "input": INPUT, "output": TEXT}]})
            post(mocks.collector_url + "/logs3", gzip.compress(body), {"Content-Type": "application/json", "Content-Encoding": "gzip"})
            login = json.loads(post(mocks.collector_url + "/api/apikey/login", b"{}"))
            self.assertEqual(login["org_info"][0]["api_url"], mocks.collector_url)
        self.assertFalse(mocks.collector_errors)
        self.assertEqual(len(mocks.bt_rows), 1)
        self.assertTrue(mocks.provider_url.startswith("http://127.0.0.1:"))

    def test_stream_and_audio_fixtures(self):
        with CompareMocks(delay_ms=0) as mocks:
            stream = post(mocks.provider_url + "/v1/chat/completions", encode({"model": "gpt-4o-mini", "messages": [{"role": "user", "content": INPUT}], "stream": True}))
            self.assertIn(b"[DONE]", stream)
            self.assertIn('café'.encode(), stream)
            audio = post(mocks.provider_url + "/v1/audio/speech", encode({"model": "tts-1", "input": INPUT}))
            self.assertEqual(audio, AUDIO)


if __name__ == "__main__":
    unittest.main()
