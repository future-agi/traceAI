"""S6 generator/catalog and sync regression tests. No SDK code is imported."""

import contextlib
import importlib.util
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


generator = load("privacy_fixture_generator", SPEC / "generate_privacy_fixtures.py")
sync = load("privacy_catalog_sync", SPEC / "sync.py")


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads((SPEC / "privacy.json").read_text())
        cls.reference = generator.Reference(cls.catalog)

    def test_all_golden_cases_and_coverage(self):
        with contextlib.redirect_stdout(io.StringIO()):
            generator.check_cases(self.reference, generator.golden_cases(self.catalog))

    def test_wire_no_false_positives(self):
        with contextlib.redirect_stdout(io.StringIO()):
            generator.check_wire(self.reference)

    def test_every_exclusion_example_matches_its_rule(self):
        import re
        for exclusion in self.catalog["credentials"]["exclusions"]:
            for example in exclusion["examples"]:
                with self.subTest(rule=exclusion["id"], example=example):
                    self.assertIsNotNone(re.fullmatch(exclusion["regex"], generator.normalize(example)))
                    self.assertFalse(self.reference.credential_name(example))

    def test_pattern_examples_match_their_own_rule(self):
        for pattern in self.catalog["credentials"]["patterns"]:
            self.assertIn(pattern["contains"], generator.normalize(pattern["example"]))
            self.assertTrue(self.reference.credential_name(pattern["example"]))

    def test_usage_values_survive_every_flattening(self):
        names = ["max_tokens", "input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens",
                 "cached_tokens", "reasoning_tokens", "token_count", "token_counts", "cachedContentTokenCount", "thoughtsTokenCount"]
        for name in names:
            for path in (name, "gen_ai.usage." + name, "gen_ai.usage." + name + ".0", "gen_ai.usage." + name + ".reasoning"):
                with self.subTest(path=path):
                    self.assertFalse(self.reference.credential_name(path))
                    self.assertEqual(123, self.reference.scrub(123, path))
        self.assertTrue(self.reference.credential_name("usage.api_key"))
        self.assertTrue(self.reference.credential_name("secret.input_tokens"))

    def test_metric_exclusions_never_defeat_independent_credential_markers(self):
        secret = "SYNTHETIC_PRIVATE_20261030"
        for name in ("client_secret_tokens", "authorization_tokens", "api_key_tokens", "password_token_count"):
            with self.subTest(name=name):
                self.assertTrue(self.reference.credential_name(name))
                self.assertFalse(self.reference.excluded(name))
                self.assertEqual({name: "<redacted>"}, self.reference.scrub({name: secret}))
                self.assertEqual((secret,), self.reference.harvest({"json": {name: secret}}))
                self.assertEqual((secret,), self.reference.harvest({"headers": {name: secret}}))
        for exclusion in self.catalog["credentials"]["exclusions"]:
            for marker in exclusion.get("unless_contains_patterns", ()):
                for suffix in ("tokens", "tokens_details", "token_details", "token_count", "token_counts"):
                    self.assertTrue(self.reference.credential_name(marker + "_" + suffix))

    def test_catalog_matches_generator_source(self):
        self.assertEqual(generator.privacy_catalog(), self.catalog)

    def test_token_timing_values_and_types_survive_every_flattening(self):
        names = ("time_to_first_token", "time_to_first_tokens", "time_per_output_token", "time_per_token",
                 "inter_token_latency", "token_latency", "tokens_per_second", "output_tokens_per_second",
                 "ms_per_token", "seconds_per_token")
        for name in names:
            for prefix in ("", "gen_ai.response.", "gen_ai.server.", "gen_ai_response_", "gen_ai_server_"):
                for spelling in (name, name.upper(), name.replace("_", "-"), generator.normalize(name)):
                    path = prefix + spelling
                    for value in (0, 17, 0.125):
                        with self.subTest(path=path, value=value):
                            self.assertFalse(self.reference.credential_name(path))
                            actual = self.reference.scrub(value, path)
                            self.assertEqual(value, actual)
                            self.assertIs(type(value), type(actual))
                            self.assertEqual({path: value}, self.reference.scrub({path: value}))
                            self.assertEqual(value, self.reference.scrub(value, path + "[0]"))
            native = {"gen_ai": {"response": {name: 0.125}}}
            self.assertEqual(native, self.reference.scrub(native))
            # A string measurement must not seed S5's process-wide secret registry either.
            for site in ("headers", "json"):
                self.assertEqual((), self.reference.harvest({site: {name: "123456789.125"}}))

    def test_token_timing_guards_and_site_coverage(self):
        import re
        exclusion = next(item for item in self.catalog["credentials"]["exclusions"] if item["id"] == "token-timing")
        sites = {"request_headers", "request_query", "request_body", "response_body",
                 "attribute_keys", "flattened_paths", "indexed_paths"}
        cases = generator.golden_cases(self.catalog)
        for i, name in enumerate(exclusion["examples"]):
            with self.subTest(example=name):
                pinned = [case for case in cases if case["name"].startswith(f"exclusion-token-timing-{i}-")]
                self.assertEqual(sites, {case["input"]["site"] for case in pinned})
                self.assertTrue(self.reference.credential_name("secret." + name))
                self.assertTrue(self.reference.credential_name(name + ".api_key"))
        for marker in exclusion["unless_contains_patterns"]:
            for name in generator.TOKEN_TIMING_NAMES:
                candidate = "provider_" + marker + "_" + name
                with self.subTest(marker=marker, name=name):
                    # Actually exercise the guard, not a name outside the exclusion regex.
                    self.assertIsNotNone(re.fullmatch(exclusion["regex"], generator.normalize(candidate)))
                    self.assertFalse(self.reference.excluded(candidate))
                    self.assertTrue(self.reference.credential_name(candidate))
                    prefix = f"metric-guard-token-timing-{marker}-_{name}-"
                    pinned = [case for case in cases if case["name"].startswith(prefix)]
                    self.assertEqual(sites, {case["input"]["site"] for case in pinned})
        for name in ("token", "access_token", "x-access-token", "session_token", "token_secret",
                     "api_token_latency_key", "auth_token"):
            with self.subTest(credential=name):
                self.assertTrue(self.reference.credential_name(name))
                self.assertEqual("<redacted>", self.reference.scrub(0.125, name))
                prefix = "timing-real-credential-" + name + "-"
                self.assertEqual(sites, {case["input"]["site"] for case in cases if case["name"].startswith(prefix)})

    def test_logprob_tokens_masked_and_ordinary_names_preserved(self):
        value = {"choices": [{"logprobs": {"content": [{"token": "Hello", "logprob": -0.2,
                 "top_logprobs": [{"token": "world", "logprob": -0.3}]}]}}],
                 "author_name": "Ada", "keywords": ["ordinary", "words"], "signal_strength": 0.7}
        expected = {"choices": [{"logprobs": {"content": [{"token": "<redacted>", "logprob": -0.2,
                    "top_logprobs": [{"token": "<redacted>", "logprob": -0.3}]}]}}],
                    "author_name": "Ada", "keywords": ["ordinary", "words"], "signal_strength": 0.7}
        self.assertEqual(expected, self.reference.scrub(value))
        self.assertEqual((), self.reference.harvest({"json": value}))
        # Literal census, not an empty loop over the now-removed exclusions.
        for path in ("choices.0.logprobs.content.0.token", "choices.0.logprobs.content.0.top_logprobs.0.token",
                     "logprobs.token", "access_key_id", "aws_access_key_id"):
            self.assertTrue(self.reference.credential_name(path))
            self.assertEqual("<redacted>", self.reference.scrub("Hello", path))
            self.assertTrue(self.reference.credential_name("secret." + path))
            self.assertTrue(self.reference.credential_name(path + ".api_key"))
        self.assertTrue(self.reference.credential_name("token"))
        self.assertTrue(self.reference.credential_name("choices.0.logprobs.content.0.client_secret_tokens"))

    def test_audio_content_camel_and_snake_case_any_size(self):
        for name in ("audioContent", "audio_content"):
            value = {name: "AQIDBA=="}
            self.assertEqual({name: "<omitted: 8 base64 chars>"}, self.reference.scrub(value))
            self.assertEqual("<omitted: 8 base64 chars>", self.reference.scrub("AQIDBA==", "input." + name + ".0"))

    def test_payload_descendants_retain_context_nested_and_flattened(self):
        for value in ({"mime_type": "audio/pcm", "content": {"chunk": "AQIDBA=="}},
                      {"input_audio": {"data": {"chunk": "AQIDBA=="}}}):
            actual = self.reference.scrub(value)
            self.assertNotIn("AQIDBA==", json.dumps(actual))
            self.assertIn("<omitted: 8 base64 chars>", json.dumps(actual))
        siblings = {"parts.0.mime_type": "audio/pcm"}
        self.assertEqual("<omitted: 8 base64 chars>", self.reference.scrub("AQIDBA==", "parts.0.content.chunk", siblings))
        self.assertEqual("AQIDBA==", self.reference.scrub("AQIDBA==", "parts.1.content.chunk", siblings))
        self.assertEqual("<omitted: media>", self.reference.scrub([1, 2, 3], "input_audio.data.chunk"))

    def test_nested_audio_fields_preserve_explicit_payload_ancestry(self):
        for field in self.catalog["media"]["audio"]["fields"] + ["audioContent", "audio_content"]:
            for sample, replacement in (("AQIDBA==", "<omitted: 8 base64 chars>"), (7, "<omitted: media>"),
                                        ("raw audio bytes", "<omitted: media>")):
                with self.subTest(field=field, sample=sample):
                    value = {"audio": {"data": {field: {"source": [sample]}}}}
                    clean_source = replacement if isinstance(sample, int) else [replacement]
                    expected = {"audio": {"data": {field: {"source": clean_source}}}}
                    self.assertEqual(expected, self.reference.scrub(value))
                    for path in ("audio.data." + field + ".source.0", "audio.data[0]." + field + "[0].source[0]"):
                        self.assertEqual(replacement, self.reference.scrub(sample, path))
                    self.assertEqual(expected, self.reference.scrub(expected))
        # An audio object alone supplies context, not inherited payload ancestry.
        self.assertEqual("AQIDBA==", self.reference.scrub("AQIDBA==", "audio.source.0"))
        self.assertEqual(7, self.reference.scrub(7, "audio.source.0"))

    def test_mime_audio_fields_remain_payload_through_named_wrappers(self):
        from urllib.parse import urlencode
        for field in ("audio_base64", "audioBase64"):
            for sample, replacement in (("AQIDBA==", "<omitted: 8 base64 chars>"), (7, "<omitted: media>")):
                with self.subTest(field=field, sample=sample):
                    value = {"mime_type": "audio/pcm", field: {"source": [sample]}}
                    clean_source = replacement if isinstance(sample, int) else [replacement]
                    expected = {"mime_type": "audio/pcm", field: {"source": clean_source}}
                    self.assertEqual(expected, self.reference.scrub(value))
                    self.assertEqual(replacement, self.reference.scrub(sample, field + ".source[0]",
                                                                      {"mime_type": "audio/pcm"}))
                    # Query values remain strings, so their omission length is the
                    # encoded character count even when the native sample is numeric.
                    pairs = [(field + ".source.0", str(sample)), ("mime_type", "audio/pcm")]
                    url = "https://example.invalid/?" + urlencode(pairs) + "&model=keep#anchor"
                    expected_url = ("https://example.invalid/?" + field + ".source.0=" + generator.omitted(str(sample))
                                    + "&mime_type=audio%2Fpcm&model=keep#anchor")
                    self.assertEqual(expected_url, self.reference.scrub(url))

    def test_nested_audio_payload_queries_match_native_paths(self):
        from urllib.parse import urlencode
        for field in self.catalog["media"]["audio"]["fields"] + ["audioContent", "audio_content"]:
            for sample in ("AQIDBA==", "7"):
                path = "audio.data." + field + ".source.0"
                url = "https://example.invalid/?" + urlencode([(path, sample)]) + "&model=keep#anchor"
                expected = "https://example.invalid/?" + path + "=" + generator.omitted(sample) + "&model=keep#anchor"
                with self.subTest(path=path, sample=sample):
                    self.assertEqual(expected, self.reference.scrub(url))
                    self.assertEqual({"url": expected}, self.reference.scrub({"url": url}))
                    self.assertEqual("see " + expected + " next", self.reference.scrub("see " + url + " next"))
                    self.assertEqual(expected, self.reference.scrub(expected))

    def test_nested_audio_metadata_resets_only_inherited_payload(self):
        for boundary in self.catalog["media"]["audio"]["metadata_fields"]:
            with self.subTest(boundary=boundary):
                path = "audio.data.audio.source." + boundary
                self.assertEqual("spoken words", self.reference.scrub("spoken words", path + ".content"))
                self.assertEqual(7, self.reference.scrub(7, path + ".content.0"))
                self.assertEqual("<omitted: 8 base64 chars>",
                                 self.reference.scrub("AQIDBA==", path + ".audio.data.audio.source.0"))
                self.assertEqual("<redacted>", self.reference.scrub("synthetic", path + ".api_key"))

    def test_metadata_boundaries_stop_media_not_credential_recognition(self):
        value = {"audio": {"transcript": {"content": "spoken words", "api_key": "synthetic-private"},
                           "data": {"chunk": "AQIDBA=="}}}
        expected = {"audio": {"transcript": {"content": "spoken words", "api_key": "<redacted>"},
                              "data": {"chunk": "<omitted: 8 base64 chars>"}}}
        self.assertEqual(expected, self.reference.scrub(value))
        self.assertEqual("spoken words", self.reference.scrub("spoken words", "audio.transcript.content"))
        self.assertEqual("spoken words", self.reference.scrub("spoken words", "parts.0.transcript.content",
                                                              {"parts.0.mime_type": "audio/pcm"}))

    def test_array_and_indexed_mime_hints_bind_to_exact_parent(self):
        for siblings in ({"parts.0.mimeType": ["audio/pcm"]}, {"parts[0].mimeType[0]": "audio/pcm"}):
            self.assertEqual("<omitted: 8 base64 chars>", self.reference.scrub("AQIDBA==", "parts.0.content.chunk", siblings))
            self.assertEqual("AQIDBA==", self.reference.scrub("AQIDBA==", "parts.1.content.chunk", siblings))

    def test_query_question_marks_observation_export_parity(self):
        secret = "SYNTHETIC_PRIVATE_20261030"
        for name in ("client_secret?tag", "password?foo", "token?filter"):
            url = "https://example.invalid/?" + name + "=" + secret
            with self.subTest(name=name):
                self.assertTrue(self.reference.query_name(name))
                self.assertEqual((secret,), self.reference.harvest({"url": url}))
                # No known-secret seed: returned URLs must be safe on their own.
                self.assertEqual("https://example.invalid/?" + name + "=<redacted>", self.reference.scrub(url))

    def test_query_apostrophes_observation_export_parity(self):
        from urllib.parse import quote
        secret = "SYNTHETIC_PRIVATE_20261030"
        for name in ("client_secret'tag", "password'foo", "token'filter"):
            self.assertTrue(self.reference.query_name(name))
            for raw_name in (name, quote(name, safe="")):
                for base in ("https://example.invalid/", "/path"):
                    url = base + "?" + raw_name + "=" + secret + "&model=keep#fragment?token=ordinary"
                    # The credential-named pair after the '#' is replaced too; in prose it runs to the end of the text.
                    expected = base + "?" + raw_name + "=<redacted>&model=keep#fragment?token=<redacted>"
                    with self.subTest(name=raw_name, base=base):
                        self.assertEqual((secret,), self.reference.harvest({"url": url}))
                        self.assertEqual(expected, self.reference.scrub(url))
                        self.assertEqual({"url": expected}, self.reference.scrub({"url": url}))
                        self.assertEqual("see " + expected, self.reference.scrub("see " + url + " next"))
                        self.assertEqual(expected, self.reference.scrub(expected))

    def test_query_apostrophe_values_never_retain_credential_suffix(self):
        secret = "SYNTHETIC_PRIVATE_20261030"
        for credential in ("'" + secret, secret + "'suffix", secret + "'", "'" + secret + "'"):
            url = "https://example.invalid/?token=" + credential + "&model=keep#anchor"
            expected = "https://example.invalid/?token=<redacted>&model=keep#anchor"
            with self.subTest(credential=credential):
                self.assertEqual((credential,), self.reference.harvest({"url": url}))
                self.assertEqual(expected, self.reference.scrub(url))
                self.assertNotIn(secret, self.reference.scrub(url))

    def test_single_quoted_url_value_runs_to_the_end(self):
        # A value that starts with a quote is not a whole URL value; the pair rule carries the token value to the end of the
        # text, the closing quote included.
        url = "'https://example.invalid/?token='SYNTHETIC_PRIVATE_20261030'"
        expected = "'https://example.invalid/?token=<redacted>"
        self.assertEqual(expected, self.reference.scrub(url))
        self.assertEqual(expected, self.reference.scrub(expected))
        ordinary = "https://example.invalid/?model=author's-model&key_id=public"
        self.assertEqual(ordinary, self.reference.scrub(ordinary))

    def test_query_decoded_data_uris_match_scalar_scrubbing(self):
        from urllib.parse import quote
        samples = (
            ("data:audio/wav;base64,AQIDBA==", "<omitted: 8 base64 chars>"),
            ("data:image/svg+xml,%3Csvg%3E", "<omitted: data URI>"),
            ("data:,literal-payload", "<omitted: data URI>"),
            ("data:audio/wav;base64,Q%2BQ%3D", "<omitted: 4 base64 chars>"),
            ("data:audio/wav;base64,QUJD\r\nRA==", "<omitted: 10 base64 chars>"),
            ("DATA:audio/wav;BASE64,", "<omitted: 0 base64 chars>"),
        )
        for value, replacement in samples:
            with self.subTest(value=value):
                self.assertEqual(replacement, self.reference.scrub(value))
                self.assertEqual({"blob": replacement}, self.reference.scrub({"blob": value}))
                url = "https://example.invalid/?blob=" + quote(value, safe="") + "&model=keep#anchor"
                expected = "https://example.invalid/?blob=" + replacement + "&model=keep#anchor"
                self.assertEqual(expected, self.reference.scrub(url))
                self.assertEqual({"url": expected}, self.reference.scrub({"url": url}))
                self.assertEqual(expected, self.reference.scrub(expected))

    def test_query_data_uri_decoded_payload_uses_exact_value_boundary(self):
        from urllib.parse import quote
        for value in ("data:image/svg+xml,<svg>literal</svg>", "data:,literal'payload", "data:,literal payload"):
            url = "https://example.invalid/?blob=" + quote(value, safe="") + "&model=keep#anchor"
            expected = "https://example.invalid/?blob=<omitted: data URI>&model=keep#anchor"
            with self.subTest(value=value):
                self.assertEqual(expected, self.reference.scrub(url))
                self.assertEqual(expected, self.reference.scrub(expected))

    def test_query_literal_data_uris_preserve_parameter_boundaries(self):
        for value, replacement in (("data:audio/wav;base64,AQIDBA==", "<omitted: 8 base64 chars>"),
                                   ("data:image/svg+xml,%3Csvg%3E", "<omitted: data URI>"),
                                   ("data:,literal-payload", "<omitted: data URI>")):
            url = "https://example.invalid/?blob=" + value + "&model=keep#anchor"
            expected = "https://example.invalid/?blob=" + replacement + "&model=keep#anchor"
            with self.subTest(value=value):
                self.assertEqual(expected, self.reference.scrub(url))
                self.assertEqual(expected, self.reference.scrub(expected))

    def test_exact_url_values_frame_queries_before_delimiter_scanning(self):
        from urllib.parse import quote
        samples = ('data:audio/basic,A"SYNTHETIC_MEDIA_20261030',
                   "data:audio/basic,A'SYNTHETIC_MEDIA_20261030",
                   "data:,literal payload\nmore payload",
                   'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg">literal</svg>',
                   "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg'>literal</svg>",
                   "data:,literal\\payload")
        for value in samples:
            for base in ("https://example.invalid/path", "/path", ""):
                for raw_value in (value, quote(value, safe="")):
                    url = base + "?blob=" + raw_value + "&model=keep#anchor"
                    expected = base + "?blob=<omitted: data URI>&model=keep#anchor"
                    with self.subTest(value=raw_value, base=base):
                        for path in ("", "http.url", "input.url", "parts.0.url"):
                            self.assertEqual(expected, self.reference.scrub(url, path))
                        self.assertEqual({"url": expected}, self.reference.scrub({"url": url}))
                        self.assertEqual([expected], self.reference.scrub([url]))
                        self.assertEqual(expected, self.reference.scrub(expected))
                        self.assertEqual((), self.reference.harvest({"url": url}))

    def test_url_prefixed_prose_scans_later_credential_urls_at_every_value_site(self):
        secret = "SYNTHETIC_PRIVATE_20261030"
        for first in ("https://example.invalid/#fragment", "https://example.invalid/?model=keep",
                      "/path#fragment", "?model=keep"):
            for separator in (" then ", "\nnext\t", ' "see" ', " <see> "):
                value = first + separator + "https://example.invalid/?token=" + secret + "&model=keep#anchor"
                # Prose: the token value runs to the end of the text.
                expected = first + separator + "https://example.invalid/?token=<redacted>"
                with self.subTest(first=first, separator=separator):
                    for path in ("", "exception.message", "output.value", "parts.0.message"):
                        self.assertEqual(expected, self.reference.scrub(value, path))
                    self.assertEqual({"message": expected}, self.reference.scrub({"message": value}))
                    self.assertEqual([expected], self.reference.scrub([value]))
                    self.assertEqual({"messages": [{"text": expected}]},
                                     self.reference.scrub({"messages": [{"text": value}]}))
                    self.assertEqual(expected, self.reference.scrub(value, framed=False))
                    self.assertEqual(expected, self.reference.scrub(expected))

    def test_url_prefixed_prose_scans_later_audio_urls_with_query_context(self):
        for first in ("https://example.invalid/#fragment", "https://example.invalid/?model=keep"):
            for query in ("audio=AQIDBA==", "mime_type=audio%2Fpcm&content=AQIDBA==",
                          "parts.0.content.chunk=AQIDBA==&parts.0.mime_type=audio%2Fpcm",
                          "type=input_audio&data=AQIDBA=="):
                value = first + " then https://example.invalid/?" + query + "&model=keep#anchor"
                expected = value.replace("AQIDBA==", "<omitted: 8 base64 chars>")
                with self.subTest(first=first, query=query):
                    self.assertEqual(expected, self.reference.scrub(value))
                    self.assertEqual(expected, self.reference.scrub(value, "exception.message"))
                    self.assertEqual({"message": [expected]}, self.reference.scrub({"message": [value]}))
                    self.assertEqual(expected, self.reference.scrub(value, framed=False))
                    self.assertEqual(expected, self.reference.scrub(expected))

    def test_url_prefix_alone_never_frames_surrounding_prose_as_query_values(self):
        # The audio value keeps the prose after it. The token value is a credential-named pair in prose: it runs to its true
        # end, the end of the text or the space before the next pair ("https:").
        for first, clean, suffixes in (
                ("https://example.invalid/?token=synthetic", "https://example.invalid/?token=<redacted>",
                 ((" trailing prose", ""), (" then https://example.invalid/?model=keep#anchor", " https://example.invalid/?model=keep#anchor"))),
                ("https://example.invalid/?audio=AQIDBA==", "https://example.invalid/?audio=<omitted: 8 base64 chars>",
                 ((" trailing prose", " trailing prose"),
                  (" then https://example.invalid/?model=keep#anchor", " then https://example.invalid/?model=keep#anchor")))):
            for suffix, kept in suffixes:
                with self.subTest(first=first, suffix=suffix):
                    self.assertEqual(clean + kept, self.reference.scrub(first + suffix))
                    self.assertEqual(clean + kept, self.reference.scrub(first + suffix, "exception.message"))

    def test_exact_url_classification_masks_only_data_query_payloads(self):
        for value in ("https://example.invalid/?token=synthetic trailing prose",
                      "https://example.invalid/?audio=AQIDBA== trailing prose",
                      "https://example.invalid/#fragment then https://example.invalid/?token=synthetic",
                      'https://example.invalid/?blob=data:,literal"payload&model=keep trailing prose',
                      'https://example.invalid/?blob=data:,literal"payload#anchor trailing prose'):
            with self.subTest(value=value):
                self.assertFalse(self.reference.is_url_value(value))
        for value in ('https://example.invalid/?blob=data:,literal" payload&model=keep#anchor',
                      '/path?blob=data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg">payload</svg>',
                      '?blob=data:,literal\\payload\nmore payload&model=keep',
                      "https://example.invalid/?audio=<omitted: 8 base64 chars>&token=<redacted>"):
            with self.subTest(value=value):
                self.assertTrue(self.reference.is_url_value(value))

    def test_unframed_url_scanning_retains_surrounding_prose(self):
        value = 'https://example.invalid/?blob=data:audio/basic,A"prose after'
        expected = 'https://example.invalid/?blob=<omitted: data URI>"prose after'
        self.assertEqual(expected, self.reference.scrub(value, framed=False))
        self.assertEqual("see " + expected, self.reference.scrub("see " + value))
        self.assertEqual("https://example.invalid/?blob=<omitted: data URI>", self.reference.scrub(value))

    def test_query_data_uri_scanning_has_boundaries_and_is_nonrecursive(self):
        from urllib.parse import quote
        for value in ("metadata:source,year", "candidate_data:source,year", "data:audio/wav;base64",
                      quote("data:audio/wav;base64,AQIDBA==", safe="")):
            url = "https://example.invalid/?blob=" + quote(value, safe="") + "&model=keep"
            with self.subTest(value=value):
                self.assertEqual(url, self.reference.scrub(url))
        value = "before data:audio/wav;base64,AQIDBA== after"
        url = "https://example.invalid/?blob=" + quote(value, safe="") + "&model=keep"
        expected = "https://example.invalid/?blob=before <omitted: 8 base64 chars> after&model=keep"
        self.assertEqual(expected, self.reference.scrub(url))

    def test_fragment_and_second_question_pairs_are_replaced(self):
        # A credential-named pair after '#' or after a second '?' is replaced at every site, whole URL value or prose; it is not
        # part of the query, so request observation does not harvest it.
        for url in ("#fragment?token=ordinary", "https://example.invalid/#fragment?token=ordinary",
                    "https://example.invalid/?model=keep#fragment?token=ordinary",
                    "https://example.invalid/?model=why?token=ordinary"):
            expected = url.replace("?token=ordinary", "?token=<redacted>")
            self.assertEqual(expected, self.reference.scrub(url))
            self.assertEqual("see " + expected, self.reference.scrub("see " + url + " next", framed=False))
            self.assertEqual((), self.reference.harvest({"url": url}))

    def test_data_uri_scheme_boundary(self):
        for value in ("metadata:source,year", "candidate_data:source,year", "x-data:source,year", "other:data:source,year"):
            self.assertEqual(value, self.reference.scrub(value))
        self.assertEqual("image=<omitted: 4 base64 chars>", self.reference.scrub("image=data:image/png;base64,QQ=="))

    def test_catalog_magic_fits_bounded_prefix(self):
        for rule in self.catalog["media"]["magic"]:
            for test in rule["tests"]:
                self.assertLessEqual(test["offset"] + len(bytes.fromhex(test["hex"])), self.catalog["media"]["base64_prefix_decoded_bytes"])
                if "mask" in test:
                    self.assertEqual(len(test["hex"]), len(test["mask"]))

    def test_magic_decoder_never_decodes_more_than_prefix_budget(self):
        import base64
        encoded = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"a" * 4096).decode("ascii")
        decode = base64.b64decode
        sizes = []

        def observed(*args, **kwargs):
            output = decode(*args, **kwargs)
            sizes.append(len(output))
            return output

        with patch.object(generator.base64, "b64decode", side_effect=observed):
            self.assertEqual("png", self.reference.magic(encoded))
        self.assertTrue(sizes)
        self.assertLessEqual(max(sizes), self.catalog["media"]["base64_prefix_decoded_bytes"])

    def test_reference_node_and_text_budgets(self):
        with self.assertRaisesRegex(ValueError, "structure budget"):
            self.reference.scrub([0] * (self.catalog["limits"]["max_nodes"] + 1))
        with self.assertRaisesRegex(ValueError, "text budget"):
            self.reference.scrub("x" * (self.catalog["limits"]["max_text_chars"] + 1))

    def test_reference_depth_budget(self):
        # Nesting deeper than max_depth is a name path of more than max_depth components: the whole value is the placeholder.
        value = "hello"
        for _ in range(self.catalog["limits"]["max_depth"] + 2):
            value = [value]
        self.assertEqual("<redacted>", self.reference.scrub(value))
        # Nesting under empty names adds no path component, and the structure budget still applies.
        value = "hello"
        for _ in range(self.catalog["limits"]["max_depth"] + 2):
            value = {"": value}
        with self.assertRaisesRegex(ValueError, "structure budget"):
            self.reference.scrub(value)

    def test_data_uri_whole_values_omit_quoted_literal_payloads(self):
        from urllib.parse import quote
        samples = ("data:audio/basic,A'SYNTHETIC_MEDIA_20261030",
                   'data:audio/basic,A"SYNTHETIC_MEDIA_20261030',
                   "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg'>literal</svg>",
                   'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg">literal</svg>',
                   "data:,literal payload\nwith&parameters#fragment")
        for value in samples:
            with self.subTest(value=value):
                expected = "<omitted: data URI>"
                for path in ("", "blob", "input.value", "parts.0.blob", "input.audio.0"):
                    self.assertEqual(expected, self.reference.scrub(value, path))
                self.assertEqual({"blob": [expected]}, self.reference.scrub({"blob": [value]}))
                url = "https://example.invalid/?blob=" + quote(value, safe="") + "&model=keep#anchor"
                self.assertEqual("https://example.invalid/?blob=" + expected + "&model=keep#anchor", self.reference.scrub(url))
                self.assertEqual(expected, self.reference.scrub(expected))

    def test_data_uri_exact_values_precede_context_and_url_scanning(self):
        self.assertEqual("<omitted: 4 base64 chars>", self.reference.scrub("data:audio/basic;base64,QQ==", "audio.data"))
        self.assertEqual("<omitted: data URI>", self.reference.scrub("data:,https://example.invalid/?model='payload'"))
        for value in ("data:audio/basic,A'payload", "data:audio/basic;base64,QQ== after"):
            self.assertEqual("<omitted: data URI>", self.reference.scrub(value))
            self.assertEqual("<omitted: data URI>", self.reference.data_text(value, framed=True))
        self.assertEqual("data:audio/basic", self.reference.scrub("data:audio/basic"))

    def test_only_free_text_uses_delimiter_limited_data_uri_scanning(self):
        for value, expected in (("data:audio/basic,literal after", "<omitted: data URI> after"),
                                ("data:audio/basic;base64,QQ== after", "<omitted: 4 base64 chars> after")):
            self.assertEqual(expected, self.reference.scrub(value, framed=False))
            self.assertEqual(expected, self.reference.text(value, ()))
            self.assertEqual("<omitted: data URI>", self.reference.scrub(value))
        self.assertEqual("before <omitted: 4 base64 chars> after",
                         self.reference.scrub("before data:audio/basic;base64,QQ== after"))

    def test_query_media_siblings_match_native_and_flattened_objects(self):
        from urllib.parse import quote, urlencode
        for hint_name, hint_value, path in (("mime_type", "audio/pcm", "content"),
                                            ("content-type", "image/png", "source.data"),
                                            ("parts.0.mime_type", "audio/pcm", "parts.0.content.chunk"),
                                            ("parts[0].mime_type[0]", "audio/pcm", "parts[0].content.chunk"),
                                            ("type", "input_audio", "data"),
                                            ("parts.0.type", "output_audio", "parts.0.source.b64_json")):
            for pairs in ([(hint_name, hint_value), (path, "AQIDBA==")],
                          [(path, "AQIDBA=="), (hint_name, hint_value)]):
                with self.subTest(pairs=pairs):
                    expected_object = dict(pairs)
                    expected_object[path] = "<omitted: 8 base64 chars>"
                    self.assertEqual(expected_object, self.reference.scrub(dict(pairs)))
                    raw_query = urlencode(pairs, quote_via=quote)
                    expected_query = raw_query.replace("AQIDBA%3D%3D", "<omitted: 8 base64 chars>")
                    url = "https://example.invalid/?" + raw_query + "&&model=keep#anchor"
                    expected = "https://example.invalid/?" + expected_query + "&&model=keep#anchor"
                    self.assertEqual(expected, self.reference.scrub(url))
                    self.assertEqual({"url": expected}, self.reference.scrub({"url": url}))
                    self.assertEqual("see " + expected + " next", self.reference.scrub("see " + url + " next"))
                    self.assertEqual(expected, self.reference.scrub(expected))
        native = {"parts": [{"mime_type": "audio/pcm", "content": {"chunk": "AQIDBA=="}}]}
        self.assertEqual({"parts": [{"mime_type": "audio/pcm", "content": {"chunk": "<omitted: 8 base64 chars>"}}]},
                         self.reference.scrub(native))

    def test_query_duplicate_media_hints_combine_without_reordering(self):
        for hints in ("mime_type=audio%2Fpcm&mime_type=text%2Fplain", "mime_type=text%2Fplain&mime_type=audio%2Fpcm"):
            url = "https://example.invalid/?content=AQIDBA%3D%3D&" + hints + "&content=Qg%3D%3D&empty=&flag#anchor"
            expected = "https://example.invalid/?content=<omitted: 8 base64 chars>&" + hints + "&content=<omitted: 4 base64 chars>&empty=&flag#anchor"
            self.assertEqual(expected, self.reference.scrub(url))
            self.assertEqual(expected, self.reference.scrub(expected))

    def test_query_media_isolates_indices_metadata_and_decoding_depth(self):
        url = ("https://example.invalid/?parts.0.mime_type=audio%2Fpcm&parts.0.content.chunk=AQIDBA%3D%3D"
               "&parts.1.content.chunk=AQIDBA%3D%3D&parts.0.transcript.content=words"
               "&blob=data%253Aaudio%252Fpcm%252CAQIDBA&voice=alloy#anchor")
        expected = url.replace("parts.0.content.chunk=AQIDBA%3D%3D", "parts.0.content.chunk=<omitted: 8 base64 chars>")
        self.assertEqual(expected, self.reference.scrub(url))
        negative = "https://example.invalid/?mime_type=text%2Fplain&content=AQIDBA%3D%3D&type=text"
        self.assertEqual(negative, self.reference.scrub(negative))

    def test_token_details_objects_counters_and_harvesting_are_not_credentials(self):
        from urllib.parse import urlencode
        counters = {"cache_creation": 2, "cache_read": 3, "audio_tokens": 5, "reasoning_tokens": 7}
        for prefix in ("input", "prompt", "completion", "output"):
            for suffix in ("token_details", "tokens_details"):
                name = prefix + "_" + suffix
                with self.subTest(name=name):
                    for path in (name, "usage." + name, "gen_ai.usage." + name + "[0]", "audio." + name):
                        self.assertFalse(self.reference.credential_name(path))
                        self.assertEqual(counters, self.reference.scrub(counters, path))
                        for counter, number in counters.items():
                            self.assertFalse(self.reference.credential_name(path + "." + counter))
                            self.assertEqual(number, self.reference.scrub(number, path + "." + counter))
                    value = {name: counters, "audio": {name: counters}}
                    self.assertEqual(value, self.reference.scrub(value))
                    # Long strings make accidental registry harvesting observable.
                    self.assertEqual((), self.reference.harvest({"headers": {name: "123456789"},
                                                               "json": {name: {"cache_read": "123456789"}},
                                                               "url": "https://example.invalid/?" + urlencode({name + ".cache_read": "123456789"})}))
                    self.assertTrue(self.reference.credential_name("secret." + name))
                    self.assertTrue(self.reference.credential_name(name + ".api_key"))
                    self.assertTrue(self.reference.credential_name("client_secret_" + name))

    def test_audio_objects_only_capture_explicit_payloads_not_parameters(self):
        parameters = {"voice": "alloy", "format": "wav", "max_tokens": 5, "sample_rate": 16000, "speed": 1.25,
                      "id": "audio_20261030", "expires_at": 1793383200, "input_tokens": 11,
                      "usage": {"output_tokens": 7}, "custom_parameter": {"ordinary": "keep"}}
        for audio_field in self.catalog["media"]["audio"]["fields"]:
            with self.subTest(field=audio_field):
                value = {audio_field: dict(parameters, data="AQIDBA==")}
                expected = {audio_field: dict(parameters, data="<omitted: 8 base64 chars>")}
                self.assertEqual(expected, self.reference.scrub(value))
                self.assertEqual({audio_field: [parameters]}, self.reference.scrub({audio_field: [parameters]}))
                for name, item in parameters.items():
                    for path in (audio_field + "." + name, "input." + audio_field + "[0]." + name):
                        self.assertEqual(item, self.reference.scrub(item, path))
        self.assertEqual(parameters, self.reference.scrub(parameters, siblings={"type": "input_audio"}))

    def test_audio_payloads_and_indexed_samples_still_omit_any_size(self):
        for field in self.catalog["media"]["audio"]["payload_fields"]:
            for path in ("audio." + field, "input.audio[0]." + field + ".chunk[0]"):
                with self.subTest(path=path):
                    self.assertEqual("<omitted: 4 base64 chars>", self.reference.scrub("QQ==", path))
                    self.assertEqual("<omitted: media>", self.reference.scrub("raw audio bytes", path))
                    self.assertEqual("<omitted: media>", self.reference.scrub([0, 1, 2], path))
                    self.assertEqual("<omitted: media>", self.reference.scrub(7, path))
        for path in ("audio", "input.audio.0", "input.audio[0][1]", "input.audio.samples.0"):
            self.assertEqual("<omitted: media>", self.reference.scrub(7, path))
        self.assertEqual({"audio": {"samples": "<omitted: media>", "voice": "alloy"}},
                         self.reference.scrub({"audio": {"samples": [0, 1, 2], "voice": "alloy"}}))

    def test_audio_metadata_does_not_disable_independent_privacy_rules(self):
        secret = "SYNTHETIC_PRIVATE_20261030"
        value = {"audio": {"voice": "data:audio/basic,A'payload", "api_key": secret,
                           "custom_parameter": "echo " + secret, "format": "iVBORw0KGgo="}}
        expected = {"audio": {"voice": "<omitted: data URI>", "api_key": "<redacted>",
                              "custom_parameter": "echo <redacted>", "format": "<omitted: 12 base64 chars>"}}
        self.assertEqual(expected, self.reference.scrub(value, secrets=[secret]))
        self.assertEqual((secret,), self.reference.harvest({"json": value}))

    def test_generated_bytes_are_current(self):
        expected = (json.dumps({"version": 1, "catalog_version": self.catalog["version"],
                                "cases": generator.golden_cases(self.catalog)}, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        self.assertEqual(expected, (SPEC / "privacy-fixtures.json").read_bytes())


class PairRuleTests(unittest.TestCase):
    """The credential pair rule and the fragment-pair catalog rule, against literal data committed under notes/.

    notes/pair-rule-java-overrides-1122.json holds the texts the Java agent's own code produced at d1b3f3a1 for the 139 cases it
    overrides; notes/pair-rule-cases-1122.json holds the 42 whole-URL fragment targets and the new cases, each with its expected text.
    """
    NOTES = SPEC / "notes"
    # Cases where the JSON quoted-name ruling (c) decides the text, not the Java agent at d1b3f3a1. Each value is the expected text.
    RULING_C = {}

    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads((SPEC / "privacy.json").read_text())
        cls.reference = generator.Reference(cls.catalog)
        cls.rows = json.loads((SPEC / "privacy-fixtures.json").read_text())["cases"]
        cls.bundle = {case["name"]: case for case in cls.rows}
        cls.java = json.loads((cls.NOTES / "pair-rule-java-overrides-1122.json").read_text(encoding="utf-8"))
        cls.data = json.loads((cls.NOTES / "pair-rule-cases-1122.json").read_text(encoding="utf-8"))

    def scrub(self, case):
        inputs = case["input"]
        return self.reference.scrub(inputs["value"], inputs["path"], inputs.get("siblings"), inputs.get("known_secrets", ()),
                                    framed=inputs["site"] != "free_text_urls")

    def test_java_override_cases_are_the_shared_expectation(self):
        self.assertEqual(139, len(self.java))
        mismatched = []
        for name, text in self.java.items():
            expected = self.RULING_C.get(name, text)
            if self.bundle[name]["expected"] != expected or self.scrub(self.bundle[name]) != expected:
                mismatched.append(name)
        self.assertEqual([], mismatched)

    def test_whole_url_pairs_after_fragment_or_second_question_are_replaced(self):
        targets = self.data["fragment_targets"]
        self.assertEqual(42, len(targets))
        mismatched = [name for name, expected in targets.items()
                      if self.bundle[name]["expected"] != expected or self.scrub(self.bundle[name]) != expected]
        self.assertEqual([], mismatched)

    def test_new_pair_rule_cases_are_generated_with_their_literal_expectation(self):
        new = self.data["new_cases"]
        self.assertEqual(256, len(new))
        missing, mismatched = [], []
        for case in new:
            actual = self.bundle.get(case["name"])
            if actual is None or actual["input"] != {"site": case["site"], "path": case["path"], "value": case["value"]}:
                missing.append(case["name"])
            elif actual["expected"] != case["expected"]:
                mismatched.append(case["name"])
            framed = case["site"] != "free_text_urls"
            if self.reference.scrub(case["value"], case["path"], framed=framed) != case["expected"]:
                mismatched.append(case["name"] + " (reference)")
        self.assertEqual(([], []), (missing, mismatched))

    @staticmethod
    def digest(value):
        import hashlib
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def same(before, after):
        """JSON equality that keeps types apart (1, 1.0 and true are different values)."""
        return json.dumps(before, sort_keys=True) == json.dumps(after, sort_keys=True)

    @classmethod
    def more_redacted(cls, before, after):
        """Whether after is before with one or more non-empty runs replaced by the placeholder, nothing else changed: a placeholder
        that replaces nothing (a pure insertion) does not count."""
        if isinstance(before, dict) or isinstance(after, dict):
            return (isinstance(before, dict) and isinstance(after, dict) and list(before) == list(after)
                    and all(cls.same(before[key], after[key]) or cls.more_redacted(before[key], after[key]) for key in before)
                    and not cls.same(before, after))
        if isinstance(before, list) or isinstance(after, list):
            return (isinstance(before, list) and isinstance(after, list) and len(before) == len(after)
                    and all(cls.same(item, other) or cls.more_redacted(item, other) for item, other in zip(before, after))
                    and not cls.same(before, after))
        if not isinstance(before, str) or not isinstance(after, str) or before == after:
            return False
        # Glob match: the literal segments of after, in order, with a non-empty run of before between each two. Leftmost matching
        # is exact for a pattern whose only wildcard is "a run of one or more characters".
        segments = after.split("<redacted>")
        if len(segments) < 2 or not before.startswith(segments[0]) or not before.endswith(segments[-1]):
            return False
        position, last = len(segments[0]), len(before) - len(segments[-1])
        for segment in segments[1:-1]:
            found = before.find(segment, position + 1, last)
            if found < 0:
                return False
            position = found + len(segment)
        return position < last

    def test_bundle_diff_is_the_literal_changed_set(self):
        diff = json.loads((self.NOTES / "pair-rule-bundle-diff-1122.json").read_text(encoding="utf-8"))
        rows = [line.split(" ", 2) for line in (self.NOTES / "pair-rule-base-digests-1122.txt").read_text(encoding="utf-8").splitlines()
                if not line.startswith("#")]
        rows = [(inputs, expected, json.loads(name)) for inputs, expected, name in rows]
        base = {name: (inputs, expected) for inputs, expected, name in rows}
        # Phase 1b appends its cases after these 6494 (PairRule1bTests guards them and pins that no case here changes).
        self.assertEqual((6238, len(self.rows)), (len(base), len(self.bundle)), "rows, and no duplicate name")
        self.assertEqual((6238, 6494), (diff["base_case_count"], diff["case_count"]))
        names = list(self.bundle)
        self.assertEqual([name for _, _, name in rows], names[:6238], "base cases keep their order and none is removed")
        self.assertEqual(diff["added"], names[6238:6494])
        self.assertEqual(diff["added"], [case["name"] for case in self.data["new_cases"]])
        changed_inputs = [name for name, (inputs, _) in base.items()
                          if self.digest({"covers": self.bundle[name]["covers"], "input": self.bundle[name]["input"]}) != inputs]
        self.assertEqual([], changed_inputs)
        actual = sorted(name for name, (_, expected) in base.items() if self.digest(self.bundle[name]["expected"]) != expected)
        self.assertEqual(sorted(diff["changed"]), actual)
        self.assertEqual(sorted(set(self.java) | set(self.data["fragment_targets"])), actual)
        for name, change in diff["changed"].items():
            with self.subTest(name=name):
                self.assertEqual(base[name][1], self.digest(change["base_expected"]))
                self.assertEqual(change["expected"], self.bundle[name]["expected"])
                self.assertTrue(self.more_redacted(change["base_expected"], change["expected"]))

    def test_more_redacted_check(self):
        self.assertTrue(self.more_redacted("a=1&b=2 next", "a=<redacted>"))
        self.assertTrue(self.more_redacted("a=<redacted>&b", "a=<redacted>"))
        self.assertTrue(self.more_redacted({"u": "x=1 y"}, {"u": "x=<redacted> y"}))
        self.assertFalse(self.more_redacted("a=1", "a=1"))
        self.assertFalse(self.more_redacted("a=1 next", "a=<redacted> nxt"))
        self.assertFalse(self.more_redacted("a=1", "b=<redacted>"))
        self.assertFalse(self.more_redacted("ab", "a<redacted>b<redacted>c"))
        self.assertTrue(self.more_redacted({"password": "a", "model": "keep"}, {"password": "<redacted>", "model": "keep"}))
        self.assertFalse(self.more_redacted([1, "a"], [True, "<redacted>"]))
        self.assertFalse(self.more_redacted({"a": 1, "b": "x"}, {"a": 1.0, "b": "<redacted>"}))

    def test_pair_rule_reference_on_literal_text(self):
        redacted = "<redacted>"
        for text, expected in (
                ("see #a?token=abc,password=xyz", "see #a?token=" + redacted + ",password=" + redacted),
                ("password=" + redacted + ",TAILC more", "password=" + redacted),
                ("x https://example.invalid/?model=keep#token=TAILA next", "x https://example.invalid/?model=keep#token=" + redacted),
                ('"password": 12345 TAILL more', '"password": "' + redacted + '"'),
                ('"password": 12345, "model": "keep"', '"password": "' + redacted + '", "model": "keep"'),
                ("--password 'a b' rest", "--password " + redacted + " rest"),
                ("Authorization: Basic x y\nnext", "Authorization: " + redacted + "\nnext"),
                ('note "Cookie": sid=a; id=b\nnext', 'note "Cookie": "' + redacted + '"\nnext'),
                ('log "password": [1],TAIL word', 'log "password": "' + redacted + '"'),
                ('log "pin":{abc], more text', 'log "pin":"' + redacted + '"'),
                ('log "password": 12\f, x', 'log "password": "' + redacted + '"'),
                ('log "row\u2028tokens":"demo"', 'log "row\u2028tokens":"' + redacted + '"')):
            with self.subTest(text=text):
                self.assertEqual(expected, self.reference.scrub(text, framed=False))
        for url, expected in (("https://example.invalid/?model=keep#token=TAILA", "https://example.invalid/?model=keep#token=" + redacted),
                              ("https://example.invalid/?a=1?token=TAILB", "https://example.invalid/?a=1?token=" + redacted),
                              ("https://example.invalid/#section?sp=TAILM", "https://example.invalid/#section?sp=" + redacted),
                              ("https://example.invalid/?flag?sp=TAILN", "https://example.invalid/?flag?sp=" + redacted),
                              ("https://example.invalid/?a=keep?token?tag=TAILO", "https://example.invalid/?a=keep?token?tag=" + redacted),
                              ("https://example.invalid/?p=x?sig=TAILP%20data:,hi", "https://example.invalid/?p=x?sig=" + redacted),
                              ("https://example.invalid/?token=" + redacted + "&model=keep", "https://example.invalid/?token=" + redacted + "&model=keep")):
            with self.subTest(url=url):
                self.assertEqual(expected, self.reference.scrub(url))


class PairRule1bTests(unittest.TestCase):
    """Phase 1b: the shared vocabulary, URL path pairs, map keys, the name path budget, the green catalog check, the pair rule over
    whole URL values by position, the guard's non-empty runs, bounded names that fail closed, quoted names spelled &quot; or \\",
    object and array values under a quoted name in prose, and a single-pass second '?' read. Literal texts throughout;
    notes/pair-rule-1b-cases-1122.json holds every new case with its expected value."""
    NOTES = SPEC / "notes"
    R = "<redacted>"
    PAD = "x" * 130
    # The base cases (phase 1 head, 5efb9f4c) whose expectation phase 1b changes. None: every one keeps its expectation.
    CHANGED = ()

    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads((SPEC / "privacy.json").read_text())
        cls.reference = generator.Reference(cls.catalog)
        cls.rows = json.loads((SPEC / "privacy-fixtures.json").read_text())["cases"]
        cls.bundle = {case["name"]: case for case in cls.rows}
        cls.data = json.loads((cls.NOTES / "pair-rule-1b-cases-1122.json").read_text(encoding="utf-8"))

    def check(self, value, expected, framed=True, path=""):
        with self.subTest(value=value, framed=framed, path=path):
            self.assertEqual(expected, self.reference.scrub(value, path, framed=framed))

    def whole_url(self, url, expected, prose):
        """Ruling 6's sites: request_query, response_body {url} and attribute_keys http.url, then the text in prose."""
        self.check(url, expected)
        self.check({"url": url}, {"url": expected})
        self.check(url, expected, path="http.url")
        self.check("see " + url + " next", prose, framed=False)

    def bundled(self, prefix):
        names = [case["name"] for case in self.data["new_cases"] if case["name"].startswith(prefix)]
        self.assertTrue(names, prefix)
        for name in names:
            with self.subTest(case=name):
                self.assertIn(name, self.bundle)

    # 1. The shared vocabulary.
    def test_1_catalog_vocabulary_is_shared_and_versioned(self):
        self.assertEqual(4, self.catalog["version"])
        self.assertEqual(["pwd", "pass", "passphrase", "passwd", "passcode", "pin"], self.catalog["credentials"]["vocabulary"]["words"])

    def test_1_vocabulary_names_whole_or_segment(self):
        for name in ("pwd", "PASS", "passphrase", "passwd", "passcode", "pin", "db_pwd", "db.pass", "dbPass", "user-pin", "X-Pin",
                     "app[passcode]", "db pwd", "db/pwd", "x.y.passphrase"):
            with self.subTest(name=name):
                self.assertTrue(self.reference.credential_name(name))
                self.assertTrue(self.reference.query_name(name))
        for name in ("spinner", "pinned", "compass", "bypass", "passage", "pins", "dbpwdx", "opinion", "input"):
            with self.subTest(name=name):
                self.assertFalse(self.reference.credential_name(name))
        # As PrivacyCatalog.credentialName reads them: UTF-16 units (each half of a character outside the BMP is a delimiter), and
        # exclusions whose '.' matches no line terminator.
        self.assertTrue(self.reference.credential_name("\U00010400pin"))
        self.assertTrue(self.reference.credential_name("metric\rtokens"))

    def test_1_vocabulary_at_every_site(self):
        R = self.R
        self.check("https://example.invalid/?pwd=TAILV&model=keep", "https://example.invalid/?pwd=" + R + "&model=keep")
        self.check("https://example.invalid/?model=keep#dbPass=TAILV&view=keep", "https://example.invalid/?model=keep#dbPass=" + R + "&view=keep")
        self.check("https://example.invalid/v1/user-pin=TAILV/next", "https://example.invalid/v1/user-pin=" + R + "/next")
        self.check({"nested": [{"passcode": "TAILV", "model": "keep"}]}, {"nested": [{"passcode": R, "model": "keep"}]})
        self.check({"note passphrase=TAILV": "keep", "model": "keep"}, {"note passphrase=" + R: R, "model": "keep"})
        self.check("TAILV", R, path="db.passwd")
        self.check("TAILV", R, path="metadata.pin.0")
        self.check("log dbPwd=TAILV then more", "log dbPwd=" + R, framed=False)
        self.bundled("pair-rule-1b-vocabulary-")

    # 2. URL path pairs.
    def test_2_url_path_pairs(self):
        R = self.R
        self.whole_url("https://example.invalid/password=TAILP", "https://example.invalid/password=" + R,
                       "see https://example.invalid/password=" + R)
        self.whole_url("https://example.invalid/a/password=TAILP/b", "https://example.invalid/a/password=" + R + "/b",
                       "see https://example.invalid/a/password=" + R)
        self.whole_url("https://example.invalid/a/password=TAILP?model=keep#frag", "https://example.invalid/a/password=" + R + "?model=keep#frag",
                       "see https://example.invalid/a/password=" + R)
        # Phase 1c ruling (a): the pair rule reads %3D as a separator, so in prose the value runs to its true end.
        self.whole_url("https://example.invalid/a/password%3DTAILP/b", "https://example.invalid/a/password%3D" + R + "/b",
                       "see https://example.invalid/a/password%3D" + R)
        self.whole_url("/a/password=TAILP/b?model=keep", "/a/password=" + R + "/b?model=keep", "see /a/password=" + R)
        self.whole_url("https://example.invalid/a/model=keep/b", "https://example.invalid/a/model=keep/b",
                       "see https://example.invalid/a/model=keep/b next")
        self.bundled("pair-rule-1b-path-")

    # 3. Map keys.
    def test_3_map_keys(self):
        R = self.R
        self.check({"note pwd=TAILK": "keep", "model": "keep"}, {"note pwd=" + R: R, "model": "keep"})
        self.check({"id AKIAEXAMPLETAILR0000": "a", "id AKIAEXAMPLETAILS0000": "b", "model": "keep"}, {"id " + R: R, "model": "keep"})
        self.check({"model": "keep", "pwd=TAILM": "a", "pwd=TAILN": "b"}, {"model": "keep", "pwd=" + R: R})
        self.check({"AKIAEXAMPLETAILQ0000": "keep", "model": "keep"}, {R: "keep", "model": "keep"})
        self.check({"nested": [{"id AKIAEXAMPLETAILR0000": "a", "id AKIAEXAMPLETAILS0000": "b"}]}, {"nested": [{"id " + R: R}]})
        # A key is read as a whole string value is: a whole data URI is one replacement.
        self.check({"data:text/plain,hello world": "a", "data:text/plain,goodbye": "b"}, {"<omitted: data URI>": R})
        self.bundled("pair-rule-1b-map-key-")

    # 4. The name path budget.
    def test_4_name_path_budget(self):
        depth = self.catalog["limits"]["max_depth"]
        self.assertEqual(32, depth)
        at, over = "0." * (depth - 1) + "field", "0." * depth + "field"
        self.check("log " + at + "=keep then more", "log " + at + "=keep then more", framed=False)
        self.check("log " + over + "=keep then more", self.R, framed=False)
        self.check({at: "keep", "model": "keep"}, {at: "keep", "model": "keep"})
        self.check({over: "keep", "model": "keep"}, self.R)
        self.check("keep", "keep", path=at)
        self.check("keep", self.R, path=over)
        self.check("keep", self.R, path="a[0]" * 17)
        nested = "keep"
        for _ in range(depth + 1):
            nested = [nested]
        self.check(nested, self.R)
        self.assertEqual(self.R, self.reference.scrub("keep", "model", {"0." * depth + "type": "text"}))
        # A pair name read through its window: the window alone holds more than max_depth components.
        self.check("log " + "0." * 70 + "field=keep then more", self.R, framed=False)
        self.bundled("pair-rule-1b-depth-")

    # 5. The catalog check.
    def test_5_generator_writes_the_committed_catalog_bytes(self):
        self.assertEqual((SPEC / "privacy.json").read_bytes(), generator.catalog_bytes(generator.privacy_catalog()))

    # 6. The pair rule over a whole URL value, skipping only the catalog's spans by position.
    def test_6_whole_url_pairs_outside_catalog_spans(self):
        R = self.R
        self.whole_url("https://example.invalid/?model=keep,password=TAILA", "https://example.invalid/?model=keep,password=" + R,
                       "see https://example.invalid/?model=keep,password=" + R)
        self.whole_url("https://example.invalid/#foo=bar,password=TAILB", "https://example.invalid/#foo=bar,password=" + R,
                       "see https://example.invalid/#foo=bar,password=" + R)
        self.whole_url("https://example.invalid/#/cb;access_token=TAILC", "https://example.invalid/#/cb;access_token=" + R,
                       "see https://example.invalid/#/cb;access_token=" + R)
        self.whole_url("https://example.invalid/?pwd=TAILD", "https://example.invalid/?pwd=" + R, "see https://example.invalid/?pwd=" + R)
        self.bundled("pair-rule-1b-whole-url-")

    def test_6_skip_is_by_position_not_by_placeholder_text(self):
        R = self.R
        # The catalog's own span: the query rule replaced token's value, so the pair rule leaves it and reads on after it.
        self.check("https://example.invalid/?token=TAILE&model=keep", "https://example.invalid/?token=" + R + "&model=keep")
        # Placeholder text the catalog did not write is no skip: the pair rule runs the value to its true end.
        self.check("https://example.invalid/?q=password=" + R + ",TAILZ", "https://example.invalid/?q=password=" + R)
        self.check("https://example.invalid/?note=password:" + R + "%20TAILZ&model=keep", "https://example.invalid/?note=password:" + R)
        spans = []
        text = generator.Rewrite(self.reference)
        text.keep("a=")
        text.put(R)
        text.keep("&b")
        self.assertEqual(("a=" + R + "&b", [(2, 12)]), text.result())
        self.assertEqual("password=" + R + "&model=keep", self.reference.credential_pairs("password=" + R + "&model=keep", [(9, 19)]))
        self.assertEqual("password=" + R, self.reference.credential_pairs("password=" + R + "&model=keep", spans))
        # A query value the catalog rewrote for a data URI: only the data URI is a span, not the text around it.
        self.assertEqual(("/?model=note <omitted: data URI>", [(13, 32)]), self.reference.framed_url("/?model=note%20data:text/plain,hello"))
        self.check("https://example.invalid/?model=note%20password:TAILX%20data:text/plain,hello&view=keep",
                   "https://example.invalid/?model=note password:" + R)
        # User information that runs on to a later '@' than the catalog's is replaced whole.
        self.check("https://user:TAILU@TAILV@example.invalid/path", "https://" + R + "@example.invalid/path")
        # A placeholder never cuts a data URI short: path, fragment and query read data URIs in the original text.
        self.check("https://example.invalid/data:,TAILD/password=TAILP/TAILE",
                   "https://example.invalid/<omitted: data URI>" + R + "<omitted: data URI>")
        self.check("https://example.invalid/#data:,TAILD&pwd=TAILP&TAILE", "https://example.invalid/#<omitted: data URI>" + R + "<omitted: data URI>")
        self.check("https://example.invalid/?note=data:,TAILD?pwd=TAILP", "https://example.invalid/?note=<omitted: data URI>")

    # 7. The guard rejects a pure insertion.
    def test_7_more_redacted_needs_a_non_empty_run_per_placeholder(self):
        more = PairRuleTests.more_redacted
        self.assertFalse(more("password=TAILA", "password=TAILA" + self.R))
        self.assertFalse(more("password=TAILA next", "password=" + self.R + "TAILA next"))
        self.assertFalse(more("a=1 b=2", "a=" + self.R + "1 b=2"))
        self.assertTrue(more("password=TAILA next", "password=" + self.R + " next"))
        self.assertTrue(more("a=" + self.R + " b=2", "a=" + self.R + " b=" + self.R))

    # 8. No length trick keeps a credential.
    def test_8_bounded_names_fail_closed(self):
        R, pad = self.R, self.PAD
        self.whole_url("https://example.invalid/?a=1?" + pad + "token=TAILE", "https://example.invalid/?a=1?" + pad + "token=" + R,
                       "see https://example.invalid/?a=1?" + pad + "token=" + R)
        self.whole_url("https://example.invalid/?a=1?token" + pad + "=TAILE&model=keep",
                       "https://example.invalid/?a=1?token" + pad + "=" + R + "&model=keep", "see https://example.invalid/?a=1?token" + pad + "=" + R)
        self.whole_url("https://example.invalid/?" + pad + "token=TAILE&model=keep", "https://example.invalid/?" + pad + "token=" + R + "&model=keep",
                       "see https://example.invalid/?" + pad + "token=" + R)
        self.whole_url("https://example.invalid/#a=1?" + pad + "token=TAILE", "https://example.invalid/#a=1?" + pad + "token=" + R,
                       "see https://example.invalid/#a=1?" + pad + "token=" + R)
        self.whole_url("https://example.invalid/#a?token" + pad + "=TAILE&view=keep", "https://example.invalid/#a?token" + pad + "=" + R + "&view=keep",
                       "see https://example.invalid/#a?token" + pad + "=" + R)
        self.check("log " + pad + "token=TAILE then more", "log " + pad + "token=" + R, framed=False)
        self.check("log token" + pad + "=TAILE then more", "log token" + pad + "=" + R, framed=False)
        self.check('log "' + pad + 'token": "TAILE" then more', 'log "' + pad + 'token": "' + R + '" then more', framed=False)
        self.check("log --token" + pad + " TAILE then more", "log --token" + pad + " " + R + " then more", framed=False)
        self.bundled("pair-rule-1b-long-name-")

    # 9. Quoted names spelled &quot; or \".
    def test_9_entity_and_escaped_quoted_names(self):
        R = self.R
        self.check("log &quot;password&quot;: &quot;TAILF&quot; then more", "log &quot;password&quot;: &quot;" + R + "&quot; then more", framed=False)
        self.check('log \\"password\\": \\"TAILG\\" then more', 'log \\"password\\": \\"' + R + '\\" then more', framed=False)
        self.check({"message": 'log \\"password\\": \\"TAILG\\" then more'}, {"message": 'log \\"password\\": \\"' + R + '\\" then more'})
        self.check('{"message": "log \\"password\\": \\"TAILG\\" then more"}', '{"message":"log \\"password\\": \\"' + R + '\\" then more"}',
                   path="input.value")
        for prefix in ("pair-rule-1b-quoted-entity", "pair-rule-1b-quoted-escaped", "pair-rule-1b-quoted-json-string"):
            self.bundled(prefix)

    # 10. Object and array values under a quoted credential name in prose.
    def test_10_object_and_array_values_run_to_the_true_end(self):
        R = self.R
        for framed in (True, False):
            self.check('"password": {"a": "TAILH"} and more', '"password": "' + R + '"', framed=framed)
            self.check('"password": ["TAILI"] and more', '"password": "' + R + '"', framed=framed)
        self.bundled("pair-rule-1b-quoted-object-value-")
        self.bundled("pair-rule-1b-quoted-array-value-")

    # 11. The second '?' read is one pass.
    def test_11_second_question_reads_each_character_a_bounded_number_of_times(self):
        read = [0]
        original = generator.Reference.query_name

        def counting(reference, name):
            read[0] += len(name)
            return original(reference, name)
        value = "1" + ("?" * generator.QUESTION_CAP + "x" * 120 + "=") * 40
        with patch.object(generator.Reference, "query_name", counting):
            self.assertEqual(value, self.reference.second_question(value))
        self.assertLessEqual(read[0], (generator.QUESTION_CAP + 1) * len(value))
        # More '?' marks in one name than the cap: not decided, so replaced (fail closed).
        self.assertEqual("1" + "?" * (generator.QUESTION_CAP + 1) + "x=" + self.R,
                         self.reference.second_question("1" + "?" * (generator.QUESTION_CAP + 1) + "x=TAILQ"))

    def test_11_second_question_matches_every_suffix_reading(self):
        import random
        rng = random.Random(1122)
        pieces = ["?", "=", "x", "a", ".", "_", "-", "%3F", "token", "key", "se", "sp", "sig", "pin", "pwd", "max_tokens", "keyid", "y" * 60]

        def brute(value):
            last_equals = -1
            for i, c in enumerate(value):
                if c != "=":
                    continue
                marks = [q for q in range(last_equals + 1, i) if value[q] == "?"]
                if len(marks) > generator.QUESTION_CAP or any(
                        i - q - 1 > generator.NAME_CAP or self.reference.query_name(generator.unquote_plus(value[q + 1:i])) for q in marks):
                    return value[:i + 1] + self.R
                last_equals = i
            return value
        self.assertEqual("?keyid.?to.ken?x=" + self.R, self.reference.second_question("?keyid.?to.ken?x=TAILK"))
        for _ in range(3000):
            value = "".join(rng.choice(pieces) for _ in range(rng.randrange(1, 14)))
            with self.subTest(value=value):
                self.assertEqual(brute(value), self.reference.second_question(value))

    def test_bundle_diff_is_the_literal_changed_set(self):
        """The guard: the base cases (phase 1 head) keep their order, inputs and covers; the changed set is the literal CHANGED list,
        equal to the actual diff, and every change is the base expectation with non-empty runs replaced by the placeholder."""
        diff = json.loads((self.NOTES / "pair-rule-1b-bundle-diff-1122.json").read_text(encoding="utf-8"))
        rows = [line.split(" ", 2) for line in (self.NOTES / "pair-rule-1b-base-digests-1122.txt").read_text(encoding="utf-8").splitlines()
                if not line.startswith("#")]
        rows = [(inputs, expected, json.loads(name)) for inputs, expected, name in rows]
        base = {name: (inputs, expected) for inputs, expected, name in rows}
        # Phase 1c appends its cases after these 6937 (PairRule1cTests guards them, and the two 1b cases its ruling (a) changes).
        self.assertEqual((6494, len(self.rows)), (len(base), len(self.bundle)), "rows, and no duplicate name")
        self.assertEqual(("5efb9f4ccf24a31211b1e093369de4daa2dd2d81", 6494, 6937),
                         (diff["base"], diff["base_case_count"], diff["case_count"]))
        names = list(self.bundle)
        self.assertEqual([name for _, _, name in rows], names[:6494], "base cases keep their order and none is removed")
        self.assertEqual(diff["added"], names[6494:6937])
        self.assertEqual(diff["added"], [case["name"] for case in self.data["new_cases"]])
        changed_inputs = [name for name, (inputs, _) in base.items()
                          if PairRuleTests.digest({"covers": self.bundle[name]["covers"], "input": self.bundle[name]["input"]}) != inputs]
        self.assertEqual([], changed_inputs)
        actual = sorted(name for name, (_, expected) in base.items() if PairRuleTests.digest(self.bundle[name]["expected"]) != expected)
        self.assertEqual(sorted(self.CHANGED), actual)
        self.assertEqual(sorted(diff["changed"]), actual)
        for name, change in diff["changed"].items():
            with self.subTest(name=name):
                self.assertEqual(base[name][1], PairRuleTests.digest(change["base_expected"]))
                self.assertEqual(change["expected"], self.bundle[name]["expected"])
                self.assertTrue(PairRuleTests.more_redacted(change["base_expected"], change["expected"]))

    def test_new_cases_are_generated_with_their_literal_expectation(self):
        new = self.data["new_cases"]
        self.assertEqual(443, len(new))
        # Phase 1c ruling (a) changes exactly two of these cases (notes/pair-rule-1c-cases-1122.json, "changed").
        later = json.loads((self.NOTES / "pair-rule-1c-cases-1122.json").read_text(encoding="utf-8"))["changed"]
        self.assertEqual(["pair-rule-1b-path-encoded-lower-text", "pair-rule-1b-path-encoded-text"], sorted(later))
        missing, mismatched, overridden = [], [], []
        for case in new:
            expected = case["expected"]
            if case["name"] in later:
                self.assertEqual(expected, later[case["name"]]["base_expected"])
                expected = later[case["name"]]["expected"]
                overridden.append(case["name"])
            actual = self.bundle.get(case["name"])
            if actual is None or actual["input"] != {"site": case["site"], "path": case["path"], "value": case["value"]}:
                missing.append(case["name"])
            elif actual["expected"] != expected:
                mismatched.append(case["name"])
            if self.reference.scrub(case["value"], case["path"], framed=case["site"] != "free_text_urls") != expected:
                mismatched.append(case["name"] + " (reference)")
        self.assertEqual(([], [], sorted(later)), (missing, mismatched, sorted(overridden)))


class PairRule1cTests(unittest.TestCase):
    """Phase 1c: a gap between a name and its separator that is not readable leaves the name undecided (fail closed); an omission
    span never shields a credential pair's value that starts in it; URL path segments read every '=' and %3D, and the pair rule reads
    %3D as a separator. Literal texts throughout; notes/pair-rule-1c-cases-1122.json holds every new case with its expected value
    and the two base cases ruling (a) changes."""
    NOTES = SPEC / "notes"
    R = "<redacted>"
    # The base cases (phase 1b head, a21a68ef) whose expectation phase 1c changes: exactly the two of ruling (a).
    CHANGED = ("pair-rule-1b-path-encoded-text", "pair-rule-1b-path-encoded-lower-text")

    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads((SPEC / "privacy.json").read_text())
        cls.reference = generator.Reference(cls.catalog)
        cls.rows = json.loads((SPEC / "privacy-fixtures.json").read_text())["cases"]
        cls.bundle = {case["name"]: case for case in cls.rows}
        cls.data = json.loads((cls.NOTES / "pair-rule-1c-cases-1122.json").read_text(encoding="utf-8"))

    def check(self, value, expected, framed=True, path=""):
        with self.subTest(value=value, framed=framed, path=path):
            self.assertEqual(expected, self.reference.scrub(value, path, framed=framed))

    def whole_url(self, url, expected, prose):
        """The whole URL sites: request_query, response_body {url} and attribute_keys http.url, then the text in prose."""
        self.check(url, expected)
        self.check({"url": url}, {"url": expected})
        self.check(url, expected, path="http.url")
        self.check("see " + url + " next", prose, framed=False)

    def prose(self, text, expected):
        """Prose (free_text_urls) and a JSON site (response_body {"message": text})."""
        self.check(text, expected, framed=False)
        self.check({"message": text}, {"message": expected})

    def bundled(self, prefix):
        names = [case["name"] for case in self.data["new_cases"] if case["name"].startswith(prefix)]
        self.assertTrue(names, prefix)
        for name in names:
            with self.subTest(case=name):
                self.assertIn(name, self.bundle)

    # 1. The gap between a name and its separator.
    def test_1_gap_that_is_not_readable_leaves_the_name_undecided(self):
        R = self.R
        for gap in ("\t", " " * 9, "  \t  ", "\u2003", "\u00a0", "\n", "\x0b", " " * 40):
            with self.subTest(gap=gap):
                self.prose("log password" + gap + "=TAILV then more", "log password" + gap + "=" + R)
                self.prose('log "password"' + gap + ': "TAILV" then more', 'log "password"' + gap + ': "' + R + '" then more')
                self.prose("log model" + gap + "=keep then more", "log model" + gap + "=" + R)
                self.prose('log "model"' + gap + ': "keep" then more', 'log "model"' + gap + ': "' + R + '" then more')
                self.prose("log model" + gap + "%3Dkeep then more", "log model" + gap + "%3D" + R)
                self.prose("log &quot;model&quot;" + gap + ": &quot;keep&quot; then more",
                           "log &quot;model&quot;" + gap + ": &quot;" + R + "&quot; then more")
        # A --name, a gap, then a separator: the pair is read at the separator, as for a bare name.
        for gap in ("\t", " " * 9, "  \t  ", "\u2003"):
            with self.subTest(gap=gap):
                self.prose("log --password" + gap + "=TAILV then more", "log --password" + gap + "=" + R)
                self.prose("log --password" + gap + "= TAILV then more", "log --password" + gap + "= " + R)
                self.prose("log --password" + gap + ": TAILV then more", "log --password" + gap + ": " + R)
                self.prose("log --password" + gap + "%3D TAILV then more", "log --password" + gap + "%3D " + R)
                self.prose("log --model" + gap + "=keep then more", "log --model" + gap + "=" + R)
                # A list header keeps its extent (the rest of the line) after any gap.
                self.prose("log cookie" + gap + ": a=1; b=TAILV, c=TAILW\nthen more", "log cookie" + gap + ": " + R + "\nthen more")
                self.prose('log "cookie"' + gap + ": a=1; b=TAILV, c=TAILW\nthen more", 'log "cookie"' + gap + ': "' + R + '"\nthen more')
                self.prose("log Set-Cookie" + gap + ": sid=TAILV; Path=/; HttpOnly\nthen more",
                           "log Set-Cookie" + gap + ": " + R + "\nthen more")
        # The --name rule itself is unchanged: the next word, or a quoted word to its closing quote.
        self.prose("log --password TAILV then more", "log --password " + R + " then more")
        self.prose('log --password "TAIL V" then more', "log --password " + R + " then more")

    def test_1_readable_gap_is_up_to_eight_spaces(self):
        R = self.R
        for gap in ("", " ", " " * 8):
            with self.subTest(gap=gap):
                self.prose("log model" + gap + "=keep then more", "log model" + gap + "=keep then more")
                self.prose('log "model"' + gap + ': "keep" then more', 'log "model"' + gap + ': "keep" then more')
                self.prose("log password" + gap + "=TAILV then more", "log password" + gap + "=" + R)
                self.prose('log "password"' + gap + ': "TAILV" then more', 'log "password"' + gap + ': "' + R + '" then more')
                self.prose("log --password" + gap + "= TAILV then more", "log --password" + gap + "= " + R)
                self.prose("log cookie" + gap + ": a=1; b=TAILV\nthen more", "log cookie" + gap + ": " + R + "\nthen more")
        # A name of one character is not a pair name, whatever the gap.
        self.prose("log x\t=keep then more", "log x\t=keep then more")
        # json_name: (opening, quote, close, undecided).
        self.assertEqual((0, '"', 9, False), generator.json_name('"password"' + " " * 8 + ': "v"', 18))
        self.assertEqual((0, '"', 9, True), generator.json_name('"password"' + " " * 9 + ': "v"', 19))
        self.assertEqual((0, '"', 9, True), generator.json_name('"password"\t: "v"', 11))
        self.bundled("pair-rule-1c-gap-")

    def test_1_gap_reads_are_linear(self):
        """Each whitespace run is read back by the one separator after it: the gap reads add up to at most twice the text (json_name
        and the pair rule each read the gap before a ':')."""
        read = [0]
        original = generator.gap_start

        def counting(text, end, floor):
            start = original(text, end, floor)
            read[0] += end - start
            return start
        text = ("a" + "\t " * 40 + ":") * 2000 + ("b" + " " * 300 + "=") * 500 + "\t" * 20000 + "=" + " " * 20000 + ":"
        with patch.object(generator, "gap_start", counting):
            self.assertEqual(text, self.reference.credential_pairs(text))
        self.assertLessEqual(read[0], 2 * len(text))

    # 2. Omission spans in a credential value.
    def test_2_omission_span_never_shields_a_credential_value(self):
        R = self.R
        for cut in ("%20", "%22", "%27", "%3C", "%3E", "%5C"):
            self.whole_url("https://example.invalid/?q=password=data:,x" + cut + "TAILX", "https://example.invalid/?q=password=" + R,
                           "see https://example.invalid/?q=password=" + R)
        self.whole_url("https://example.invalid/?q=password=data:text/plain;base64,QUJD%20TAILX", "https://example.invalid/?q=password=" + R,
                       "see https://example.invalid/?q=password=" + R)
        self.whole_url("https://example.invalid/?q=password:%20data:,x%20TAILX", "https://example.invalid/?q=password: " + R,
                       "see https://example.invalid/?q=password: " + R)
        self.whole_url("https://example.invalid/?q=pwd=data:,x%20TAILX", "https://example.invalid/?q=pwd=" + R,
                       "see https://example.invalid/?q=pwd=" + R)
        # A data URI that is not in a credential value keeps its omission.
        self.whole_url("https://example.invalid/?q=model=data:,x%20keep", "https://example.invalid/?q=model=<omitted: data URI> keep",
                       "see https://example.invalid/?q=model=<omitted: data URI> keep next")
        self.bundled("pair-rule-1c-data-uri-")

    def test_2_span_kind_is_recorded_by_position_never_read_from_text(self):
        R, Span = self.R, generator.Span
        out = generator.Rewrite(self.reference)
        out.keep("password=")
        out.put("<omitted: data URI>", omission=True)
        out.keep(" TAILX")
        text, spans = out.result()
        self.assertEqual(("password=<omitted: data URI> TAILX", [(9, 28)]), (text, spans))
        self.assertTrue(spans[0].omission)
        self.assertEqual("password=" + R, self.reference.credential_pairs(text, spans))
        # The same text under a placeholder span is shielded: the kind decides, not the text.
        self.assertEqual(text, self.reference.credential_pairs(text, [(9, 28)]))
        self.assertEqual(text, self.reference.credential_pairs(text, [Span(9, 28)]))
        # Placeholder text under an omission span shields nothing.
        self.assertEqual("password=" + R, self.reference.credential_pairs("password=" + R + " TAILX", [Span(9, 19, True)]))
        self.assertEqual("password=" + R + " TAILX", self.reference.credential_pairs("password=" + R + " TAILX", [Span(9, 19)]))
        # The kind moves with its span under known secrets and value shapes.
        moved = generator.replace_runs("TAILSECRET <omitted: data URI>", [(0, 10)], [Span(11, 30, True)])[1]
        self.assertEqual([(11, 30)], moved)
        self.assertTrue(moved[0].omission)
        # The catalog pass writes each data URI omission and each whole media value as an omission, each placeholder as one.
        self.assertEqual([True], [span.omission for span in self.reference.framed_url("/?model=note%20data:text/plain,hello")[1]])
        self.assertEqual([False], [span.omission for span in self.reference.framed_url("/?token=TAILE&model=keep")[1]])
        self.assertEqual([True], [span.omission for span in self.reference.framed_url("/?note=data:,TAILD?pwd=TAILP")[1]])

    # 3. Every '=' and %3D in a path segment.
    def test_3_path_segments_read_every_equals(self):
        R = self.R
        for sep in (";", ",", "&"):
            for equals in ("=", "%3D", "%3d"):
                with self.subTest(sep=sep, equals=equals):
                    self.whole_url("https://example.invalid/a=1" + sep + "password" + equals + "TAILX/next",
                                   "https://example.invalid/a=1" + sep + "password" + equals + R + "/next",
                                   "see https://example.invalid/a=1" + sep + "password" + equals + R)
        self.whole_url("https://example.invalid/a=1;password=TAILX;model=keep,token%3DTAILY&view=keep/next",
                       "https://example.invalid/a=1;password=" + R + ";model=keep,token%3D" + R + "&view=keep/next",
                       "see https://example.invalid/a=1;password=" + R + ";model=keep,token%3D" + R)
        self.whole_url("https://example.invalid/a=1;model%3Dkeep,view=keep/next", "https://example.invalid/a=1;model%3Dkeep,view=keep/next",
                       "see https://example.invalid/a=1;model%3Dkeep,view=keep/next next")
        # A value holds '=' and a separator with no pair after it; it ends at the next pair.
        self.whole_url("https://example.invalid/password=a=b;c;d=keep/next", "https://example.invalid/password=" + R + ";d=keep/next",
                       "see https://example.invalid/password=" + R + ";d=keep/next next")
        self.assertEqual(3, generator.Reference.path_value_end("a;b;c=d", 0, 7))
        self.assertEqual(7, generator.Reference.path_value_end("a;b;=c;", 0, 7))
        # The pair rule reads %3D and %3d as a separator in prose too.
        self.check("log password%3DTAILX then more", "log password%3D" + R, framed=False)
        self.check("log api_key%3dTAILX then more", "log api_key%3d" + R, framed=False)
        self.check("log model%3Dkeep then more", "log model%3Dkeep then more", framed=False)
        self.check("https://example.invalid/?q=password%3DTAILX", "https://example.invalid/?q=password%3D" + R)
        self.bundled("pair-rule-1c-path-every-")

    def test_3_later_pair_after_a_skipped_path_span(self):
        """Ruling (b): the path pair's value ends at the segment end, and a later credential pair in the same URL is still replaced."""
        R = self.R
        self.whole_url("https://example.invalid/a=1;password=TAILX/next/secret:TAILY",
                       "https://example.invalid/a=1;password=" + R + "/next/secret:" + R, "see https://example.invalid/a=1;password=" + R)
        self.whole_url("https://example.invalid/a=1;password=TAILX/next?model=keep,secret=TAILY",
                       "https://example.invalid/a=1;password=" + R + "/next?model=keep,secret=" + R,
                       "see https://example.invalid/a=1;password=" + R + ",secret=" + R)
        self.whole_url("https://example.invalid/a=1;password=TAILX/next#view=keep,secret=TAILY",
                       "https://example.invalid/a=1;password=" + R + "/next#view=keep,secret=" + R,
                       "see https://example.invalid/a=1;password=" + R + ",secret=" + R)
        text = "a=1;password=" + R + "/next?model=keep,secret=TAILY"
        self.assertEqual("a=1;password=" + R + "/next?model=keep,secret=" + R, self.reference.credential_pairs(text, [(13, 23)]))
        self.bundled("pair-rule-1c-path-later-")

    # 4. A credential-like host, pinned as it is.
    def test_4_credential_like_host_pinned(self):
        R = self.R
        self.whole_url("http://keycloak.example.invalid:8080/path?x=1", "http://keycloak.example.invalid:" + R,
                       "see http://keycloak.example.invalid:" + R)
        self.whole_url("http://user:TAILU@keycloak.example.invalid:8080/path?x=1", "http://" + R + "@keycloak.example.invalid:" + R,
                       "see http://" + R + "@keycloak.example.invalid:" + R)
        self.whole_url("http://service.example.invalid:8080/path?x=1", "http://service.example.invalid:8080/path?x=1",
                       "see http://service.example.invalid:8080/path?x=1 next")
        self.bundled("pair-rule-1c-host-")

    # 5. URL path shapes: whole URL and prose outputs, pinned.
    def test_5_path_shapes_whole_url_and_prose(self):
        R = self.R
        for shape, url, whole, prose in (
                ("single", "https://example.invalid/password=TAILP", "https://example.invalid/password=" + R,
                 "see https://example.invalid/password=" + R),
                ("mid", "https://example.invalid/a/password=TAILP/b", "https://example.invalid/a/password=" + R + "/b",
                 "see https://example.invalid/a/password=" + R),
                ("query-fragment", "https://example.invalid/a/password=TAILP?model=keep#frag",
                 "https://example.invalid/a/password=" + R + "?model=keep#frag", "see https://example.invalid/a/password=" + R),
                ("encoded", "https://example.invalid/a/password%3DTAILP/b", "https://example.invalid/a/password%3D" + R + "/b",
                 "see https://example.invalid/a/password%3D" + R),
                ("encoded-lower", "https://example.invalid/a/api_key%3dTAILP?model=keep",
                 "https://example.invalid/a/api_key%3d" + R + "?model=keep", "see https://example.invalid/a/api_key%3d" + R),
                ("matrix", "https://example.invalid/a;access_token=TAILP/b", "https://example.invalid/a;access_token=" + R + "/b",
                 "see https://example.invalid/a;access_token=" + R),
                ("userinfo", "https://user:TAILQ@example.invalid/a/token=TAILP/b?model=keep",
                 "https://" + R + "@example.invalid/a/token=" + R + "/b?model=keep", "see https://" + R + "@example.invalid/a/token=" + R),
                ("relative", "/a/password=TAILP/b?model=keep", "/a/password=" + R + "/b?model=keep", "see /a/password=" + R),
                ("not-credential", "https://example.invalid/a/model=keep/b", "https://example.invalid/a/model=keep/b",
                 "see https://example.invalid/a/model=keep/b next")):
            with self.subTest(shape=shape):
                self.whole_url(url, whole, prose)
                self.assertEqual((url, whole), (self.bundle["pair-rule-1b-path-" + shape + "-query"]["input"]["value"],
                                                self.bundle["pair-rule-1b-path-" + shape + "-query"]["expected"]))
                self.assertEqual(prose, self.bundle["pair-rule-1b-path-" + shape + "-text"]["expected"])

    def test_bundle_diff_is_the_literal_changed_set(self):
        """The guard: the base cases (phase 1b head) keep their order, inputs and covers; the changed set is the literal CHANGED list,
        equal to the actual diff, and every change is the base expectation with non-empty runs replaced by the placeholder; the added
        cases are the literal list of notes/pair-rule-1c-cases-1122.json, in order, after the base cases."""
        diff = json.loads((self.NOTES / "pair-rule-1c-bundle-diff-1122.json").read_text(encoding="utf-8"))
        rows = [line.split(" ", 2) for line in (self.NOTES / "pair-rule-1c-base-digests-1122.txt").read_text(encoding="utf-8").splitlines()
                if not line.startswith("#")]
        rows = [(inputs, expected, json.loads(name)) for inputs, expected, name in rows]
        base = {name: (inputs, expected) for inputs, expected, name in rows}
        # Phase 1d appends its cases after these 7113 (PairRule1dTests guards them and pins that no case here changes).
        self.assertEqual((6937, len(self.rows)), (len(base), len(self.bundle)), "rows, and no duplicate name")
        self.assertEqual(("a21a68efb01ea2f8fe918436f905efc82f2f792f", 6937, 7113),
                         (diff["base"], diff["base_case_count"], diff["case_count"]))
        names = list(self.bundle)
        self.assertEqual([name for _, _, name in rows], names[:6937], "base cases keep their order and none is removed")
        self.assertEqual(diff["added"], names[6937:7113])
        self.assertEqual(diff["added"], [case["name"] for case in self.data["new_cases"]])
        changed_inputs = [name for name, (inputs, _) in base.items()
                          if PairRuleTests.digest({"covers": self.bundle[name]["covers"], "input": self.bundle[name]["input"]}) != inputs]
        self.assertEqual([], changed_inputs)
        actual = sorted(name for name, (_, expected) in base.items() if PairRuleTests.digest(self.bundle[name]["expected"]) != expected)
        self.assertEqual(sorted(self.CHANGED), actual)
        self.assertEqual(sorted(diff["changed"]), actual)
        self.assertEqual(sorted(self.data["changed"]), actual)
        for name, change in diff["changed"].items():
            with self.subTest(name=name):
                self.assertEqual(base[name][1], PairRuleTests.digest(change["base_expected"]))
                self.assertEqual(change["base_expected"], self.data["changed"][name]["base_expected"])
                self.assertEqual(change["expected"], self.bundle[name]["expected"])
                self.assertEqual(change["expected"], self.data["changed"][name]["expected"])
                self.assertTrue(PairRuleTests.more_redacted(change["base_expected"], change["expected"]))

    def test_new_cases_are_generated_with_their_literal_expectation(self):
        new = self.data["new_cases"]
        self.assertEqual(176, len(new))
        missing, mismatched = [], []
        for case in new:
            actual = self.bundle.get(case["name"])
            if actual is None or actual["input"] != {"site": case["site"], "path": case["path"], "value": case["value"]}:
                missing.append(case["name"])
            elif actual["expected"] != case["expected"]:
                mismatched.append(case["name"])
            if self.reference.scrub(case["value"], case["path"], framed=case["site"] != "free_text_urls") != case["expected"]:
                mismatched.append(case["name"] + " (reference)")
        self.assertEqual(([], []), (missing, mismatched))


class PairRule1dTests(unittest.TestCase):
    """Phase 1d: the name read-back stops at any %XX escape; every --name gap is read with str.isspace; a --name value that starts
    with ':' or '://' is replaced; encoded and escaped whitespace between a name and its separator is an unreadable gap (fail
    closed). Literal texts throughout; notes/pair-rule-1d-cases-1122.json holds every new case with its expected value."""
    NOTES = SPEC / "notes"
    R = "<redacted>"

    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads((SPEC / "privacy.json").read_text())
        cls.reference = generator.Reference(cls.catalog)
        cls.rows = json.loads((SPEC / "privacy-fixtures.json").read_text())["cases"]
        cls.bundle = {case["name"]: case for case in cls.rows}
        cls.data = json.loads((cls.NOTES / "pair-rule-1d-cases-1122.json").read_text(encoding="utf-8"))

    def check(self, value, expected, framed=True, path=""):
        with self.subTest(value=value, framed=framed, path=path):
            self.assertEqual(expected, self.reference.scrub(value, path, framed=framed))

    def prose(self, text, expected):
        """Prose (free_text_urls) and a JSON site (response_body {"message": text})."""
        self.check(text, expected, framed=False)
        self.check({"message": text}, {"message": expected})

    def whole_url(self, url, expected, prose):
        self.check(url, expected)
        self.check({"url": url}, {"url": expected})
        self.check(url, expected, path="http.url")
        self.check("see " + url + " next", prose, framed=False)

    def bundled(self, prefix):
        names = [case["name"] for case in self.data["new_cases"] if case["name"].startswith(prefix)]
        self.assertTrue(names, prefix)
        for name in names:
            with self.subTest(case=name):
                self.assertIn(name, self.bundle)

    # 1. The name read-back stops at any %XX escape.
    def test_1_read_back_stops_at_any_escape(self):
        R = self.R
        self.prose("log a%3Dpwd%3DTAILX then more", "log a%3Dpwd%3D" + R)
        self.prose("log a%3dpwd%3dTAILX then more", "log a%3dpwd%3d" + R)
        self.prose("log a%3Dpwd=TAILX then more", "log a%3Dpwd=" + R)
        self.prose("log a%3Dpwd:TAILX then more", "log a%3Dpwd:" + R)
        self.prose("log a%41pwd%3DTAILX then more", "log a%41pwd%3D" + R)
        self.prose("log a%20pwd=TAILX then more", "log a%20pwd=" + R)
        self.prose("log a%3Dmodel%3Dkeep then more", "log a%3Dmodel%3Dkeep then more")
        self.whole_url("https://example.invalid/?q=a%3Dpass%3DTAILX", "https://example.invalid/?q=a%3Dpass%3D" + R,
                       "see https://example.invalid/?q=a%3Dpass%3D" + R)
        self.whole_url("https://example.invalid/#a%3Dpass%3DTAILX", "https://example.invalid/#a%3Dpass%3D" + R,
                       "see https://example.invalid/#a%3Dpass%3D" + R)
        for word in self.catalog["credentials"]["vocabulary"]["words"] + ["password"]:
            with self.subTest(word=word):
                self.prose("log a%3D" + word + "%3DTAILX then more", "log a%3D" + word + "%3D" + R)
                self.whole_url("https://example.invalid/?q=a%3D" + word + "%3DTAILX", "https://example.invalid/?q=a%3D" + word + "%3D" + R,
                               "see https://example.invalid/?q=a%3D" + word + "%3D" + R)
                self.whole_url("https://example.invalid/#a%3D" + word + "%3DTAILX", "https://example.invalid/#a%3D" + word + "%3D" + R,
                               "see https://example.invalid/#a%3D" + word + "%3D" + R)
        self.bundled("pair-rule-1d-readback-")

    # 2. A --name gap is read with str.isspace.
    def test_2_dashes_gap_is_str_isspace(self):
        R = self.R
        for code in ("\u00a0", "\u2007", "\u202f", "\u0085"):
            for lead in ("", " ", "\t"):
                gap = lead + code
                with self.subTest(gap=gap):
                    self.prose("log --password" + gap + "=TAILV then more", "log --password" + gap + "=" + R)
                    self.prose("log --password" + gap + ":TAILV then more", "log --password" + gap + ":" + R)
                    self.prose("log --password" + gap + "TAILV then more", "log --password" + gap + R + " then more")
        self.prose("log --password\u00a0TAILV", "log --password\u00a0" + R)
        self.bundled("pair-rule-1d-dashes-gap-")

    # 3. A --name value that starts with ':' or '://' is the --name's value.
    def test_3_dashes_value_that_starts_with_a_colon(self):
        R = self.R
        self.prose("log --password ://TAILV then more", "log --password :" + R)
        self.prose("log --password :TAILV then more", "log --password :" + R)
        self.prose("log --password\t://TAILV then more", "log --password\t:" + R)
        self.prose("log --token=://TAILV then more", "log --token=" + R)
        self.check("log --password ://TAILV", "log --password :" + R, framed=False)
        self.assertEqual("--password :" + R, self.reference.credential_pairs("--password ://TAILV"))
        self.bundled("pair-rule-1d-dashes-colon-")

    # 4. Encoded and escaped whitespace between a name and its separator is an unreadable gap.
    GAPS = ("%20", "%09", "%0A", "%0B", "%0C", "%0D", "%C2%A0", "%0a", "%0b", "%0c", "%0d", "%c2%a0", "%c2%A0", "%C2%a0",
            "\\t", "\\n", "\\r", "\\f", "\\b", "\\u0020", "\\u0009", "\\u00a0", "\\u00A0", "\\u2003", "\\u2028", "\\u0085",
            " %20", "%20 ")

    def test_4_encoded_gap_fails_closed(self):
        R = self.R
        for gap in self.GAPS:
            for separator in ("=", "%3D", ":"):
                with self.subTest(gap=gap, separator=separator):
                    self.prose("log password" + gap + separator + "TAILX then more", "log password" + gap + separator + R)
            with self.subTest(gap=gap, form="quoted"):
                self.prose('log "password"' + gap + ': "TAILX" then more', 'log "password"' + gap + ': "' + R + '" then more')
                self.prose('log \\"password\\"' + gap + ': \\"TAILX\\" then more', 'log \\"password\\"' + gap + ': \\"' + R + '\\" then more')
        for gap in ("%20", "\\t"):
            with self.subTest(gap=gap, form="non-credential and list header"):
                self.prose("log model" + gap + "=keep then more", "log model" + gap + "=" + R)
                self.prose("log cookie" + gap + ": a=1; b=TAILX, c=TAILY\nthen more", "log cookie" + gap + ": " + R + "\nthen more")
        self.prose('log {\\"password\\"\\t: \\"TAILX\\", \\"model\\": \\"keep\\"}',
                   'log {\\"password\\"\\t: \\"' + R + '\\", \\"model\\": \\"keep\\"}')
        # The brief's examples.
        self.prose("password%20%3DTAILX", "password%20%3D" + R)
        self.prose("password%09=TAILX", "password%09=" + R)
        self.bundled("pair-rule-1d-encoded-gap-")

    def test_4_dashes_gap_that_ends_in_an_encoded_gap_fails_closed(self):
        """Review finding: after a --name, plain whitespace and then encoded whitespace and a separator is one unreadable gap; the
        value after the separator is replaced (the --name rule used to take the word "%20=" as the value and keep the rest)."""
        R = self.R
        for gap in ("%20", "%09", "%C2%A0", "\\t", "\\u00a0"):
            for lead in (" ", "\t", "\u00a0"):
                for separator in ("=", ":", "%3D"):
                    with self.subTest(gap=gap, lead=lead, separator=separator):
                        self.prose("log --password" + lead + gap + separator + " TAILX", "log --password" + lead + gap + separator + " " + R)
                        self.prose("log --password" + lead + gap + separator + "TAILX then more", "log --password" + lead + gap + separator + R)

    def test_4_a_backslash_that_is_itself_escaped_starts_no_escape(self):
        """In "password\\\\t=TAILX" the t follows an escaped backslash: it is a name character, not the \\t escape, and the name
        there is one character, so no pair is read (as for any one-character name). "password\\t=TAILX" is the escape."""
        self.assertEqual("log password\\\\t=TAILX", self.reference.credential_pairs("log password\\\\t=TAILX"))
        self.assertEqual("log password\\t=" + self.R, self.reference.credential_pairs("log password\\t=TAILX"))
        self.assertEqual(0, generator.encoded_space_width("a\\\\t", 4, 0))
        self.assertEqual(2, generator.encoded_space_width("a\\\\\\t", 5, 0))
        self.assertEqual(2, generator.encoded_space_width("a\\t", 3, 0))

    def test_4_gap_reads_are_linear(self):
        """Each gap, escapes included, is read back once by the one separator after it (json_name and the pair rule each read the gap
        before a ':', so at most twice)."""
        read = [0]
        original = generator.gap_start

        def counting(text, end, floor):
            start = original(text, end, floor)
            read[0] += end - start
            return start
        text = (("a" + "%20" * 40 + ":") * 1000 + ("b" + "\\t " * 100 + "=") * 500 + "\\" * 20000 + "=" + "%0A" * 20000 + ":"
                + "\\u0020" * 5000 + "%C2%A0" * 5000 + "=")
        with patch.object(generator, "gap_start", counting):
            self.reference.credential_pairs(text)
        self.assertLessEqual(read[0], 2 * len(text))

    # 5. Ruling (b) of phase 1c, asserted directly on credential_pairs.
    def test_5_a_later_pair_after_a_skipped_span_is_replaced(self):
        """The pair rule reads on after a span it skipped: a later credential pair is replaced, whatever separates it from the span. A
        reader that stopped after a skipped span would fail it."""
        R = self.R
        span = generator.Span(13, 23)
        head = "a=1;password=" + R
        for tail, expected in (
                ("/next/secret:TAILY", "/next/secret:" + R), ("/next?model=keep,secret=TAILY", "/next?model=keep,secret=" + R),
                ("/next#view=keep,secret=TAILY", "/next#view=keep,secret=" + R), (";secret=TAILY", ";secret=" + R),
                ("/next?x=1&token%3DTAILY&z=2", "/next?x=1&token%3D" + R), ('/x "api_key": "TAILY"', '/x "api_key": "' + R + '"'),
                ("/x --password TAILY", "/x --password " + R)):
            with self.subTest(tail=tail):
                self.assertEqual(head + expected, self.reference.credential_pairs(head + tail, [span]))
                self.assertEqual(head + expected, self.reference.credential_pairs(head + tail, [tuple(span)]))

    def test_bundle_diff_is_the_literal_changed_set(self):
        """The guard: the base cases (phase 1c head, 442071ae) keep their order, inputs, covers and expectations (the changed list is
        empty: no existing case may change); the added cases are the literal list of notes/pair-rule-1d-cases-1122.json, in order,
        after the base cases."""
        diff = json.loads((self.NOTES / "pair-rule-1d-bundle-diff-1122.json").read_text(encoding="utf-8"))
        rows = [line.split(" ", 2) for line in (self.NOTES / "pair-rule-1d-base-digests-1122.txt").read_text(encoding="utf-8").splitlines()
                if not line.startswith("#")]
        rows = [(inputs, expected, json.loads(name)) for inputs, expected, name in rows]
        base = {name: (inputs, expected) for inputs, expected, name in rows}
        # Phase 1e r2 appends its cases after these 7569 (its own guard pins them).
        self.assertEqual((7113, len(self.rows)), (len(base), len(self.bundle)), "rows, and no duplicate name")
        self.assertEqual(("442071ae28850dd4bb205b44fb0fb550cf25f154", 7113, 7569),
                         (diff["base"], diff["base_case_count"], diff["case_count"]))
        names = list(self.bundle)
        self.assertEqual([name for _, _, name in rows], names[:7113], "base cases keep their order and none is removed")
        self.assertEqual(diff["added"], names[7113:7569])
        self.assertEqual(diff["added"], [case["name"] for case in self.data["new_cases"]])
        changed_inputs = [name for name, (inputs, _) in base.items()
                          if PairRuleTests.digest({"covers": self.bundle[name]["covers"], "input": self.bundle[name]["input"]}) != inputs]
        self.assertEqual([], changed_inputs)
        changed = sorted(name for name, (_, expected) in base.items() if PairRuleTests.digest(self.bundle[name]["expected"]) != expected)
        self.assertEqual([], changed, "the changed list is empty")
        self.assertEqual(({}, {}), (diff["changed"], self.data["changed"]))

    def test_new_cases_are_generated_with_their_literal_expectation(self):
        new = self.data["new_cases"]
        self.assertEqual(456, len(new))
        missing, mismatched = [], []
        for case in new:
            actual = self.bundle.get(case["name"])
            if actual is None or actual["input"] != {"site": case["site"], "path": case["path"], "value": case["value"]}:
                missing.append(case["name"])
            elif actual["expected"] != case["expected"]:
                mismatched.append(case["name"])
            if self.reference.scrub(case["value"], case["path"], framed=case["site"] != "free_text_urls") != case["expected"]:
                mismatched.append(case["name"] + " (reference)")
        self.assertEqual(([], []), (missing, mismatched))


class PairRule1eR2Tests(unittest.TestCase):
    """Phase 1e r2: I1-I5 of the draft under the rulings of security record 527cc071. STOP 1: after a credential --name and ONE line
    break the next line is read fresh (a pair that starts it is read by its own branch, else only its first word is the value; a blank
    line ends the --name). New pair shapes: single-dash flags with a full credential name, and quoted names with '=' or %3D. Literal
    texts throughout; notes/pair-rule-1e-r2-cases-1122.json holds every added case with its expected value."""
    NOTES = SPEC / "notes"
    R = "<redacted>"
    LINE_BREAKS = (("nl", "\n"), ("cr", "\r"), ("nel", "\u0085"), ("u2028", "\u2028"))

    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads((SPEC / "privacy.json").read_text())
        cls.reference = generator.Reference(cls.catalog)
        cls.rows = json.loads((SPEC / "privacy-fixtures.json").read_text())["cases"]
        cls.bundle = {case["name"]: case for case in cls.rows}
        cls.data = json.loads((cls.NOTES / "pair-rule-1e-r2-cases-1122.json").read_text(encoding="utf-8"))

    def check(self, value, expected, framed=True, path=""):
        with self.subTest(value=value, framed=framed, path=path):
            self.assertEqual(expected, self.reference.scrub(value, path, framed=framed))

    def prose(self, text, expected):
        """Prose (free_text_urls) and a JSON site (response_body {"message": text})."""
        self.check(text, expected, framed=False)
        self.check({"message": text}, {"message": expected})

    def bundled(self, prefix):
        names = [case["name"] for case in self.data["new_cases"] if case["name"].startswith(prefix)]
        self.assertTrue(names, prefix)
        for name in names:
            with self.subTest(case=name):
                self.assertTrue(name in self.bundle, name + " is not in the bundle")

    # STOP 1: the 16 composition cases. Every value is redacted and no marker is left.
    def test_stop1_composition(self):
        R = self.R
        for label, lb in self.LINE_BREAKS:
            with self.subTest(line_break=label):
                self.prose("log --password" + lb + "TAILV token: TAILX", "log --password" + lb + R + " token: " + R)
                self.prose("log --password" + lb + "token=TAILX", "log --password" + lb + "token=" + R)
                self.prose("log --password" + lb + "TAILV" + lb + "token: TAILX", "log --password" + lb + R + lb + "token: " + R)
                self.prose("log --password" + lb + "TAILV, secret=TAILY", "log --password" + lb + R + " secret=" + R)
        self.bundled("pair-rule-1e-stop1-nl-word-")

    def test_stop1_first_word_of_the_next_line(self):
        R = self.R
        for lb in ("\n", "\r", "\r\n", "\u0085", "\u2028", "\u2029", "\x0c", "\x0b"):
            with self.subTest(line_break=lb):
                self.prose("log --password" + lb + "TAILV then more", "log --password" + lb + R + " then more")
                self.prose("log --password " + lb + "  TAILV then more", "log --password " + lb + "  " + R + " then more")
                self.prose("log -password" + lb + "TAILV then more", "log -password" + lb + R + " then more")
                self.prose("log --password" + lb + '"token": "TAILX" then more', "log --password" + lb + '"token": "' + R + '" then more')
                self.prose("log --password" + lb + "--token TAILX then more", "log --password" + lb + "--token " + R + " then more")
                self.prose("log --model" + lb + "keep then more", "log --model" + lb + "keep then more")
        self.assertEqual("--password\n" + R + " x", self.reference.credential_pairs("--password\nTAILV x"))
        self.bundled("pair-rule-1e-stop1-crlf-")

    def test_stop1_blank_line_ends_the_name(self):
        R = self.R
        for gap in ("\n\n", "\n \n", "\r\n\r\n", "\u0085\u0085"):
            with self.subTest(gap=gap):
                self.prose("log --password" + gap + "word then more", "log --password" + gap + "word then more")
        self.prose("log --password\n\ntoken=TAILX", "log --password\n\ntoken=" + R)
        self.prose("log --password\n=TAILV then more", "log --password\n=" + R)
        self.bundled("pair-rule-1e-stop1-blank-")

    def test_stop1_first_attempt_cases_keep_their_base_expectation(self):
        R = self.R
        for gap in ("\u0085", " \u0085", "\t\u0085"):
            with self.subTest(gap=gap):
                self.prose("log --password" + gap + "TAILV then more", "log --password" + gap + R + " then more")
        for name in ("pair-rule-1d-dashes-gap-" + lead + "-nel-bare-" + site for lead in ("none", "space", "tab") for site in ("prose", "json")):
            with self.subTest(case=name):
                case = self.bundle[name]
                text = case["input"]["value"] if case["input"]["site"] == "free_text_urls" else case["input"]["value"]["message"]
                clean = case["expected"] if case["input"]["site"] == "free_text_urls" else case["expected"]["message"]
                self.assertTrue(text.endswith("TAILV then more"))
                self.assertEqual(text.replace("TAILV", R), clean)

    # Single-dash flags with a full credential name.
    def test_single_dash_flags(self):
        R = self.R
        for text, clean in (
                ("log -password TAILV then more", "log -password " + R + " then more"),
                ("log -password\tTAILV then more", "log -password\t" + R + " then more"),
                ("log -password TAILV then more", "log -password " + R + " then more"),
                ("log -password=TAILV then more", "log -password=" + R),
                ("log -password%20TAILV then more", "log -password%20" + R + " then more"),
                ("log -password 'TAILV TAILW' then more", "log -password " + R + " then more"),
                ("log -pin TAILV then more", "log -pin " + R + " then more"),
                ("log -db_password TAILV then more", "log -db_password " + R + " then more"),
                ("log -password token: TAILX then more", "log -password " + R),
                ("log -password TAILV token: TAILX", "log -password " + R + " token: " + R),
                ("log -token TAILV -password TAILX then more", "log -token " + R + " -password " + R + " then more"),
                ('log -password TAILV "token"=TAILX', "log -password " + R + ' "token"=' + R),
                ("log --password -token TAILX then more", "log --password -token " + R + " then more"),
                ("log -passwordTAILV then more", "log -passwordTAILV " + R + " more")):
            self.prose(text, clean)
        for text in ("log -p TAILV then more", "log -abc TAILV then more", "log -model TAILV then more"):
            self.prose(text, text)
        self.bundled("pair-rule-1e-single-dash-")

    # Quoted names with '=' or %3D: the value runs to its true end.
    def test_quoted_name_with_equals(self):
        R = self.R
        for quote in ('"', "'", '\\"', "&quot;"):
            for separator in ("=", "%3D", "%3d"):
                name = quote + "password" + quote
                with self.subTest(quote=quote, separator=separator):
                    self.prose("log " + name + separator + "TAILV then more", "log " + name + separator + R)
                    self.prose("log " + name + separator + "TAILV token=TAILX", "log " + name + separator + R + " token=" + R)
                    control = "log " + quote + "model" + quote + separator + "keep then more"
                    self.prose(control, control)
        self.prose('log "password" =TAILV then more', 'log "password" =' + R)
        self.prose('log "password"\t=TAILV then more', 'log "password"\t=' + R)
        self.prose('log "model"\t=keep then more', 'log "model"\t=' + R)
        self.prose('log "password"="TAILV" then more', 'log "password"=' + R)
        self.prose('log "cookie"=a=1; b=TAILX\nthen more', 'log "cookie"=' + R + "\nthen more")
        self.prose('log "user%5Bpassword%5D"=TAILV then more', 'log "user%5Bpassword%5D"=' + R)
        self.assertEqual('"password"=' + R, self.reference.credential_pairs('"password"=TAILV then more'))
        self.bundled("pair-rule-1e-quoted-eq-")

    # I1-I5 of the draft.
    def test_i1_separator_in_a_name_word(self):
        R = self.R
        self.prose("log --password token: TAILX", "log --password " + R)
        self.prose("log --password -api_key= TAILX", "log --password " + R)
        self.prose("log --password user[pwd]= TAILX", "log --password " + R)
        self.prose("log --password https://example.invalid/?pwd= TAILX", "log --password " + R)
        self.prose("log --password token: TAILX", "log --password " + R)
        self.prose("log --password TAILV token: TAILX", "log --password " + R + " token: " + R)
        self.prose("log --password a=b then more", "log --password " + R + " then more")
        self.bundled("pair-rule-1e-k1-")

    def test_i2_escaped_name_is_decoded_once(self):
        R = self.R
        self.prose("log user%5Bpassword%5D=TAILX then more", "log user%5Bpassword%5D=" + R)
        self.prose("log user%5Bpwd%5D:TAILX then more", "log user%5Bpwd%5D:" + R)
        self.prose("log user%5Btoken%5D%3DTAILX then more", "log user%5Btoken%5D%3D" + R)
        self.prose("log user%255Bpassword%255D=TAILX then more", "log user%255Bpassword%255D=" + R)
        self.prose("a=1&user%5Bpassword%5D=TAILX&b=2", "a=1&user%5Bpassword%5D=" + R)
        self.prose("log user%5Bmodel%5D=keep then more", "log user%5Bmodel%5D=keep then more")
        self.bundled("pair-rule-1e-k2-")

    def test_i3_encoded_gap_after_a_name(self):
        R = self.R
        self.prose("log --password%20TAILV then more", "log --password%20" + R + " then more")
        self.prose("log --password%C2%A0TAILV then more", "log --password%C2%A0" + R + " then more")
        self.prose("log --password\\u2003TAILV then more", "log --password\\u2003" + R + " then more")
        self.prose("log --password%20--token%20TAILV", "log --password%20--token%20" + R)
        self.prose("log --token%20Bearer TAILVTAILV then more", "log --token%20" + R + " then more")
        self.prose("log --model%20keep then more", "log --model%20keep then more")
        self.bundled("pair-rule-1e-k3-")

    def test_i4_data_uri_continuation_lines(self):
        R = self.R
        self.prose("log data:,x\n--token= TAILX", "log <omitted: data URI>\n--token= " + R)
        self.prose("log data:,x\n-api_key= TAILX", "log <omitted: data URI>\n-api_key= " + R)
        self.prose("log data:text/plain;base64,QUJD\npassword=TAILX", "log <omitted: 4 base64 chars>\npassword=" + R)
        self.prose("log data:text/plain;base64,QUJD\nRA== then more", "log <omitted: 9 base64 chars> then more")
        self.bundled("pair-rule-1e-k4-")

    def test_i5_encoded_control_starts_the_value(self):
        R = self.R
        self.prose("log --password %0DTAILX then more", "log --password " + R + " then more")
        self.prose("log --password %0D%0ATAILX then more", "log --password " + R + " then more")
        self.prose("log --password \\nTAILX then more", "log --password " + R + " then more")
        self.prose("log --password \\bTAILX then more", "log --password " + R + " then more")
        self.prose("log --password %0D TAILX then more", "log --password " + R + " then more")
        self.prose("log --password %0D --token TAILX", "log --password " + R + " --token " + R)
        self.prose("log --password %20TAILX then more", "log --password %20" + R + " then more")
        self.bundled("pair-rule-1e-i5-")

    def test_bundle_diff_is_the_literal_changed_set(self):
        """The guard: the 7569 base cases (3b074675, the phase 1d bundle) keep their order, inputs, covers and expectations (the
        changed list is empty, notes/pair-rule-1e-r2-targets-1122.json); the 318 added cases are the literal list of
        notes/pair-rule-1e-r2-cases-1122.json, in order, after the base cases. Every added case is its input unchanged (the literal
        list of controls and blank-line cases) or its input with one or more non-empty runs replaced by the placeholder (or a data
        URI omission, I4); none leaves a TAIL marker of a credential value (the one literal exception is a name that runs on with no
        gap, where the marker is part of the name)."""
        diff = json.loads((self.NOTES / "pair-rule-1e-r2-bundle-diff-1122.json").read_text(encoding="utf-8"))
        targets = json.loads((self.NOTES / "pair-rule-1e-r2-targets-1122.json").read_text(encoding="utf-8"))
        rows = [line.split(" ", 2) for line in (self.NOTES / "pair-rule-1e-r2-base-digests-1122.txt").read_text(encoding="utf-8").splitlines()
                if not line.startswith("#")]
        rows = [(inputs, expected, json.loads(name)) for inputs, expected, name in rows]
        base = {name: (inputs, expected) for inputs, expected, name in rows}
        self.assertEqual((7569, 7887, 7887), (len(base), len(self.rows), len(self.bundle)), "rows, and no duplicate name")
        self.assertEqual(("3b0746756525b422c3243db4dde258a29b62e567", 7569, 7887), (diff["base"], diff["base_case_count"], diff["case_count"]))
        names = list(self.bundle)
        self.assertEqual([name for _, _, name in rows], names[:7569], "base cases keep their order and none is removed")
        self.assertEqual(diff["added"], names[7569:])
        self.assertEqual(diff["added"], [case["name"] for case in self.data["new_cases"]])
        self.assertEqual(diff["added"], targets["added"])
        changed_inputs = [name for name, (inputs, _) in base.items()
                          if PairRuleTests.digest({"covers": self.bundle[name]["covers"], "input": self.bundle[name]["input"]}) != inputs]
        self.assertEqual([], changed_inputs)
        changed = sorted(name for name, (_, expected) in base.items() if PairRuleTests.digest(self.bundle[name]["expected"]) != expected)
        self.assertEqual([], changed, "the changed list is empty")
        self.assertEqual(({}, {}, [], []), (diff["changed"], self.data["changed"], targets["changed"], targets["outside_classes"]))
        # Strictly more redacted, and no marker left, for every added case.
        unchanged = [name for name in diff["added"] if self.bundle[name]["input"]["value"] == self.bundle[name]["expected"]]
        self.assertEqual(diff["unchanged_added"], unchanged)
        self.assertTrue(all(any(tag in name for tag in ("-control-", "-blank-", "-short-flag-", "-cluster-")) for name in unchanged), unchanged)
        omission = re.compile(r"<omitted: (?:[0-9]+ base64 chars|data URI)>")
        for name in diff["added"]:
            if name in unchanged:
                continue
            case = self.bundle[name]
            with self.subTest(case=name):
                before, after = case["input"]["value"], case["expected"]
                if "-k4-" in name:
                    after = omission.sub("<redacted>", json.dumps(after, ensure_ascii=False))
                    before = json.dumps(before, ensure_ascii=False)
                self.assertTrue(PairRuleTests.more_redacted(before, after), name)
        kept = [name for name in diff["added"] if name not in unchanged and re.search(r"TAIL[A-Z]+", json.dumps(self.bundle[name]["expected"]))]
        self.assertEqual(["pair-rule-1e-single-dash-no-gap-prose", "pair-rule-1e-single-dash-no-gap-json"], kept)
        self.assertEqual(diff["marker_kept_added"], kept)

    def test_review_a_word_never_leaves_a_value_behind(self):
        """Review findings on 48deb365 (each value was replaced at 3859f0f1): an encoded gap at the start of the next line is gap, not
        the first word; a line of encoded whitespace only is no blank line; a separator after the value word and a gap on its line is
        read, the word being that pair's name (undecided after an unreadable gap)."""
        R = self.R
        for text, clean in (
                ("--password\n%20 SECRETA0", "--password\n%20 " + R),
                ("--password\n%09 SECRETA0 more", "--password\n" + R + " more"),
                ("-token\r%C2%A0  SECRETA0", "-token\r%C2%A0  " + R),
                ("--password\n%20\nSECRETA0 x", "--password\n%20\n" + R + " x"),
                ("--password log\u00a0=SECRETA0 x", "--password " + R),
                ("--password foo\u00a0%3DSECRETA0", "--password " + R),
                ("-apikeylog log\u00a0%3dSECRETA0", "-apikeylog " + R),
                ("-token SECRETA0 = SECRETB1", "-token " + R),
                ("-password log\t=SECRETA0", "-password " + R),
                ("--password log =keep then more", "--password " + R + " =keep then more"),
                ("--password\n\nSECRETA0 x", "--password\n\nSECRETA0 x")):
            with self.subTest(text=text):
                self.assertEqual(clean, self.reference.credential_pairs(text))

    def test_new_cases_are_generated_with_their_literal_expectation(self):
        new = self.data["new_cases"]
        self.assertEqual(318, len(new))
        missing, mismatched = [], []
        for case in new:
            actual = self.bundle.get(case["name"])
            if actual is None or actual["input"] != {"site": case["site"], "path": case["path"], "value": case["value"]}:
                missing.append(case["name"])
            elif actual["expected"] != case["expected"]:
                mismatched.append(case["name"])
            if self.reference.scrub(case["value"], case["path"], framed=case["site"] != "free_text_urls") != case["expected"]:
                mismatched.append(case["name"] + " (reference)")
        self.assertEqual(([], []), (missing[:5], mismatched[:5]), f"{len(missing)} missing, {len(mismatched)} mismatched")


class SharedCopyTests(unittest.TestCase):
    ROOT = SPEC.parents[1]
    COPIES = (
        "csharp/src/TraceAI.AutoInstrumentation/Wire/privacy.json",
        "go/auto/internal/wire/privacy.json",
        "java/traceai-javaagent/src/main/resources/ai/traceai/auto/privacy.json",
        "python/fi_instrumentation/auto/wire/privacy.json",
        "ruby/lib/traceai/auto/privacy.json",
        "typescript/packages/fi-core/src/privacy.json",
        "typescript/packages/traceai_auto/src/wire/privacy.json",
    )

    def test_all_eight_privacy_copies_are_byte_identical(self):
        expected = (SPEC / "privacy.json").read_bytes()
        for relative in self.COPIES:
            self.assertEqual(expected, (self.ROOT / relative).read_bytes(), relative)

    def test_jwt_shape_is_unbounded_and_has_no_trailing_boundary(self):
        # A token is replaced whole whatever its length; each SDK scans runs once instead of bounding the shape.
        catalog = json.loads((SPEC / "privacy.json").read_text())
        jwt = [s["regex"] for s in catalog["credentials"]["value_shapes"] if "eyJ" in s["regex"]]
        self.assertEqual(["\\beyJ[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}"], jwt)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="traceai-spec-sync-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.spec = self.root / "spec" / "auto-instrumentation"
        self.spec.mkdir(parents=True)
        for name in sync.FILES:
            (self.spec / name).write_bytes(b"canonical " + name.encode("ascii") + b"\n")
        self.addCleanup(patch.stopall)
        patch.object(sync, "ROOT", self.root).start()
        patch.object(sync, "SPEC_DIR", self.spec).start()
        self.first = self.root / sync.SDK_WIRE_DIRS["python"]
        self.first.mkdir(parents=True)
        self.copy(self.first, "wire-protocols.json")

    def copy(self, directory, name, contents=None):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_bytes(contents if contents is not None else (self.spec / name).read_bytes())
        return path

    def run_check(self, **kwargs):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            status = sync.check(**kwargs)
        return status, output.getvalue()

    def test_missing_unadopted_sdk_reported_without_error(self):
        status, output = self.run_check()
        self.assertEqual(0, status)
        self.assertEqual(12, output.count("adoption pending; non-blocking"))
        self.assertIn("0 errors", output)
        self.assertFalse((self.first / "privacy.json").exists())

    def test_strict_flag_requires_all_six_sdks_and_both_files(self):
        status, output = self.run_check(require_privacy=True)
        self.assertEqual(1, status)
        self.assertIn("12 errors", output)
        for relative in sync.SDK_WIRE_DIRS.values():
            for name in sync.FILES:
                self.copy(self.root / relative, name)
        status, output = self.run_check(require_privacy=True)
        self.assertEqual(0, status)
        self.assertIn("0 pending/missing", output)

    def test_testdata_copies_cannot_satisfy_shipping_adoption(self):
        for sdk, relative in sync.SDK_ROOTS.items():
            self.copy(self.root / sync.SDK_WIRE_DIRS[sdk], "wire-protocols.json")
            for name in ("privacy.json", "privacy-fixtures.json"):
                self.copy(self.root / relative / "tests" / "data", name)
        status, output = self.run_check(require_privacy=True)
        self.assertEqual(1, status)
        self.assertIn("12 errors", output)
        self.assertIn("python/fi_instrumentation/auto/wire/privacy.json", output)
        status, output = self.run_check()
        self.assertEqual(0, status)
        self.assertIn("12 pending/missing", output)

    def test_embedded_shipping_catalog_requires_adjacent_privacy_files(self):
        for relative in sync.SDK_WIRE_DIRS.values():
            for name in sync.FILES:
                self.copy(self.root / relative, name)
        embedded = self.root / "go/launcher/embedded/ruby/traceai/auto"
        self.copy(embedded, "wire-protocols.json")
        status, output = self.run_check(require_privacy=True)
        self.assertEqual(1, status)
        self.assertIn("2 errors", output)
        self.assertIn("go/launcher/embedded/ruby/traceai/auto/privacy.json", output)
        for name in ("privacy.json", "privacy-fixtures.json"):
            self.copy(embedded, name)
        self.assertEqual(0, self.run_check(require_privacy=True)[0])

    def test_stale_catalog_is_error_even_before_adoption(self):
        path = self.copy(self.first, "privacy.json", b"stale\n")
        status, output = self.run_check()
        self.assertEqual(1, status)
        self.assertIn("stale", output)
        self.assertEqual(b"stale\n", path.read_bytes())

    def test_stale_fixture_bundle_is_error(self):
        self.copy(self.first, "privacy.json")
        self.copy(self.first, "privacy-fixtures.json", b"stale\n")
        self.assertEqual(1, self.run_check()[0])

    def test_every_duplicate_and_embedded_shipping_copy_is_checked(self):
        self.copy(self.first, "privacy.json")
        self.copy(self.root / "go" / "launcher" / "embedded" / "ruby", "privacy.json", b"stale\n")
        self.assertEqual(1, self.run_check()[0])
        self.assertEqual(2, len(sync.copies("privacy.json")))

    def test_build_cache_scratch_and_symlink_trees_are_pruned(self):
        for skip in ("target", "node_modules", ".scratch", ".cache", ".venv-worker"):
            self.copy(self.root / "go" / skip, "privacy.json", b"stale\n")
        (self.root / "linked-sdk").symlink_to(self.first, target_is_directory=True)
        self.assertEqual([], sync.copies("privacy.json"))
        self.assertEqual(0, self.run_check()[0])

    def test_write_only_updates_existing_files(self):
        stale = self.copy(self.first, "privacy.json", b"stale\n")
        status, output = self.run_check(write=True)
        self.assertEqual(0, status)
        self.assertIn("1 updated", output)
        self.assertEqual((self.spec / "privacy.json").read_bytes(), stale.read_bytes())
        self.assertFalse((self.first / "privacy-fixtures.json").exists())

    def test_missing_wire_and_missing_spec_source_fail(self):
        (self.first / "wire-protocols.json").unlink()
        self.assertEqual(1, self.run_check()[0])
        self.copy(self.first, "wire-protocols.json")
        (self.spec / "privacy.json").unlink()
        self.assertEqual(1, self.run_check()[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
