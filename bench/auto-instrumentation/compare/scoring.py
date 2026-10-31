"""Export-only, value-checked semantic rubric. No SDK expectations masquerade as data."""
from __future__ import annotations

import base64
import copy
import json
import math

from mocks import AUDIO, BT_KEY, FI_KEY, FI_SECRET, INPUT, MEDIA_B64, SECRET, TEXT

SCENARIOS = ("openai_chat", "openai_stream", "anthropic_stream", "gemini", "embeddings", "tool_calls",
             "transcription", "tts", "langchain", "vercel", "raw_http", "unsupported_sdk", "openai_error")
STREAMS = {"openai_stream", "anthropic_stream"}
MANUAL = {"raw_http", "unsupported_sdk"}
TIMING_TOLERANCE_NS = 3_000_000
MIN_STREAM_GAP_NS = 10_000_000
# These paths establish units only, NEVER semantic meaning. Unknown units abstain.
STREAM_SECONDS_FIELDS = {"metrics.time_to_first_token", "gen_ai.response.time_to_first_chunk",
                         "gen_ai.response.time_to_first_token"}


def stream_timing(span, tool, call=None, contracts=None):
    """Validate exported elapsed timings against two distinct application events.

    A mock pause before text separates the first SDK item from semantic text.
    Reviewed implementation trigger contracts are necessary: a first-body/item
    timer remains TTFC even when buffering makes it coincide with text. Only a
    source-verified text-aware trigger can earn semantic TTFT, and its value must
    also align with separated application observations. Unknown source/units,
    coincident references and out-of-window semantic timings abstain. This tests
    the fixture's behavior, not universal semantic correctness.
    """
    root = span.get("attributes", {}) if tool == "traceai" else span
    first = (call or {}).get("first_chunk_unix_ns")
    text = (call or {}).get("first_text_unix_ns")
    tolerance = TIMING_TOLERANCE_NS + (call or {}).get("span_clock_tolerance_ns", 0)
    separated = (finite_number(first) and finite_number(text)
                 and text - first >= max(MIN_STREAM_GAP_NS, 2 * tolerance + 1))
    start, end = span_times(span, tool)
    candidates = []
    for key, value in flatten(root):
        if not any(name in key for name in ("time_to_first", "ttft", "first_token_latency")):
            continue
        trigger = (contracts or {}).get(key, {}).get("trigger", "unverified")
        result = {"field": key, "value": value, "trigger_semantics": trigger,
                  "classification": "unresolved", "alignment": "unresolved"}
        if key not in STREAM_SECONDS_FIELDS:
            result["reason"] = "unknown units/origin; no semantic credit"
        elif not finite_number(value) or value <= 0 or start <= 0:
            result["reason"] = "invalid elapsed value or span origin"
        else:
            event_ns = start + value * 1e9
            result["event_unix_ns"] = event_ns
            in_span = start - tolerance <= event_ns <= end + tolerance
            if separated:
                result.update({"first_item_distance_us": (event_ns - first) / 1000,
                               "semantic_text_distance_us": (event_ns - text) / 1000})
                if abs(event_ns - first) <= tolerance:
                    result["alignment"] = "first_chunk_or_item"
                elif abs(event_ns - text) <= tolerance:
                    result["alignment"] = "semantic_text"
            if not in_span:
                result["reason"] = "timing outside exported span"
            elif trigger == "first_chunk_or_item":
                result["classification"] = "first_chunk_or_item"
                result["reason"] = "reviewed body/item trigger; buffering/text coincidence is NOT semantic TTFT"
            elif trigger == "semantic_text" and result["alignment"] == "semantic_text":
                result["classification"] = "semantic_ttft"
            else:
                result["reason"] = "requires reviewed semantic trigger AND separated text alignment"
        candidates.append(result)
    return {"reference_gap_us": (text - first) / 1000 if separated else None,
            "tolerance_us": tolerance / 1000, "candidates": candidates,
            "ttfc": any(c["classification"] == "first_chunk_or_item" for c in candidates),
            "ttft": any(c["classification"] == "semantic_ttft" for c in candidates)}


def spec(scenario):
    model = {"gemini": "gemini-2.0-flash", "embeddings": "text-embedding-3-small",
             "transcription": "whisper-1", "tts": "tts-1", "anthropic_stream": "claude-sonnet-4-5",
             "openai_error": "compare-error"}.get(scenario, "gpt-4o-mini")
    provider = {"gemini": "google", "anthropic_stream": "anthropic", "unsupported_sdk": "together", "raw_http": "openai_compatible"}.get(scenario, "openai")
    required = ["model", "provider", "input", "latency", "error_status"]
    if scenario != "openai_error":
        required.append("output")
    if scenario not in ("tts", "transcription", "openai_error"):
        required.append("token_usage")
    if scenario in STREAMS:
        required.append("ttft")
    if scenario == "tool_calls":
        required.append("tool_calls")
    if scenario in ("transcription", "tts"):
        required += ["audio_transcript", "audio_duration"]
    return {"model": model, "provider": provider, "required": required}


def merge(a, b):
    for key, value in b.items():
        if isinstance(value, dict) and isinstance(a.get(key), dict):
            merge(a[key], value)
        else:
            a[key] = copy.deepcopy(value)
    return a


def merge_bt(rows):
    """Braintrust exports partial rows keyed by id; count records, not HTTP batches."""
    combined = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        merge(combined.setdefault(row["id"], {}), row)
    return [r for r in combined.values() if r.get("span_id") or r.get("span_attributes") or "metrics" in r]


def flatten(value, prefix=""):
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            decoded = json.loads(value)
        except ValueError:
            decoded = value
        if not isinstance(decoded, str):
            yield from flatten(decoded, prefix)
            return
    if isinstance(value, dict):
        for key, item in value.items():
            yield from flatten(item, prefix + "." + str(key) if prefix else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from flatten(item, prefix + "." + str(index))
    else:
        yield prefix.lower(), value


def finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def span_times(span, tool):
    if tool == "traceai":
        return span.get("start_ns", 0), span.get("end_ns", 0)
    metrics = span.get("metrics", {})
    return tuple(int(metrics.get(k, 0) * 1e9) for k in ("start", "end"))


def evidence(span, tool, scenario):
    s = spec(scenario)
    root = span.get("attributes", {}) if tool == "traceai" else span
    leaves = list(flatten(root))
    inputs = [(k, v) for k, v in leaves if (k.startswith(("input", "gen_ai.input", "llm.input", "prompt"))
                                           or k.startswith("gen_ai.prompt"))]
    outputs = [(k, v) for k, v in leaves if k.startswith(("output", "gen_ai.output", "gen_ai.completion", "llm.output"))]
    def has(items, needle):
        return any(needle in str(v) for _, v in items)
    def numeric(items, pred, expected=None):
        return any(pred(k) and finite_number(v) and (v == expected if expected is not None else v > 0)
                   for k, v in items)
    model_values = [v for k, v in leaves if "model" in k and isinstance(v, str)]
    model = any(v == s["model"] or (s["model"] == "gpt-4o-mini" and v.startswith("gpt-4o-mini-")) for v in model_values)
    provider_values = [str(v).lower() for k, v in leaves if k in ("gen_ai.system", "gen_ai.provider.name", "llm.system")
                       or k.endswith((".provider", ".model_provider", ".provider_name"))]
    aliases = {"google": {"google", "google_genai", "google-genai", "gemini", "google.genai"},
               "openai": {"openai"}, "openai_compatible": {"openai_compatible"},
               "anthropic": {"anthropic"}, "together": {"together", "together_ai", "togetherai"}}
    provider = bool(set(provider_values) & aliases[s["provider"]])
    is_input = has(inputs, INPUT)
    if scenario == "transcription":
        # File metadata counts as input; raw media is NOT required for completeness.
        is_input = has(inputs, "compare.wav") or any("audio" in k or "file" in k for k, _ in inputs)
    output = has(outputs, TEXT)
    if scenario == "tool_calls":
        output = has(outputs, "lookup_weather") and has(outputs, "Paris")
    if scenario == "embeddings":
        output = (numeric(leaves, lambda k: k == "gen_ai.embeddings.dimension.count" or
                           (k.startswith("output") and ("dim" in k or "embedding_length" in k)), 3)
                  or (numeric(outputs, lambda k: True, 0.1) and numeric(outputs, lambda k: True, 0.3)))
    if scenario == "tts":
        output = any("audio" in k or "byte" in k or "attachment" in str(v) for k, v in outputs)
        output |= numeric(leaves, lambda k: "audio" in k and "byte" in k, len(AUDIO))
    input_tokens = numeric(leaves, lambda k: k.endswith(("input_tokens", "prompt_tokens", "prompttokencount")), 11)
    output_tokens = numeric(leaves, lambda k: k.endswith(("output_tokens", "completion_tokens", "candidatestokencount")), 7)
    tokens = input_tokens and (scenario == "embeddings" or output_tokens)
    start, end = span_times(span, tool)
    latency = bool(start > 0 and end >= start)
    # Coverage assigns TTFT only after validating against the call's two events.
    # A positive token-named field alone (including Braintrust's) is not evidence.
    ttft = False
    if tool == "traceai":
        code = span.get("status", {}).get("code", 0)
        error = code in (2, "ERROR", "STATUS_CODE_ERROR")
        success = code in (0, 1, "UNSET", "OK", "STATUS_CODE_UNSET", "STATUS_CODE_OK")
    else:
        error = bool(span.get("error")) or any(k.endswith("error") and bool(v) for k, v in leaves)
        success = latency and not error
    audio_transcript = has(outputs, TEXT) or has([(k, v) for k, v in leaves if "transcript" in k], TEXT)
    if scenario == "tts":
        audio_transcript = has(inputs, INPUT) or has([(k, v) for k, v in leaves if "transcript" in k], INPUT)
    duration = numeric(leaves, lambda k: "duration" in k or k.endswith(".seconds"), 0.1)
    return {"model": model, "provider": provider, "input": is_input, "output": output,
            "token_usage": tokens, "tool_calls": has(outputs, "lookup_weather") and has(outputs, "Paris"),
            "latency": latency, "ttft": ttft, "error_status": error if scenario == "openai_error" else success,
            "audio_transcript": audio_transcript, "audio_duration": duration}


def privacy(exports):
    bodies = [base64.b64decode(e["body_b64"]) for e in exports]
    # Authentication headers to the designated collector are intentionally NOT
    # treated as payload leaks. Request headers are not stored.
    credentials = {name: any(secret.encode() in b for b in bodies) for name, secret in (
        ("provider", SECRET), ("traceai_api", FI_KEY), ("traceai_secret", FI_SECRET), ("braintrust", BT_KEY))}
    needles = (b"COMPARE_RAW_MEDIA_7831", MEDIA_B64.encode(), base64.b64encode(b"COMPARE_RAW_MEDIA_7831"))
    return {"observed_exports": len(exports), "credentials_in_payload": any(credentials.values()),
            "credential_canaries": credentials, "raw_media_in_payload": any(n in b for n in needles for b in bodies),
            "interpretation": "canary scan of exported bodies/attachments, not absence-of-all-secrets proof; zero exports is unobserved"}


def coverage(tool, scenario, calls, spans, bt_rows, exports, timing_contracts=None):
    if tool == "baseline":
        return {"spans": len(spans) + len(merge_bt(bt_rows)), "score": None, "privacy": privacy(exports)}
    actual = spans if tool == "traceai" else merge_bt(bt_rows)
    required = spec(scenario)["required"]
    fields_hit = {k: 0 for k in required}
    per_call, mapped = [], set()
    for call in calls:
        matched = []
        for i, span in enumerate(actual):
            start, _ = span_times(span, tool)
            tolerance = call.get("span_clock_tolerance_ns", 0)
            if call["start_unix_ns"] - tolerance <= start <= call["end_unix_ns"] + tolerance:
                matched.append(span); mapped.add(i)
        combined = {k: False for k in required}
        timings = []
        for span in matched:
            seen = evidence(span, tool, scenario)
            if scenario in STREAMS:
                timing = stream_timing(span, tool, call, timing_contracts)
                seen["ttft"] = timing["ttft"]
                timings.append({"span_id": span.get("span_id"), **timing})
            for k in required:
                combined[k] |= seen[k]
        for k, yes in combined.items():
            fields_hit[k] += int(yes)
        per_call.append({"i": call["i"], "span_count": len(matched), "fields": combined,
                         "stream_timing": timings})
    denom = len(calls) * len(required)
    return {"spans": len(actual), "calls_with_spans": sum(c["span_count"] > 0 for c in per_call),
            "unmapped_spans": len(actual) - len(mapped), "required_fields": required,
            "field_hits": fields_hit, "score": sum(fields_hit.values()) / denom if denom else None,
            "per_call": per_call, "stream_timing": {
                "first_chunk_calls": sum(any(t["ttfc"] for t in c["stream_timing"]) for c in per_call),
                "semantic_ttft_calls": sum(any(t["ttft"] for t in c["stream_timing"]) for c in per_call),
                "unresolved_calls": sum(not any(t["ttfc"] or t["ttft"] for t in c["stream_timing"]) for c in per_call),
                "policy": "Reviewed source trigger + known units/origin + separated app text alignment required for semantic TTFT. A body/item trigger NEVER gets TTFT even if buffering coincides with text. Unknown source abstains."} if scenario in STREAMS else None,
            "privacy": privacy(exports)}


def safe_ratio(numerator, denominator):
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator
