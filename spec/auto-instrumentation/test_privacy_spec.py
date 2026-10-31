"""S6 generator/catalog and sync regression tests. No SDK code is imported."""

import contextlib
import importlib.util
import io
import json
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

    def test_logprob_context_and_ordinary_names_preserved(self):
        value = {"choices": [{"logprobs": {"content": [{"token": "Hello", "logprob": -0.2,
                 "top_logprobs": [{"token": "world", "logprob": -0.3}]}]}}],
                 "author_name": "Ada", "keywords": ["ordinary", "words"], "signal_strength": 0.7}
        self.assertEqual(value, self.reference.scrub(value))
        self.assertEqual((), self.reference.harvest({"json": value}))
        for exclusion in self.catalog["credentials"]["contextual_exclusions"]:
            for path in exclusion["examples"]:
                self.assertFalse(self.reference.credential_name(path))
                self.assertEqual("Hello", self.reference.scrub("Hello", path))
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
                    expected = base + "?" + raw_name + "=<redacted>&model=keep#fragment?token=ordinary"
                    with self.subTest(name=raw_name, base=base):
                        self.assertEqual((secret,), self.reference.harvest({"url": url}))
                        self.assertEqual(expected, self.reference.scrub(url))
                        self.assertEqual({"url": expected}, self.reference.scrub({"url": url}))
                        self.assertEqual("see " + expected + " next", self.reference.scrub("see " + url + " next"))
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

    def test_single_quoted_url_preserves_enclosing_quotes(self):
        url = "'https://example.invalid/?token='SYNTHETIC_PRIVATE_20261030'"
        expected = "'https://example.invalid/?token=<redacted>'"
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
                expected = first + separator + "https://example.invalid/?token=<redacted>&model=keep#anchor"
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
        for first, clean in (("https://example.invalid/?token=synthetic", "https://example.invalid/?token=<redacted>"),
                             ("https://example.invalid/?audio=AQIDBA==", "https://example.invalid/?audio=<omitted: 8 base64 chars>")):
            for suffix in (" trailing prose", " then https://example.invalid/?model=keep#anchor"):
                with self.subTest(first=first, suffix=suffix):
                    self.assertEqual(clean + suffix, self.reference.scrub(first + suffix))
                    self.assertEqual(clean + suffix, self.reference.scrub(first + suffix, "exception.message"))

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

    def test_fragment_queries_remain_unchanged(self):
        for url in ("#fragment?token=ordinary", "https://example.invalid/#fragment?token=ordinary",
                    "https://example.invalid/?model=keep#fragment?token=ordinary",
                    "https://example.invalid/?model=why?token=ordinary"):
            self.assertEqual(url, self.reference.scrub(url))
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
        value = "hello"
        for _ in range(self.catalog["limits"]["max_depth"] + 2):
            value = [value]
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
