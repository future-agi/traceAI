"""Generate privacy-fixtures.json and validate the S6 catalog (stdlib only).

Edit the cases here, never the generated JSON. --check is strictly read-only.
The reference below is an executable recognition contract, not an SDK exporter:
S1 detached snapshots/hide flags and S5 registry lifecycle remain SDK duties.
"""

import argparse
import base64
import binascii
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, unquote_plus, urlsplit

SPEC = Path(__file__).resolve().parent
OUTPUT = SPEC / "privacy-fixtures.json"
BASE64 = re.compile(r"[A-Za-z0-9+/=_\-\r\n]+\Z")
# Consume the whole token even when there is no comma. A failed data: prefix
# must never cause rescanning of its suffix (the round-four quadratic case).
DATA = re.compile(r'''(?<![A-Za-z0-9+.\-_:])data:[^\s"'<>\\]*(?:[\r\n]+[A-Za-z0-9+/_=\-]+)*''', re.I)
OMISSION = re.compile(r"<omitted: (?:[0-9]+ base64 chars|media|data URI)>\Z")
# First isolate a URI token, including stable replacement markers. Query parsing
# is then linear and cannot mistake a question mark in a fragment/name for the
# first query delimiter. Relative references must begin at a text boundary.
# Apostrophes are legal URI sub-delimiters, including in query names/values.
URL = re.compile(r'''(?:https?://|(?<!\S)[/?])(?:<redacted>|<omitted: (?:[0-9]+ base64 chars|media|data URI)>|[^\s"<>\\])+''', re.I)
# Give URL framing precedence over data: scanning so a literal query data URI
# cannot consume the following &parameter or #fragment as part of its payload.
TEXT = re.compile(rf"(?P<url>{URL.pattern})|(?P<data>{DATA.pattern})", re.I)
USERINFO = re.compile(r"^(https?://)[^\s/@]+:[^\s/@]+@", re.I)


def normalize(name):
    return name.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")).translate(
        str.maketrans("", "", "-_."))


def components(path):
    return [part for part in re.split(r"[.\[\]]+", path) if part and not part.isdecimal()]


def leaf(path):
    parts = components(path)
    return normalize(parts[-1]) if parts else ""


class Reference:
    def __init__(self, catalog):
        self.catalog = catalog
        self.credentials = catalog["credentials"]
        self.patterns = [item["contains"] for item in self.credentials["patterns"]]
        patterns = {item["id"]: item["contains"] for item in self.credentials["patterns"]}
        self.exclusions = [(re.compile(item["regex"]), [patterns[name] for name in item.get("unless_contains_patterns", ())])
                           for item in self.credentials["exclusions"]]
        self.contextual_exclusions = [re.compile(item["path_regex"]) for item in self.credentials["contextual_exclusions"]]
        self.signed = {normalize(name) for name in catalog["signed_url"]["parameters"]}
        self.media = catalog["media"]
        self.mime = self.media["mime_siblings"]
        self.audio = self.media["audio"]

    def excluded(self, name):
        folded = normalize(name)
        return any(regex.fullmatch(folded) and not any(marker in folded for marker in guards)
                   for regex, guards in self.exclusions)

    def credential_name(self, name):
        parts = [normalize(part) for part in components(name)]
        excluded = False
        # Exclusions apply to one component, never the rest of the path. In
        # particular a logprob token exemption cannot excuse secret ancestors,
        # and wildcard metric exclusions cannot hide independent auth markers.
        for index, folded in enumerate(parts):
            context = ".".join(parts[:index + 1])
            if self.excluded(folded) or any(regex.fullmatch(context) for regex in self.contextual_exclusions):
                excluded = True
            elif any(pattern in folded for pattern in self.patterns):
                return True
        if excluded:
            return False
        folded = normalize(name)
        return not self.excluded(name) and any(pattern in folded for pattern in self.patterns)

    def query_name(self, name):
        return normalize(name) in self.signed or self.credential_name(name)

    def magic(self, value):
        if not isinstance(value, str) or not BASE64.fullmatch(value):
            return None
        # Decode at most 32 bytes, never the full media. Alphabet checking is
        # linear in the already-bounded captured text. Accept URL-safe/unpadded
        # base64 and CR/LF wrapping in every SDK.
        compact = value.replace("\r", "").replace("\n", "")
        count = self.media["base64_prefix_decoded_bytes"]
        prefix = compact[:(count * 8 + 5) // 6]
        try:
            decoded = base64.b64decode(prefix.translate(str.maketrans("-_", "+/")) + "=" * (-len(prefix) % 4), validate=True)[:count]
        except (ValueError, binascii.Error):
            return None
        for rule in self.media["magic"]:
            matched = True
            for test in rule["tests"]:
                expected = bytes.fromhex(test["hex"])
                mask = bytes.fromhex(test.get("mask", "ff" * len(expected)))
                actual = decoded[test["offset"]:test["offset"] + len(expected)]
                if len(actual) != len(expected) or any((a & m) != (e & m) for a, e, m in zip(actual, expected, mask)):
                    matched = False
                    break
            if matched:
                return rule["id"]
        return None

    def hint(self, name, value):
        if isinstance(value, list):
            hints = [self.hint(name, item) for item in value if isinstance(item, str)]
            return any(media for media, _ in hints), any(audio for _, audio in hints)
        if not isinstance(value, str):
            return False, False
        folded = leaf(name)
        mime = value.split(";", 1)[0].strip().lower()
        if folded in self.mime["names"]:
            return (any(mime.startswith(prefix) for prefix in self.mime["prefixes"]) or mime in self.mime["exact"],
                    mime.startswith("audio/"))
        if folded == "type" and normalize(value) in self.audio["type_names"]:
            return False, True
        return False, False

    @staticmethod
    def hint_parent(path):
        parts = [part for part in re.split(r"[.\[\]]+", path) if part]
        while parts and parts[-1].isdecimal():
            parts.pop()
        return ".".join(parts[:-1])

    def index_hints(self, items, path="", hints=None):
        """One hint index for native/flat objects, attributes and query pairs.

        Accept pairs rather than a dict so duplicate query hints combine without
        losing a media hint to a later non-media value. Names retain indices.
        """
        indexed = dict(hints or {})
        for name, item in items:
            hint = self.hint(name, item)
            if any(hint):
                full_name = path + "." + name if path else name
                parent = self.hint_parent(full_name)
                previous = indexed.get(parent, (False, False))
                indexed[parent] = (previous[0] or hint[0], previous[1] or hint[1])
        return indexed

    def context(self, path, media=False, audio=False, hints=None):
        hints = hints or {}
        hint_media, hint_audio = hints.get("", (False, False))
        media, audio = media or hint_media, audio or hint_audio
        payload, direct_audio, prefix = False, False, []
        # An audio field itself (including array indices) can carry samples, but
        # named children of an audio object are not all payload. Once an explicit
        # payload field starts, arbitrary wrappers remain payload until metadata.
        # Hints apply only at their exact indexed parent.
        for part in filter(None, re.split(r"[.\[\]]+", path)):
            prefix.append(part)
            name = normalize(part)
            if not part.isdecimal():
                if name in self.audio["metadata_fields"]:
                    media, audio, payload = False, False, False
                # Direct audio samples are local to this field and its indices;
                # explicit payload ancestry is independent and survives wrappers,
                # including another audio field. A MIME payload field can also be
                # an audio field (audio_base64), so these are not exclusive tests.
                direct_audio = name in self.audio["fields"]
                audio = audio or direct_audio
                if ((media and name in self.mime["payload_fields"])
                        or (audio and name in self.audio["payload_fields"])):
                    payload = True
            hint_media, hint_audio = hints.get(".".join(prefix), (False, False))
            media, audio = media or hint_media, audio or hint_audio
        return media, audio, payload or direct_audio

    def payload(self, path, media=False, audio=False, hints=None):
        return self.context(path, media, audio, hints)[2]

    def media_string(self, value, path, media=False, audio=False, hints=None, framed=True):
        if value == self.credentials["replacement"] or OMISSION.fullmatch(value):
            return None
        if framed:
            replacement = self.data_uri(value)
            if replacement is not None:
                return replacement
        encoded = bool(BASE64.fullmatch(value))
        if self.magic(value) or self.payload(path, media, audio, hints):
            return omitted(value) if encoded else self.media["other_replacement"]
        if (encoded and leaf(path) in self.media["unrecognized_base64_fields"]
                and len(value) > self.media["unrecognized_base64_min_chars_exclusive"]):
            return omitted(value)
        return None

    def data_uri(self, value):
        """Recognize one whole value; payload delimiters are irrelevant here."""
        if value[:5].lower() != "data:":
            return None
        metadata, comma, payload = value[5:].partition(",")
        if not comma:
            return None
        payload = unquote(payload)  # '+' is a base64 character, not a space.
        if metadata.lower().endswith(";base64") and (not payload or BASE64.fullmatch(payload)):
            return omitted(payload)
        return self.media["data_uri"]["other_replacement"]

    def data_text(self, value, framed=False):
        """Only free text needs delimiter-limited, nonrecursive URI scanning."""
        if framed:
            replacement = self.data_uri(value)
            if replacement is not None:
                return replacement
        return DATA.sub(lambda match: self.data_uri(match.group(0)) or match.group(0), value)

    def url_value(self, original):
        """Scrub one already-framed URL; never delimit its query values as prose."""
        target, hashmark, fragment = original.partition("#")
        base, question, raw_query = target.partition("?")
        base = USERINFO.sub(r"\1<redacted>@", base)
        parsed = []
        for pair in raw_query.split("&"):
            raw_name, equals, raw_value = pair.partition("=")
            parsed.append((raw_name, equals, raw_value, unquote_plus(raw_name), unquote_plus(raw_value)))
        # Queries are flat objects, not unrelated scalars. Index every hint
        # before scrubbing any value, retaining duplicates and original bytes.
        hints = self.index_hints((name, decoded) for _, equals, _, name, decoded in parsed if equals)
        pairs = []
        for raw_name, equals, raw_value, name, decoded in parsed:
            if equals:
                if self.query_name(name):
                    raw_value = self.credentials["replacement"]
                else:
                    replacement = self.media_string(decoded, name, hints=hints)
                    if replacement is None:
                        # Do not recurse through text()/URL decoding: a query
                        # adds one serialization layer, not an unbounded scan
                        # of nested percent-encoded URLs.
                        cleaned = self.data_text(decoded, framed=True)
                        if cleaned != decoded:
                            replacement = cleaned
                    if replacement is not None:
                        raw_value = replacement
            pairs.append(raw_name + equals + raw_value)
        return self.data_text(base) + question + "&".join(pairs) + hashmark + self.data_text(fragment)

    def is_url_value(self, value):
        """Validate the whole URL, not just a URL prefix in captured prose.

        A literal data-URI query value owns its payload's delimiters. Mask only
        those values for lexical validation; never mask credential/audio values
        here, since redaction could hide trailing prose or another URL. Preserve
        the original value for the actual query parser.
        """
        if URL.fullmatch(value):
            return True
        if not re.match(r"(?:https?://|[/?])", value, re.I):
            return False
        target, hashmark, fragment = value.partition("#")
        base, question, raw_query = target.partition("?")
        if not question:
            return False
        pairs = []
        for pair in raw_query.split("&"):
            raw_name, equals, raw_value = pair.partition("=")
            if equals and self.data_uri(unquote_plus(raw_value)) is not None:
                raw_value = self.media["data_uri"]["other_replacement"]
            pairs.append(raw_name + equals + raw_value)
        candidate = base + question + "&".join(pairs) + hashmark + fragment
        return candidate != value and URL.fullmatch(candidate) is not None

    def text(self, value, secrets, framed=False):
        def token(match):
            original = match.group(0)
            if match.lastgroup == "data":
                return self.data_text(original)
            # Retain enclosing single quotes in prose without treating internal
            # apostrophes (even the first character of a value) as delimiters.
            closing_quote = ""
            if match.start() and value[match.start() - 1] == "'" and original.endswith("'"):
                original, closing_quote = original[:-1], "'"
            return self.url_value(original) + closing_quote

        # An outer scalar boundary does not prove the value is a single URL:
        # messages/errors may start with one URL and contain several more. Exact
        # URLs still frame literal data-URI queries before prose tokenization.
        if framed and self.is_url_value(value):
            value = self.url_value(value)
        else:
            value = TEXT.sub(token, value)
        for secret in sorted({s for s in secrets if len(s) >= 8 and s != "<redacted>"}, key=lambda s: (-len(s), s)):
            value = "<redacted>".join(part.replace(secret, "<redacted>") for part in value.split("<redacted>"))
        return value

    def scrub(self, value, path="", siblings=None, secrets=(), _media=False, _audio=False, _depth=0, _budget=None, _hints=None, framed=True):
        limits = self.catalog["limits"]
        budget = _budget if _budget is not None else [limits["max_nodes"], limits["max_text_chars"]]
        budget[0] -= 1
        if _depth > limits["max_depth"] or budget[0] < 0:
            raise ValueError("privacy structure budget")
        if self.credential_name(path):
            return self.credentials["replacement"]
        # Index hints once. Recompute context in path order so metadata resets
        # ancestry and a later local MIME hint can start its own independent scope.
        hints = _hints if _hints is not None else {}
        if siblings:
            hints = self.index_hints(siblings.items(), hints=hints)
        if isinstance(value, dict):
            if len(value) > budget[0]:
                raise ValueError("privacy structure budget")
            budget[1] -= sum(len(name) for name in value)
            if budget[1] < 0:
                raise ValueError("privacy text budget")
            hints = self.index_hints(value.items(), path, hints)
            out = {}
            for name, item in value.items():
                child_path = path + "." + name if path else name
                out[name] = self.scrub(item, child_path, None, secrets, _media, _audio, _depth + 1, budget, hints)
            return out
        media, audio, payload = self.context(path, _media, _audio, hints)
        if isinstance(value, list):
            if len(value) > budget[0]:
                raise ValueError("privacy structure budget")
            if payload and value and all(isinstance(v, (int, float, bool)) for v in value):
                return self.media["other_replacement"]
            return [self.scrub(item, path + "." + str(i), None, secrets, _media, _audio, _depth + 1, budget, hints)
                    for i, item in enumerate(value)]
        if not isinstance(value, str):
            if value is not None and payload:
                return self.media["other_replacement"]
            return value
        budget[1] -= len(value)
        if budget[1] < 0:
            raise ValueError("privacy text budget")
        replacement = self.media_string(value, path, _media, _audio, hints, framed)
        if replacement is not None:
            return replacement
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except ValueError:
                pass  # Malformed/embedded JSON policy belongs to the S1 boundary.
            else:
                clean = self.scrub(parsed, "", None, secrets, media, audio, _depth + 1, budget)
                if clean != parsed:
                    value = json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
        return self.text(value, secrets, framed=framed)

    def harvest(self, request):
        """Only already-parsed credentials; never read a transport or buffer bodies."""
        found = set()

        def walk(value, path=""):
            if isinstance(value, dict):
                for name, item in value.items():
                    walk(item, path + "." + name if path else name)
            elif isinstance(value, list):
                for item in value:
                    walk(item, path)
            elif isinstance(value, str) and self.credential_name(path):
                found.add(value)
                if leaf(path).endswith("authorization") and len(value.split(None, 1)) == 2:
                    found.add(value.split(None, 1)[1])
        walk(request.get("headers", {}))
        walk(request.get("json", {}))
        walk(request.get("multipart", {}).get("fields", {}))
        for name, value in parse_qsl(urlsplit(request.get("url", "")).query):
            if self.query_name(name):
                found.add(value)
        return tuple(sorted((s for s in found if len(s) >= 8), key=lambda s: (-len(s), s)))


def omitted(value):
    return f"<omitted: {len(value)} base64 chars>"


def golden_cases(catalog):
    cases = []
    secret = "SYNTHETIC_PRIVATE_20261030"
    redacted = "<redacted>"

    def add(name, site, value, expected, path="", covers=(), siblings=None, secrets=()):
        inputs = {"site": site, "path": path, "value": value}
        if siblings:
            inputs["siblings"] = siblings
        if secrets:
            inputs["known_secrets"] = list(secrets)
        cases.append({"name": name, "covers": list(covers), "input": inputs, "expected": expected})

    def sites(prefix, name, private, covers):
        original, expected = (secret, redacted) if private else (17, 17)
        add(prefix + "-headers", "request_headers", {name: original}, {name: expected}, covers=covers)
        url = "https://example.invalid/object?" + quote(name, safe="") + "=" + str(original) + "&model=keep#anchor"
        clean_url = "https://example.invalid/object?" + quote(name, safe="") + "=" + str(expected) + "&model=keep#anchor"
        add(prefix + "-query", "request_query", url, clean_url, covers=covers)
        add(prefix + "-request", "request_body", {"nested": [{name: original}]}, {"nested": [{name: expected}]}, covers=covers)
        add(prefix + "-response", "response_body", {"result": {name: original}}, {"result": {name: expected}}, covers=covers)
        for site, path in (("attribute_keys", name), ("flattened_paths", "metadata." + name),
                           ("indexed_paths", "metadata." + name + ".0")):
            add(prefix + "-" + site, site, original, expected, path, covers)

    def url_sites(prefix, original, expected, covers):
        # Returned URLs deliberately have no S5 seed: export must recognize them
        # on its own, not pass only because request observation registered them.
        add(prefix + "-query", "request_query", original, expected, covers=covers)
        add(prefix + "-response", "response_body", {"url": original}, {"url": expected}, covers=covers)
        add(prefix + "-text", "free_text_urls", "see " + original + " next", "see " + expected + " next", covers=covers)
        add(prefix + "-attribute", "attribute_keys", original, expected, "http.url", covers)

    def bounded_url_sites(prefix, original, expected, covers):
        # Literal query payloads may contain prose delimiters. Only exact URL
        # values, not URLs discovered inside prose, supply this outer boundary.
        add(prefix + "-query", "request_query", original, expected, covers=covers)
        add(prefix + "-response", "response_body", {"url": original}, {"url": expected}, covers=covers)
        add(prefix + "-attribute", "attribute_keys", original, expected, "http.url", covers)
        add(prefix + "-indexed", "indexed_paths", original, expected, "output.urls.0", covers)
        add(prefix + "-array", "response_body", [original], [expected], covers=covers)

    def query_object_sites(prefix, pairs):
        """Pairs are (name, original, expected); preserve unchanged query bytes."""
        original, expected = [], []
        for name, value, clean in pairs:
            raw_name, raw_value = quote(name, safe=""), quote(value, safe="")
            original.append(raw_name + "=" + raw_value)
            expected.append(raw_name + "=" + (raw_value if clean == value else clean))
        url_sites(prefix, "https://example.invalid/?" + "&".join(original) + "&model=keep#anchor",
                  "https://example.invalid/?" + "&".join(expected) + "&model=keep#anchor", ["query-media-object"])

    for item in catalog["credentials"]["patterns"]:
        sites("pattern-" + item["id"], item["example"], True, ["pattern:" + item["id"]])
    for item in catalog["credentials"]["exclusions"]:
        for i, name in enumerate(item["examples"]):
            sites("exclusion-" + item["id"] + "-" + str(i), name, False, ["exclusion:" + item["id"]])
    for item in catalog["credentials"]["exclusions"]:
        for marker in item.get("unless_contains_patterns", ()):
            for suffix in ("_tokens", "_tokens_details", "_token_details", "_token_count", "_token_counts"):
                sites(f"metric-guard-{item['id']}-{marker}-{suffix}", "provider_" + marker + suffix, True,
                      ["exclusion-guard:" + item["id"] + ":" + marker])
    for item in catalog["credentials"]["contextual_exclusions"]:
        for i, path in enumerate(item["examples"]):
            for site in ("attribute_keys", "flattened_paths", "indexed_paths"):
                add(f"contextual-{item['id']}-{i}-{site}", site, "Hello", "Hello", path,
                    ["contextual-exclusion:" + item["id"]])
                add(f"contextual-ancestor-{item['id']}-{i}-{site}", site, secret, redacted, "secret." + path)
                add(f"contextual-descendant-{item['id']}-{i}-{site}", site, secret, redacted, path + ".api_key")
    logprobs = {"choices": [{"logprobs": {"content": [{"token": "Hello", "logprob": -0.2,
                 "top_logprobs": [{"token": "world", "logprob": -0.3}]}]}}]}
    add("ordinary-logprobs-content", "response_body", logprobs, logprobs)
    for name in ("author_name", "keywords", "signal_strength"):
        add("ordinary-prose-" + name, "response_body", {name: "ordinary words"}, {name: "ordinary words"})
    for prefix in ("input", "prompt", "completion", "output"):
        for token in ("token", "tokens"):
            name = prefix + "_" + token + "_details"
            counters = {"cache_read": 3, "cache_creation": 5, "audio_tokens": 7, "text_tokens": 11,
                        "reasoning_tokens": 13, "accepted_prediction_tokens": 17, "rejected_prediction_tokens": 19}
            for site in ("request_body", "response_body", "attribute_keys", "flattened_paths", "indexed_paths"):
                path = "gen_ai.usage." + name + (".0" if site == "indexed_paths" else "")
                add(f"token-details-object-{name}-{site}", site, counters, counters, path, ["token-details-objects"])
                add(f"token-details-counter-{name}-{site}", site, 3, 3, path + ".cache_read", ["token-details-objects"])
            value = {"usage": {name: counters}, "audio": {name: counters}}
            add("token-details-native-" + name, "response_body", value, value, covers=["token-details-objects"])
            url_sites("token-details-query-" + name, "https://example.invalid/?" + name + ".cache_read=3&model=keep",
                      "https://example.invalid/?" + name + ".cache_read=3&model=keep", ["token-details-objects"])
            value = {name: {"cache_read": 3, "api_key": secret}, "client_secret_" + name: secret}
            expected = {name: {"cache_read": 3, "api_key": redacted}, "client_secret_" + name: redacted}
            add("token-details-independent-credential-" + name, "request_body", value, expected,
                covers=["token-details-objects", "metric-credential-precedence"])
    for name in ("client_secret_tokens", "authorization_tokens", "api_key_tokens", "password_token_count"):
        sites("independent-credential-" + name, name, True, ["metric-credential-precedence"])
    names = set(catalog["credentials"]["seed_names"])
    names.update(name for item in catalog["credentials"]["missed_name_sources"] for name in item["names"])
    for i, name in enumerate(sorted(names)):
        sites("seed-" + str(i), name, True, ["seed:" + name])
    for i, name in enumerate(catalog["signed_url"]["parameters"]):
        for variant, raw_name in (("plain", name), ("upper", name.upper()), ("encoded", "%" + format(ord(name[0]), "02X") + name[1:])):
            url = f"https://example.invalid/object?{raw_name}={secret}&model=keep&{raw_name}=second#anchor"
            expected = f"https://example.invalid/object?{raw_name}=<redacted>&model=keep&{raw_name}=<redacted>#anchor"
            add(f"signed-{i}-{variant}", "free_text_urls", "see " + url + " next", "see " + expected + " next", covers=["signed:" + name])
            add(f"signed-request-{i}-{variant}", "request_query", url, expected, covers=["signed:" + name])
    for name in ("client_secret", "authorization_token", "password", "refresh_token", "api-key", "x-vault-token"):
        add("returned-query-" + name, "response_body", {"url": "https://example.invalid/?" + name + "=" + secret},
            {"url": "https://example.invalid/?" + name + "=<redacted>"})
    add("normalization-dots-case", "attribute_keys", secret, redacted, "X.Provider.Api.Key")
    add("normalization-bracket-credential", "indexed_paths", secret, redacted, "metadata.client_secret[0]")
    add("credential-object", "request_body", {"provider_token": {"value": secret}}, {"provider_token": redacted})
    add("credential-typed-values", "response_body", {"api_key": 123456789, "password": False, "secret": None},
        {"api_key": redacted, "password": redacted, "secret": redacted})
    add("credential-array", "attribute_keys", [secret, "second"], redacted, "authorization")
    add("credential-ancestor-beats-exclusion", "flattened_paths", 17, redacted, "metadata.secret.input_tokens")
    add("metric-ancestor-not-blanket-exemption", "flattened_paths", secret, redacted, "gen_ai.usage.tokens.api_key")
    add("metric-reasoning-descendant", "flattened_paths", 17, 17, "gen_ai.usage.output_tokens.reasoning")
    add("metric-indexed", "indexed_paths", 17, 17, "gen_ai.usage.token_counts[0]")
    add("key-id-does-not-hide-key", "request_body", {"key_id": "public-id", "private_key": secret}, {"key_id": "public-id", "private_key": redacted})
    add("userinfo", "free_text_urls", "see https://user:password@example.invalid/a", "see https://<redacted>@example.invalid/a")
    add("empty-query-secret", "free_text_urls", "https://example.invalid/?token=&model=keep", "https://example.invalid/?token=<redacted>&model=keep")
    add("already-redacted-query", "free_text_urls", "https://example.invalid/?token=<redacted>&model=keep", "https://example.invalid/?token=<redacted>&model=keep")
    add("encoded-credential-query", "free_text_urls", "https://example.invalid/?client%2Esecret=" + secret,
        "https://example.invalid/?client%2Esecret=<redacted>")
    add("ordinary-query", "free_text_urls", "https://example.invalid/?key_id=public&max_tokens=23&design=keep",
        "https://example.invalid/?key_id=public&max_tokens=23&design=keep")
    for name in ("client_secret?tag", "password?foo", "token?filter"):
        url = "https://example.invalid/?" + name + "=" + secret + "&model=keep#fragment?token=ordinary"
        expected = "https://example.invalid/?" + name + "=<redacted>&model=keep#fragment?token=ordinary"
        for site in ("request_query", "response_body", "free_text_urls"):
            add("literal-question-query-" + name + "-" + site, site, url, expected, covers=["url-query-boundaries"])
    for i, name in enumerate(("client_secret'tag", "password'foo", "token'filter")):
        for encoding, raw_name in (("literal", name), ("encoded", quote(name, safe=""))):
            for root, base in (("absolute", "https://example.invalid/"), ("relative", "/path")):
                url = base + "?" + raw_name + "=" + secret + "&model=keep#fragment?token=ordinary"
                expected = base + "?" + raw_name + "=<redacted>&model=keep#fragment?token=ordinary"
                url_sites(f"apostrophe-name-{i}-{encoding}-{root}", url, expected, ["url-apostrophes"])
    for i, credential in enumerate(("'" + secret, secret + "'suffix", secret + "'", "'" + secret + "'")):
        url = "https://example.invalid/?token=" + credential + "&model=keep#anchor"
        expected = "https://example.invalid/?token=<redacted>&model=keep#anchor"
        url_sites(f"apostrophe-value-{i}", url, expected, ["url-apostrophes"])
    url_sites("apostrophe-enclosing-quotes", "'https://example.invalid/?token='" + secret + "'",
              "'https://example.invalid/?token=<redacted>'", ["url-apostrophes"])
    url_sites("apostrophe-ordinary-value", "https://example.invalid/?model=author's-model&key_id=public",
              "https://example.invalid/?model=author's-model&key_id=public", ["url-apostrophes"])
    for i, url in enumerate(("https://example.invalid/#fragment?token=ordinary",
                             "https://example.invalid/?model=keep#fragment?token=ordinary",
                             "#fragment?token=ordinary", "/path#fragment?token=ordinary")):
        add("fragment-preserved-" + str(i), "free_text_urls", "see " + url + " next", "see " + url + " next",
            covers=["url-query-boundaries"])
    add("relative-query-reference", "free_text_urls", "see /path?token?filter=" + secret + "#fragment?token=ordinary next",
        "see /path?token?filter=<redacted>#fragment?token=ordinary next", covers=["url-query-boundaries"])
    add("query-value-question-preserved", "free_text_urls", "https://example.invalid/?model=why?token=ordinary",
        "https://example.invalid/?model=why?token=ordinary", covers=["url-query-boundaries"])
    add("multiple-url-fragments", "free_text_urls",
        "https://example.invalid/#fragment?token=ordinary then https://example.invalid/?token=" + secret,
        "https://example.invalid/#fragment?token=ordinary then https://example.invalid/?token=<redacted>", covers=["url-query-boundaries"])
    # A bounded message may begin with a URL without being one URL. No registry
    # seeds: each later URL must be independently recognized at every value site.
    for first in ("https://example.invalid/#fragment", "https://example.invalid/?model=keep",
                  "/path#fragment", "?model=keep"):
        for separator in (" then ", "\nnext\t"):
            for query, clean_query in (("token=" + secret, "token=<redacted>"),
                                       ("audio=AQIDBA==", "audio=<omitted: 8 base64 chars>"),
                                       ("mime_type=audio%2Fpcm&content=AQIDBA==",
                                        "mime_type=audio%2Fpcm&content=<omitted: 8 base64 chars>"),
                                       ("parts.0.content.chunk=AQIDBA==&parts.0.mime_type=audio%2Fpcm",
                                        "parts.0.content.chunk=<omitted: 8 base64 chars>&parts.0.mime_type=audio%2Fpcm"),
                                       ("type=input_audio&data=AQIDBA==", "type=input_audio&data=<omitted: 8 base64 chars>"),
                                       ("blob=data%3Aaudio%2Fbasic%2CA%27PAYLOAD", "blob=<omitted: data URI>")):
                value = first + separator + "https://example.invalid/?" + query + "&model=keep#anchor"
                expected = first + separator + "https://example.invalid/?" + clean_query + "&model=keep#anchor"
                prefix = "url-prefixed-prose-" + first + separator + query
                for site, path in (("response_body", ""), ("attribute_keys", "exception.message"),
                                   ("flattened_paths", "output.message"), ("indexed_paths", "output.messages[0]"),
                                   ("free_text_urls", "")):
                    add(prefix + "-" + site, site, value, expected, path, ["url-prefixed-prose"])
                for site in ("request_body", "response_body"):
                    add(prefix + "-object-" + site, site, {"message": value}, {"message": expected},
                        covers=["url-prefixed-prose"])
                add(prefix + "-array", "response_body", [value], [expected], covers=["url-prefixed-prose"])
    for query, clean_query in (("token=" + secret, "token=<redacted>"),
                               ("audio=AQIDBA==", "audio=<omitted: 8 base64 chars>")):
        for suffix in (" trailing prose", " then https://example.invalid/?model=keep#anchor"):
            value = "https://example.invalid/?" + query + suffix
            expected = "https://example.invalid/?" + clean_query + suffix
            for site, path in (("response_body", ""), ("attribute_keys", "exception.message")):
                add("url-query-trailing-prose-" + query + suffix + site, site, value, expected, path,
                    ["url-prefixed-prose"])
    add("known-secret-echo", "response_body", {"message": "provider echoed " + secret}, {"message": "provider echoed <redacted>"}, secrets=[secret])
    add("known-secret-overlap", "attribute_keys", secret + "-long", redacted, "exception.message", secrets=[secret, secret + "-long"])

    # Independent synthetic signature prefixes, not outputs from Reference.magic.
    magic_samples = {
        "png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
        "jpeg": b"\xff\xd8\xff\xe0\x00\x10JFIF\x00",
        "gif87a": b"GIF87a\x01\x00\x01\x00",
        "gif89a": b"GIF89a\x01\x00\x01\x00",
        "riff-wave": b"RIFF\x24\x00\x00\x00WAVEfmt ",
        "riff-webp": b"RIFF\x10\x00\x00\x00WEBPVP8 ",
        "id3": b"ID3\x04\x00\x00\x00\x00\x00\x00",
        "mp3-frame-sync": b"\xff\xfb\x90\x64\x00\x00",
        "ogg": b"OggS\x00\x02\x00\x00",
        "flac": b"fLaC\x00\x00\x00\x22",
        "ftyp": b"\x00\x00\x00\x18ftypisom",
        "pdf": b"%PDF-1.7\n",
    }
    for kind, raw in magic_samples.items():
        encoded = base64.b64encode(raw).decode("ascii")
        for encoding, value in (("standard", encoded), ("urlsafe", base64.urlsafe_b64encode(raw).decode("ascii")),
                                ("unpadded", encoded.rstrip("=")), ("wrapped", encoded[:4] + "\r\n" + encoded[4:])):
            for site, path in (("magic_bytes", "arbitrary"), ("request_headers", "X-Payload"),
                               ("request_body", "nested.payload"), ("response_body", "content"),
                               ("attribute_keys", "attachment"), ("flattened_paths", "output.attachment"),
                               ("indexed_paths", "output.attachment.0")):
                add(f"magic-{kind}-{encoding}-{site}", site, value, omitted(value), path, ["magic:" + kind])
            add(f"magic-{kind}-{encoding}-query", "request_query", "https://example.invalid/?blob=" + quote(value, safe=""),
                "https://example.invalid/?blob=" + omitted(value), covers=["magic:" + kind])
    for name, raw in (("riff-other", b"RIFF\x00\x00\x00\x00AVI "), ("ftyp-wrong-offset", b"ftyp\x00\x00\x00\x00"),
                      ("plain", b"ordinary captured text"), ("near-mp3", b"\xfe\xfb\x90\x64")):
        value = base64.b64encode(raw).decode("ascii")
        add("magic-negative-" + name, "magic_bytes", value, value, "content")

    for size in (1, 4, 255, 256, 257, 1024):
        value = "A" * size
        for field in catalog["media"]["unrecognized_base64_fields"]:
            # audio_base64 is independently audio at every size.
            expected = omitted(value) if field in catalog["media"]["audio"]["fields"] or size > 256 else value
            add(f"threshold-{field}-{size}", "indexed_paths", value, expected, "output." + field + ".0", ["threshold", "media-field:" + field])
        add(f"arbitrary-no-magic-{size}", "attribute_keys", value, value, "arbitrary", ["threshold"])
    add("non-base64-long-data", "request_body", {"data": "plain text " * 80}, {"data": "plain text " * 80})
    add("ordinary-image-caption", "response_body", {"data": {"answer": 42}, "image": "family-photo-caption"},
        {"data": {"answer": 42}, "image": "family-photo-caption"})

    data_uris = (
        ("data:image/png;base64,QQ==", omitted("QQ==")),
        ("DATA:audio/wav;BASE64,", omitted("")),
        ("data:audio/wav;base64,Q%2BQ%3D", omitted("Q+Q=")),
        ("data:audio/wav;base64,QUJD\r\nRA==", omitted("QUJD\r\nRA==")),
        ("data:application/pdf,%25PDF-1.7", "<omitted: data URI>"),
        ("data:image/svg+xml,%3Csvg%3E", "<omitted: data URI>"),
        ("data:,literal-payload", "<omitted: data URI>"),
        ("data:audio/wav;base64,%FF", "<omitted: data URI>"),
        ("data:audio/wav;base64,AQIDBA==", omitted("AQIDBA==")),
    )
    for i, (value, expected) in enumerate(data_uris):
        add("data-uri-" + str(i), "response_body", "before " + value + " after", "before " + expected + " after", covers=["data-uri"])
        for site, path in (("response_body", ""), ("attribute_keys", "blob"), ("indexed_paths", "audio.data.0")):
            add(f"data-uri-exact-{i}-{site}", site, value, expected, path, ["data-uri-value-boundaries"])
        for root, base in (("absolute", "https://example.invalid/"), ("relative", "/path")):
            url = base + "?blob=" + quote(value, safe="") + "&model=keep#anchor"
            cleaned = base + "?blob=" + expected + "&model=keep#anchor"
            url_sites(f"data-uri-query-{i}-{root}", url, cleaned, ["data-uri-query"])
        if "\r" not in value and "\n" not in value:
            # A literal data: query must be framed before scanning its payload;
            # following parameters/fragments are not part of the data URI.
            url_sites(f"data-uri-literal-query-{i}", "https://example.invalid/?blob=" + value + "&model=keep#anchor",
                      "https://example.invalid/?blob=" + expected + "&model=keep#anchor", ["data-uri-query"])
    literal_data_uris = (
        "data:audio/basic,A'SYNTHETIC_MEDIA_20261030",
        'data:audio/basic,A"SYNTHETIC_MEDIA_20261030',
        "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg'><text>literal</text></svg>",
        'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg"><text>literal</text></svg>',
        "data:image/svg+xml,<svg>literal</svg>",
        "data:,literal'payload",
        "data:,literal payload\nmore payload",
        "data:,literal&payload#still-payload",
        "DATA:audio/basic,'quoted' payload",
        "data:,https://example.invalid/?model=still-payload",
    )
    for i, value in enumerate(literal_data_uris):
        expected = "<omitted: data URI>"
        for site, path in (("request_body", ""), ("response_body", ""), ("attribute_keys", "blob"),
                           ("flattened_paths", "metadata.blob"), ("indexed_paths", "metadata.blob.0"),
                           ("indexed_paths", "input.audio.0")):
            add(f"data-uri-whole-value-{i}-{site}-{path}", site, value, expected, path, ["data-uri-value-boundaries"])
        for site in ("request_headers", "request_body", "response_body"):
            add(f"data-uri-whole-object-{i}-{site}", site, {"blob": value, "model": "keep"},
                {"blob": expected, "model": "keep"}, covers=["data-uri-value-boundaries"])
        add(f"data-uri-whole-array-{i}", "response_body", {"blobs": [value]}, {"blobs": [expected]},
            covers=["data-uri-value-boundaries"])
        url_sites(f"data-uri-query-framed-{i}", "https://example.invalid/?blob=" + quote(value, safe="") + "&model=keep#anchor",
                  "https://example.invalid/?blob=<omitted: data URI>&model=keep#anchor",
                  ["data-uri-query", "data-uri-value-boundaries"])
        if "&" not in value and "#" not in value:
            for root, base in (("absolute", "https://example.invalid/"), ("relative", "/path"), ("query-only", "")):
                bounded_url_sites(f"data-uri-exact-literal-url-{i}-{root}", base + "?blob=" + value + "&model=keep#anchor",
                                  base + "?blob=<omitted: data URI>&model=keep#anchor",
                                  ["url-value-boundaries", "data-uri-query", "data-uri-value-boundaries"])
    bounded_url_sites("data-uri-exact-url-backslash", 'https://example.invalid/?blob=data:,literal\\payload&model=keep#anchor',
                      "https://example.invalid/?blob=<omitted: data URI>&model=keep#anchor", ["url-value-boundaries"])
    for value, expected in (("data:audio/basic,literal after", "<omitted: data URI> after"),
                            ("data:audio/wav;base64,QQ== after", "<omitted: 4 base64 chars> after")):
        add("data-uri-free-text-" + value, "free_text_urls", value, expected, covers=["data-uri-free-text"])
        add("data-uri-exact-versus-free-text-" + value, "response_body", value, "<omitted: data URI>",
            covers=["data-uri-value-boundaries"])
    for i, value in enumerate(("before data:audio/wav;base64,AQIDBA== after", "metadata:source,year",
                               "candidate_data:source,year", "data:audio/wav;base64")):
        expected = "before <omitted: 8 base64 chars> after" if i == 0 else value
        url_sites(f"data-uri-query-context-{i}", "https://example.invalid/?blob=" + quote(value, safe="") + "&model=keep",
                  "https://example.invalid/?blob=" + (expected if i == 0 else quote(value, safe="")) + "&model=keep",
                  ["data-uri-query"])
    for i, value in enumerate(("metadata:source,year", "candidate_data:source,year", "x-data:source,year", "other:data:source,year")):
        add("data-uri-negative-boundary-" + str(i), "response_body", value, value, covers=["data-uri-boundary"])
    add("data-uri-punctuation-boundary", "response_body", "image=data:image/png;base64,QQ== after", "image=<omitted: 4 base64 chars> after",
        covers=["data-uri-boundary"])
    add("data-prefix-linear", "attribute_keys", "data:a" * 5461, "data:a" * 5461, "content", ["data-uri"])
    add("query-prefix-linear", "free_text_urls", "?" * 8192, "?" * 8192)

    for field in catalog["media"]["audio"]["fields"]:
        for size in (1, 4, 256, 257):
            value = "A" * size
            add(f"audio-{field}-{size}", "indexed_paths", value, omitted(value), "input." + field + "[0]", ["audio-field:" + field])
    for spelling in ("audioContent", "audio_content"):
        for site in ("request_body", "response_body"):
            add("audio-content-" + spelling + "-" + site, site, {spelling: "AQIDBA=="}, {spelling: omitted("AQIDBA==")},
                covers=["audio-field:audiocontent"])
        add("audio-content-indexed-" + spelling, "indexed_paths", "AQIDBA==", omitted("AQIDBA=="), "input." + spelling + ".0",
            ["audio-field:audiocontent"])
    for field in catalog["media"]["audio"]["payload_fields"]:
        add("audio-payload-" + field, "request_body", {"input_audio": {"source": {field: "QQ=="}, "format": "pcm"}},
            {"input_audio": {"source": {field: omitted("QQ==")}, "format": "pcm"}}, covers=["audio-payload:" + field])
    for field in catalog["media"]["audio"]["metadata_fields"]:
        add("audio-metadata-" + field, "response_body", {"audio": {field: "metadata-text"}},
            {"audio": {field: "metadata-text"}}, covers=["audio-metadata:" + field])
    for field in catalog["media"]["audio"]["metadata_fields"]:
        value = {"audio": {"data": {field: {"content": "spoken words"}, "chunk": "AQIDBA=="}}}
        expected = {"audio": {"data": {field: {"content": "spoken words"}, "chunk": omitted("AQIDBA==")}}}
        add("audio-metadata-boundary-" + field, "response_body", value, expected, covers=["metadata-boundaries"])
        add("audio-metadata-boundary-flat-" + field, "flattened_paths", "spoken words", "spoken words",
            "audio.transcript." + field + ".content", ["metadata-boundaries"])
    add("audio-transcript-nested-content", "response_body", {"audio": {"transcript": {"content": "spoken words"}}},
        {"audio": {"transcript": {"content": "spoken words"}}}, covers=["metadata-boundaries"])
    for field in catalog["media"]["audio"]["payload_fields"]:
        value = {"input_audio": {field: {"chunk": ["AQIDBA==", "raw audio bytes"]}, "transcript": "spoken words"}}
        expected = {"input_audio": {field: {"chunk": [omitted("AQIDBA=="), "<omitted: media>"]}, "transcript": "spoken words"}}
        add("audio-descendant-" + field, "request_body", value, expected, covers=["payload-descendants"])
        add("audio-descendant-flat-" + field, "flattened_paths", "AQIDBA==", omitted("AQIDBA=="),
            "input_audio." + field + ".chunk", ["payload-descendants"])
    for audio_field in catalog["media"]["audio"]["fields"] + ["audioContent", "audio_content"]:
        for kind, sample, replacement in (("base64", "AQIDBA==", omitted("AQIDBA==")),
                                           ("numeric", 7, "<omitted: media>"),
                                           ("literal", "raw audio bytes", "<omitted: media>")):
            value = {"audio": {"data": {audio_field: {"source": [sample]}}}}
            clean_source = replacement if kind == "numeric" else [replacement]
            expected = {"audio": {"data": {audio_field: {"source": clean_source}}}}
            for site in ("request_body", "response_body", "attribute_keys"):
                add(f"nested-audio-payload-{audio_field}-{kind}-{site}", site, value, expected,
                    covers=["payload-nested-audio"])
            for site, path in (("flattened_paths", "audio.data." + audio_field + ".source.0"),
                               ("indexed_paths", "input.audio[0].data." + audio_field + "[0].source[0]")):
                add(f"nested-audio-payload-{audio_field}-{kind}-{site}", site, sample, replacement, path,
                    ["payload-nested-audio"])
            query_object_sites(f"nested-audio-payload-query-{audio_field}-{kind}",
                               [("audio.data." + audio_field + ".source.0", str(sample),
                                 omitted(str(sample)) if kind == "numeric" else replacement)])
    for field in catalog["media"]["audio"]["payload_fields"]:
        path = "audio." + field + ".audio.source.audio.source.0"
        add("nested-audio-explicit-payload-" + field, "indexed_paths", "AQIDBA==", omitted("AQIDBA=="), path,
            ["payload-nested-audio"])
    for boundary in catalog["media"]["audio"]["metadata_fields"]:
        path = "audio.data.audio.source." + boundary + ".content.0"
        add("nested-audio-metadata-boundary-" + boundary, "indexed_paths", "spoken words", "spoken words", path,
            ["payload-nested-audio", "metadata-boundaries"])
    for kind, sample in (("base64", "AQIDBA=="), ("numeric", 7)):
        add("audio-wrapper-without-payload-" + kind, "indexed_paths", sample, sample, "audio.source.0",
            ["payload-nested-audio", "audio-object-payload-scope"])
    for field in ("audio_base64", "audioBase64"):
        for mime in ("audio/pcm", "image/png"):
            for kind, sample, replacement in (("base64", "AQIDBA==", omitted("AQIDBA==")),
                                               ("numeric", 7, "<omitted: media>")):
                value = {"mime_type": mime, field: {"source": [sample]}}
                clean_source = replacement if kind == "numeric" else [replacement]
                expected = {"mime_type": mime, field: {"source": clean_source}}
                add(f"mime-audio-overlap-{field}-{mime}-{kind}", "mime_siblings", value, expected,
                    covers=["mime-audio-payload-overlap"])
                add(f"mime-audio-overlap-flat-{field}-{mime}-{kind}", "mime_siblings", sample, replacement,
                    field + ".source.0", ["mime-audio-payload-overlap"], {"mime_type": mime})
                query_object_sites(f"mime-audio-overlap-query-{field}-{mime}-{kind}",
                                   [(field + ".source.0", str(sample),
                                     omitted(str(sample)) if kind == "numeric" else replacement),
                                    ("mime_type", mime, mime)])
    for hint, hint_value, payload_field in (("mime_type", "audio/pcm", "content"), ("type", "input_audio", "data")):
        query_object_sites("hint-nested-audio-payload-" + hint,
                           [(payload_field + ".audio.source.0", "AQIDBA==", omitted("AQIDBA==")),
                            (hint, hint_value, hint_value)])
    audio_parameters = {"voice": "alloy", "format": "wav", "max_tokens": 5, "sample_rate": 16000,
                        "speed": 1.25, "id": "audio_20261030", "expires_at": 1793383200,
                        "input_tokens": 11, "usage": {"output_tokens": 7}, "custom_parameter": "keep"}
    for audio_field in catalog["media"]["audio"]["fields"]:
        value = {audio_field: dict(audio_parameters, data="AQIDBA==")}
        expected = {audio_field: dict(audio_parameters, data=omitted("AQIDBA=="))}
        for site in ("request_body", "response_body", "attribute_keys"):
            add("audio-object-parameters-" + audio_field + "-" + site, site, value, expected,
                covers=["audio-object-payload-scope"])
        for name, item in audio_parameters.items():
            for site, path in (("flattened_paths", "input." + audio_field + "." + name),
                               ("indexed_paths", "input." + audio_field + "[0]." + name)):
                add(f"audio-parameter-{audio_field}-{name}-{site}", site, item, item, path, ["audio-object-payload-scope"])
        value = {audio_field: [dict(audio_parameters, samples=[1, 2, 3])]}
        expected = {audio_field: [dict(audio_parameters, samples="<omitted: media>")]}
        add("audio-object-array-parameters-" + audio_field, "response_body", value, expected,
            covers=["audio-object-payload-scope", "audio-arrays"])
    for field in catalog["media"]["audio"]["payload_fields"]:
        add("audio-payload-indexed-samples-" + field, "indexed_paths", 7, "<omitted: media>",
            "input.audio[0]." + field + "[0]", ["audio-object-payload-scope", "audio-arrays"])
    add("audio-parameters-independent-secrets", "response_body",
        {"audio": {"voice": "alloy", "api_key": secret, "custom_parameter": secret}},
        {"audio": {"voice": "alloy", "api_key": redacted, "custom_parameter": redacted}},
        secrets=[secret], covers=["audio-object-payload-scope"])
    add("audio-parameters-independent-media", "response_body",
        {"audio": {"voice": "data:audio/basic,A'PAYLOAD", "custom_parameter": base64.b64encode(magic_samples["png"]).decode("ascii")}},
        {"audio": {"voice": "<omitted: data URI>", "custom_parameter": omitted(base64.b64encode(magic_samples["png"]).decode("ascii"))}},
        covers=["audio-object-payload-scope"])
    add("audio-numeric-array", "attribute_keys", [0, 1, 2, 3], "<omitted: media>", "input.audio.0.bytes", ["audio-arrays"])
    add("audio-string-array", "response_body", {"audio": ["QQ==", "Qg=="]},
        {"audio": [omitted("QQ=="), omitted("Qg==")]}, covers=["audio-arrays"])
    add("audio-object-array", "request_body", {"audio": [{"data": "QQ==", "transcript": "hello"}]},
        {"audio": [{"data": omitted("QQ=="), "transcript": "hello"}]}, covers=["audio-arrays"])
    add("audio-literal-bytes", "attribute_keys", "\u0000\u0001\u0002", "<omitted: media>", "input.audio", ["audio-arrays"])
    add("audio-fake-omission-marker", "attribute_keys", "<omitted: not-a-marker>", "<omitted: media>", "input.audio")
    add("audio-query-short", "request_query", "https://example.invalid/?audio=QQ==&model=keep",
        "https://example.invalid/?audio=<omitted: 4 base64 chars>&model=keep")
    add("audio-empty-field", "attribute_keys", "", "<omitted: media>", "input.audio")
    add("audio-numeric-sample", "indexed_paths", 7, "<omitted: media>", "input.audio.0")
    add("mime-numeric-payload", "mime_siblings", {"mime_type": "audio/pcm", "content": 7},
        {"mime_type": "audio/pcm", "content": "<omitted: media>"})
    long_png = base64.b64encode(magic_samples["png"] + b"\x00" * 4096).decode("ascii")
    add("magic-large-arbitrary-field", "magic_bytes", long_png, omitted(long_png), "arbitrary")
    for audio_type in catalog["media"]["audio"]["type_names"]:
        add("audio-type-hint-" + audio_type, "request_body", {"type": audio_type, "source": {"bytes": "QQ=="}, "text": "hello"},
            {"type": audio_type, "source": {"bytes": omitted("QQ==")}, "text": "hello"}, covers=["audio-type:" + audio_type])
        for field in catalog["media"]["audio"]["payload_fields"]:
            query_object_sites(f"audio-type-query-{audio_type}-{field}",
                               [("source." + field, "AQIDBA==", omitted("AQIDBA==")), ("type", audio_type, audio_type),
                                ("voice", "alloy", "alloy"), ("format", "pcm", "pcm"), ("max_tokens", "5", "5"),
                                ("sample_rate", "16000", "16000"), ("speed", "1.25", "1.25")])
    query_object_sites("audio-query-object-parameters",
                       [("audio.voice", "alloy", "alloy"), ("audio.format", "wav", "wav"),
                        ("audio.max_tokens", "5", "5"), ("audio.sample_rate", "16000", "16000"),
                        ("audio.speed", "1.25", "1.25"), ("audio.usage.output_tokens", "7", "7"),
                        ("audio.chunk", "AQIDBA==", omitted("AQIDBA==")), ("audio.0", "AQIDBA==", omitted("AQIDBA=="))])

    mime_examples = {"audio/": "audio/wav", "image/": "image/png", "video/": "video/mp4"}
    mime_values = [mime_examples.get(prefix, prefix + "x-synthetic") for prefix in catalog["media"]["mime_siblings"]["prefixes"]]
    mime_values += catalog["media"]["mime_siblings"]["exact"] + ["AUDIO/PCM; rate=16000"]
    for mime_name in catalog["media"]["mime_siblings"]["names"]:
        # Normalization explicitly covers the six SDK spellings.
        spellings = {"mimetype": ["mimeType", "mime_type"], "mediatype": ["mediaType", "media_type"],
                     "contenttype": ["contentType", "content_type", "content-type"]}[mime_name]
        for spelling in spellings:
            for mime in mime_values:
                query_object_sites(f"mime-query-{spelling}-{mime}",
                                   [("content", "AQIDBA==", omitted("AQIDBA==")), (spelling, mime, mime),
                                    ("transcript.content", "spoken words", "spoken words")])
                for payload in catalog["media"]["mime_siblings"]["payload_fields"]:
                    value = {spelling: mime, payload: "QQ==", "transcript": "hello"}
                    expected = {spelling: mime, payload: omitted("QQ=="), "transcript": "hello"}
                    add(f"mime-{spelling}-{mime}-{payload}", "mime_siblings", value, expected,
                        covers=["mime-name:" + mime_name, "mime-payload:" + payload, "mime-type:" + mime.split(";", 1)[0].lower()])
            path = "gen_ai.input.messages.0.parts.0.inlineData.data.0"
            add("flattened-mime-" + spelling, "mime_siblings", "QQ==", omitted("QQ=="), path,
                ["mime-name:" + mime_name], {"gen_ai.input.messages.0.parts.0.inlineData." + spelling: "image/png"})
    add("mime-wrappers", "mime_siblings", {"mimeType": "audio/wav", "source": {"inlineData": {"content": "QQ=="}}},
        {"mimeType": "audio/wav", "source": {"inlineData": {"content": omitted("QQ==")}}})
    add("mime-index-isolation", "mime_siblings", {"parts.0.mime_type": "audio/wav", "parts.0.content": "QQ==", "parts.1.content": "QQ=="},
        {"parts.0.mime_type": "audio/wav", "parts.0.content": omitted("QQ=="), "parts.1.content": "QQ=="})
    add("mime-nested-index-isolation", "mime_siblings", {"parts": [{"mime_type": "audio/wav", "content": "QQ=="}, {"content": "QQ=="}]},
        {"parts": [{"mime_type": "audio/wav", "content": omitted("QQ==")}, {"content": "QQ=="}]})
    for field in catalog["media"]["mime_siblings"]["payload_fields"]:
        value = {"mime_type": "audio/pcm", field: {"chunk": "AQIDBA=="}, "transcript": {"content": "spoken words"}}
        expected = {"mime_type": "audio/pcm", field: {"chunk": omitted("AQIDBA==")}, "transcript": {"content": "spoken words"}}
        add("mime-descendant-" + field, "mime_siblings", value, expected, covers=["payload-descendants", "metadata-boundaries"])
        for hint_name, hint_value in (("parts.0.mime_type", "audio/pcm"), ("parts.0.mime_type", ["audio/pcm"]),
                                      ("parts[0].mime_type[0]", "audio/pcm")):
            variant = "array" if isinstance(hint_value, list) else "indexed" if "[" in hint_name else "scalar"
            add("mime-descendant-flat-" + field + "-" + variant, "mime_siblings", "AQIDBA==", omitted("AQIDBA=="),
                "parts.0." + field + ".chunk", ["payload-descendants", "mime-indexed-hints"], {hint_name: hint_value})
            isolated = omitted("AQIDBA==") if field in catalog["media"]["audio"]["fields"] else "AQIDBA=="
            add("mime-descendant-isolation-" + field + "-" + variant, "mime_siblings", "AQIDBA==", isolated,
                "parts.1." + field + ".chunk", ["payload-descendants", "mime-indexed-hints"], {hint_name: hint_value})
    for hint_name in ("parts.0.mime_type", "parts[0].mime_type", "parts.0.mime_type.0", "parts[0].mime_type[0]"):
        for field in catalog["media"]["mime_siblings"]["payload_fields"]:
            query_object_sites("mime-query-indexed-" + hint_name + "-" + field,
                               [("parts.0." + field + ".chunk", "AQIDBA==", omitted("AQIDBA==")),
                                ("parts.1." + field + ".chunk", "AQIDBA==",
                                 omitted("AQIDBA==") if field in catalog["media"]["audio"]["fields"] else "AQIDBA=="),
                                (hint_name, "audio/pcm", "audio/pcm"),
                                ("parts.0.transcript.content", "spoken words", "spoken words")])
    for hints in (("text/plain", "audio/pcm"), ("audio/pcm", "text/plain")):
        query_object_sites("mime-query-duplicate-hints-" + "-".join(hints),
                           [("mime_type", mime, mime) for mime in hints] +
                           [("content", "AQIDBA==", omitted("AQIDBA==")), ("content", "Qg==", omitted("Qg=="))])
    query_object_sites("mime-query-negative-hints", [("mime_type", "text/plain", "text/plain"),
                                                    ("content.chunk", "AQIDBA==", "AQIDBA=="),
                                                    ("type", "text", "text")])
    query_object_sites("mime-query-media-and-credentials", [("mime_type", "audio/pcm", "audio/pcm"),
                                                           ("content", "AQIDBA==", omitted("AQIDBA==")),
                                                           ("api_key", secret, redacted)])
    query_object_sites("mime-query-nonrecursive-value", [("blob", quote("data:audio/basic,A'PAYLOAD", safe=""),
                                                        quote("data:audio/basic,A'PAYLOAD", safe=""))])
    add("mime-metadata-boundary-flat", "mime_siblings", "spoken words", "spoken words", "parts.0.transcript.content",
        ["metadata-boundaries"], {"parts.0.mime_type": "audio/pcm"})
    add("fresh-hint-beneath-metadata", "mime_siblings",
        {"audio": {"transcript": {"mime_type": "audio/pcm", "content": {"chunk": "AQIDBA=="}}}},
        {"audio": {"transcript": {"mime_type": "audio/pcm", "content": {"chunk": omitted("AQIDBA==")}}}},
        covers=["metadata-boundaries"])
    add("mime-unrecognized", "mime_siblings", {"mime_type": "text/plain", "content": "QQ=="}, {"mime_type": "text/plain", "content": "QQ=="})
    add("structured-string", "attribute_keys", '{ "api_key": "synthetic", "max_tokens": 5 }',
        '{"api_key":"<redacted>","max_tokens":5}', "input.value")
    return cases


def check_cases(reference, cases):
    names, coverage = set(), set()
    for case in cases:
        assert case["name"] not in names, "duplicate fixture: " + case["name"]
        names.add(case["name"])
        inputs = case["input"]
        assert inputs["site"] in reference.catalog["fixture_sites"], case["name"]
        coverage.add("site:" + inputs["site"])
        coverage.update(case["covers"])
        framed = inputs["site"] != "free_text_urls"
        actual = reference.scrub(inputs["value"], inputs["path"], inputs.get("siblings"), inputs.get("known_secrets", ()), framed=framed)
        assert actual == case["expected"], f"{case['name']}: {actual!r} != {case['expected']!r}"
        again = reference.scrub(actual, inputs["path"], inputs.get("siblings"), inputs.get("known_secrets", ()), framed=framed)
        assert again == actual, "non-idempotent fixture: " + case["name"]
    required = {"site:" + site for site in reference.catalog["fixture_sites"]}
    for group in ("patterns", "exclusions"):
        required.update(("pattern:" if group == "patterns" else "exclusion:") + item["id"] for item in reference.credentials[group])
    required.update("contextual-exclusion:" + item["id"] for item in reference.credentials["contextual_exclusions"])
    required.update("exclusion-guard:" + item["id"] + ":" + marker for item in reference.credentials["exclusions"]
                    for marker in item.get("unless_contains_patterns", ()))
    required.update({"metric-credential-precedence", "url-query-boundaries", "url-apostrophes", "data-uri-query",
                     "data-uri-boundary", "payload-descendants", "metadata-boundaries", "mime-indexed-hints",
                     "data-uri-value-boundaries", "data-uri-free-text", "query-media-object",
                     "token-details-objects", "audio-object-payload-scope", "url-value-boundaries",
                     "payload-nested-audio", "mime-audio-payload-overlap", "url-prefixed-prose"})
    required.update("seed:" + name for name in reference.credentials["seed_names"])
    required.update("seed:" + name for item in reference.credentials["missed_name_sources"] for name in item["names"])
    required.update("signed:" + name for name in reference.catalog["signed_url"]["parameters"])
    required.update("magic:" + item["id"] for item in reference.media["magic"])
    required.update("media-field:" + name for name in reference.media["unrecognized_base64_fields"])
    required.update("audio-field:" + name for name in reference.audio["fields"])
    required.update("audio-payload:" + name for name in reference.audio["payload_fields"])
    required.update("audio-metadata:" + name for name in reference.audio["metadata_fields"])
    required.update("audio-type:" + name for name in reference.audio["type_names"])
    required.update("mime-name:" + name for name in reference.mime["names"])
    required.update("mime-payload:" + name for name in reference.mime["payload_fields"])
    mime_examples = {"audio/": "audio/wav", "image/": "image/png", "video/": "video/mp4"}
    required.update("mime-type:" + mime_examples.get(prefix, prefix + "x-synthetic") for prefix in reference.mime["prefixes"])
    required.update("mime-type:" + name for name in reference.mime["exact"])
    required.update({"threshold", "audio-arrays", "data-uri"})
    assert required <= coverage, "unpinned catalog rules: " + repr(sorted(required - coverage))
    print(f"privacy golden cases: {len(cases)} passed; idempotency: {len(cases)} passed; coverage: {len(required)} rules/sites pinned")


def changed_leaves(before, after, path=""):
    if isinstance(before, dict) and isinstance(after, dict) and before.keys() == after.keys():
        return [change for key in before for change in changed_leaves(before[key], after[key], path + "." + key if path else key)]
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        return [change for i, value in enumerate(before) for change in changed_leaves(value, after[i], path + "." + str(i))]
    return [(path, after)] if before != after else []


def check_wire(reference):
    # Independent, exact allowlist from the existing golden cases' not_contains
    # and redacted status expectations. No new predicate can silently bless its
    # own false positive; all other request/response leaves must be byte/value
    # identical. These six leaves are the only intentional wire-fixture edits.
    allowed = {
        ("openai-chat-input-audio-scrubbed", "request.json.messages.0.content.1.input_audio.data"),
        ("anthropic-mcp-token-redacted", "request.json.mcp_servers.0.authorization_token"),
        ("assemblyai-audio-url-private", "request.json.audio_url"),
        ("gemini-inline-image-scrubbed", "request.json.contents.0.parts.1.inlineData.data"),
        ("error-echo-redacted", "request.headers.authorization"),
        ("error-echo-redacted", "response.json.error.message"),
    }
    changed, leaves, metrics, expectations = set(), 0, 0, 0

    def count(value):
        if isinstance(value, dict):
            return sum(count(item) for item in value.values())
        if isinstance(value, list):
            return sum(count(item) for item in value)
        return 1

    files = sorted((SPEC / "fixtures").glob("*.json"))
    assert len(files) == 47, f"wire fixture set changed ({len(files)}); review the proof explicitly"
    for file in files:
        fixture = json.loads(file.read_text())
        secrets = reference.harvest(fixture["request"])
        for side in ("request", "response"):
            source = fixture[side]
            # Every body representation, every header, and request URL are
            # visited; multipart file descriptors/binary byte counts are safe
            # metadata, not actual bytes, and are checked too.
            for field in ("headers", "url", "json", "text", "sse", "ndjson", "multipart", "binary"):
                if field not in source:
                    continue
                original = source[field]
                leaves += count(original)
                cleaned = reference.scrub(original, secrets=secrets)
                for path, _ in changed_leaves(original, cleaned, side + "." + field):
                    identity = fixture["name"], path
                    assert identity in allowed, f"wire false positive: {identity}"
                    changed.add(identity)
        expect = fixture["expect"]
        for name, value in expect.get("attributes", {}).items():
            assert reference.scrub(value, name, secrets=secrets) == value, f"expected wire attribute scrubbed: {file.name} {name}"
            expectations += 1
            if "token" in normalize(name):
                assert not reference.credential_name(name), "usage attribute matched: " + name
                metrics += 1
        for name in ("name", "status_description"):
            if name in expect:
                assert reference.scrub(expect[name], secrets=secrets) == expect[name], f"expected wire {name} scrubbed: {file.name}"
                expectations += 1
    assert changed == allowed, "expected wire privacy controls no longer exercised: " + repr(allowed - changed)
    print(f"wire no-false-positive proof: {len(files)} passed; {leaves} request/response leaves; {expectations} expected values retained; {metrics} token metrics retained; {len(changed)} intentional redactions")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate reference, coverage, wire preservation and generated bytes without writing")
    args = parser.parse_args()
    catalog = json.loads((SPEC / "privacy.json").read_text())
    reference = Reference(catalog)
    cases = golden_cases(catalog)
    check_cases(reference, cases)
    check_wire(reference)
    expected = (json.dumps({"version": 1, "catalog_version": catalog["version"], "cases": cases}, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_bytes() != expected:
            parser.exit(1, "stale privacy-fixtures.json; run generate_privacy_fixtures.py\n")
        print("privacy-fixtures.json: byte-identical")
    else:
        OUTPUT.write_bytes(expected)
        print(f"wrote {OUTPUT.name} ({len(cases)} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
