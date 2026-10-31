"""Dependency-light S6 recognition, shared by observation and export.

The packaged catalog is byte-identical to spec/auto-instrumentation/privacy.json.
URL framing follows the spec's executable reference; no SDK/transport imports or
application methods are needed here. All recursive work shares a bounded budget.
"""

import base64
import binascii
import json
import math
import re
from pathlib import Path
from urllib.parse import unquote, unquote_plus

CATALOG_PATH = Path(__file__).parent / "auto" / "wire" / "privacy.json"
CATALOG = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_FOLD = str.maketrans("", "", "".join(CATALOG["normalization"]["remove"]))
_BASE64 = re.compile("[" + re.escape(CATALOG["media"]["base64_alphabet"]) + r"]+\Z")
_DATA = re.compile(r'''(?<![A-Za-z0-9+.\-_:])data:[^\s"'<>\\]*(?:[\r\n]+[A-Za-z0-9+/_=\-]+)*''', re.I)
_OMISSION = re.compile(r"<omitted: (?:[0-9]+ base64 chars|media|data URI)>\Z")
_URL = re.compile(r'''(?:https?://|(?<!\S)[/?])(?:<redacted>|<omitted: (?:[0-9]+ base64 chars|media|data URI)>|[^\s"<>\\])+''', re.I)
_TEXT = re.compile(rf"(?P<url>{_URL.pattern})|(?P<data>{_DATA.pattern})", re.I)
_USERINFO = re.compile(r"^(https?://)[^\s/@]+:[^\s/@]+@", re.I)


def normalize(name):
    return name.translate(_ASCII_LOWER).translate(_FOLD)


def components(path):
    return [part for part in re.split(r"[.\[\]]+", path) if part and not part.isdecimal()]


def leaf(path):
    parts = components(path)
    return normalize(parts[-1]) if parts else ""


class PrivacyCatalog:
    def __init__(self, catalog):
        self.catalog = catalog
        self.credentials = catalog["credentials"]
        patterns = {item["id"]: item["contains"] for item in self.credentials["patterns"]}
        self.patterns = tuple(patterns.values())
        self.exclusions = tuple((re.compile(item["regex"]), tuple(patterns[name] for name in item.get("unless_contains_patterns", ())))
                                for item in self.credentials["exclusions"])
        self.contextual_exclusions = tuple(re.compile(item["path_regex"]) for item in self.credentials["contextual_exclusions"])
        self.signed = frozenset(normalize(name) for name in catalog["signed_url"]["parameters"])
        self.media = catalog["media"]
        self.mime = self.media["mime_siblings"]
        self.audio = self.media["audio"]
        self.magic_rules = tuple((rule["id"], tuple((test["offset"], bytes.fromhex(test["hex"]),
                                bytes.fromhex(test.get("mask", "ff" * (len(test["hex"]) // 2))))
                                for test in rule["tests"])) for rule in self.media["magic"])

    def excluded(self, name):
        folded = normalize(name)
        return any(regex.fullmatch(folded) and not any(marker in folded for marker in guards)
                   for regex, guards in self.exclusions)

    def credential_name(self, name, query=False):
        """The only credential-name predicate, including signed-query additions."""
        if query and normalize(name) in self.signed:
            return True
        parts = [normalize(part) for part in components(name)]
        excluded = False
        for index, folded in enumerate(parts):
            context = ".".join(parts[:index + 1])
            if self.excluded(folded) or any(regex.fullmatch(context) for regex in self.contextual_exclusions):
                excluded = True
            elif any(pattern in folded for pattern in self.patterns):
                return True
        if excluded:
            return False
        return not self.excluded(name) and any(pattern in normalize(name) for pattern in self.patterns)

    def magic(self, value):
        if not _BASE64.fullmatch(value):
            return None
        compact = value.replace("\r", "").replace("\n", "")
        count = self.media["base64_prefix_decoded_bytes"]
        prefix = compact[:(count * 8 + 5) // 6]
        try:
            decoded = base64.b64decode(prefix.translate(str.maketrans("-_", "+/")) + "=" * (-len(prefix) % 4), validate=True)[:count]
        except (ValueError, binascii.Error):
            return None
        for name, tests in self.magic_rules:
            if all(len(decoded[offset:offset + len(expected)]) == len(expected)
                   and all((a & m) == (e & m) for a, e, m in zip(decoded[offset:offset + len(expected)], expected, mask))
                   for offset, expected, mask in tests):
                return name
        return None

    def hint(self, name, value):
        folded = leaf(name)
        if folded not in self.mime["names"] and folded != "type":
            return False, False
        if type(value) in (list, tuple):
            if len(value) > self.catalog["limits"]["max_nodes"]:
                raise ValueError("privacy hint budget")
            hints = [self.hint(name, item) for item in value if type(item) is str]
            return any(media for media, _ in hints), any(audio for _, audio in hints)
        if type(value) is not str:
            return False, False
        if len(value) > self.catalog["limits"]["max_text_chars"]:
            raise ValueError("privacy hint text budget")
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
        indexed = dict(hints or {})
        remaining = self.catalog["limits"]["max_text_chars"]
        for index, (name, item) in enumerate(items):
            if index >= self.catalog["limits"]["max_nodes"]:
                raise ValueError("privacy hint budget")
            if type(name) is not str:
                continue
            remaining -= len(name)
            if leaf(name) in self.mime["names"] or leaf(name) == "type":
                if type(item) is str:
                    remaining -= len(item)
                elif type(item) in (list, tuple):
                    if len(item) > self.catalog["limits"]["max_nodes"]:
                        raise ValueError("privacy hint budget")
                    remaining -= sum(len(v) for v in item if type(v) is str)
            if remaining < 0:
                raise ValueError("privacy hint text budget")
            hint = self.hint(name, item)
            if any(hint):
                full_name = path + "." + name if path else name
                parent = self.hint_parent(full_name)
                previous = indexed.get(parent, (False, False))
                indexed[parent] = (previous[0] or hint[0], previous[1] or hint[1])
        return indexed

    def context(self, path, hints=None):
        hints = hints or {}
        media, audio = hints.get("", (False, False))
        payload, direct_audio, prefix = False, False, []
        for part in filter(None, re.split(r"[.\[\]]+", path)):
            prefix.append(part)
            name = normalize(part)
            if not part.isdecimal():
                if name in self.audio["metadata_fields"]:
                    media, audio, payload = False, False, False
                direct_audio = name in self.audio["fields"]
                audio = audio or direct_audio
                if ((media and name in self.mime["payload_fields"])
                        or (audio and name in self.audio["payload_fields"])):
                    payload = True
            hint_media, hint_audio = hints.get(".".join(prefix), (False, False))
            media, audio = media or hint_media, audio or hint_audio
        return media, audio, payload or direct_audio

    def omitted(self, value):
        return self.media["replacement"].format(length=len(value))

    def data_uri(self, value):
        if value[:5].lower() != "data:":
            return None
        metadata, comma, payload = value[5:].partition(",")
        if not comma:
            return None
        payload = unquote(payload)
        if metadata.lower().endswith(";base64") and (not payload or _BASE64.fullmatch(payload)):
            return self.omitted(payload)
        return self.media["data_uri"]["other_replacement"]

    def media_value(self, value, path="", hints=None, framed=True):
        """The one structural-media predicate, returning a replacement or None."""
        typ = type(value)
        if typ is str:
            # S1 hide markers can decode to an MP3 frame prefix; they are not
            # media and must remain stable across processor/export snapshots.
            if value in (self.credentials["replacement"], "__REDACTED__") or _OMISSION.fullmatch(value):
                return None
            if framed:
                replacement = self.data_uri(value)
                if replacement is not None:
                    return replacement
            encoded = bool(_BASE64.fullmatch(value))
            if self.magic(value) or self.context(path, hints)[2]:
                return self.omitted(value) if encoded else self.media["other_replacement"]
            if (encoded and leaf(path) in self.media["unrecognized_base64_fields"]
                    and len(value) > self.media["unrecognized_base64_min_chars_exclusive"]):
                return self.omitted(value)
        elif self.context(path, hints)[2]:
            if typ in (list, tuple):
                if value and all(type(item) in (int, float, bool) for item in value):
                    return self.media["other_replacement"]
            elif typ in (int, float, bool):
                return self.media["other_replacement"]
        return None

    def data_text(self, value, framed=False):
        if framed:
            replacement = self.data_uri(value)
            if replacement is not None:
                return replacement
        return _DATA.sub(lambda match: self.data_uri(match.group(0)) or match.group(0), value)

    def url_value(self, original):
        target, hashmark, fragment = original.partition("#")
        base, question, raw_query = target.partition("?")
        base = _USERINFO.sub(r"\1<redacted>@", base)
        if raw_query.count("&") >= self.catalog["limits"]["max_nodes"]:
            return "<omitted: structure limit>"
        parsed = []
        for pair in raw_query.split("&"):
            raw_name, equals, raw_value = pair.partition("=")
            parsed.append((raw_name, equals, raw_value, unquote_plus(raw_name), unquote_plus(raw_value)))
        hints = self.index_hints((name, decoded) for _, equals, _, name, decoded in parsed if equals)
        pairs = []
        for raw_name, equals, raw_value, name, decoded in parsed:
            if equals:
                if self.credential_name(name, query=True):
                    raw_value = self.credentials["replacement"]
                else:
                    replacement = self.media_value(decoded, name, hints=hints)
                    if replacement is None:
                        cleaned = self.data_text(decoded, framed=True)
                        if cleaned != decoded:
                            replacement = cleaned
                    if replacement is not None:
                        raw_value = replacement
            pairs.append(raw_name + equals + raw_value)
        return self.data_text(base) + question + "&".join(pairs) + hashmark + self.data_text(fragment)

    def is_url_value(self, value):
        if _URL.fullmatch(value):
            return True
        if not re.match(r"(?:https?://|[/?])", value, re.I):
            return False
        target, hashmark, fragment = value.partition("#")
        base, question, raw_query = target.partition("?")
        if not question:
            return False
        if raw_query.count("&") >= self.catalog["limits"]["max_nodes"]:
            return False
        pairs = []
        for pair in raw_query.split("&"):
            raw_name, equals, raw_value = pair.partition("=")
            # Only exact data payloads can hide lexical delimiters. Credential
            # or audio redaction here could swallow prose and a later URL.
            if equals and self.data_uri(unquote_plus(raw_value)) is not None:
                raw_value = self.media["data_uri"]["other_replacement"]
            pairs.append(raw_name + equals + raw_value)
        candidate = base + question + "&".join(pairs) + hashmark + fragment
        return candidate != value and _URL.fullmatch(candidate) is not None

    def text(self, value, credentials=(), framed=False):
        def token(match):
            original = match.group(0)
            if match.lastgroup == "data":
                return self.data_text(original)
            closing_quote = ""
            if match.start() and value[match.start() - 1] == "'" and original.endswith("'"):
                original, closing_quote = original[:-1], "'"
            return self.url_value(original) + closing_quote

        value = self.url_value(value) if framed and self.is_url_value(value) else _TEXT.sub(token, value)
        for secret in credentials:
            if secret and secret != self.credentials["replacement"]:
                value = "<redacted>".join(part.replace(secret, "<redacted>") for part in value.split("<redacted>"))
        return value

    def scrub(self, value, credentials=(), path="", depth=0, budget=None, hints=None, framed=True):
        budget = budget if budget is not None else [self.catalog["limits"]["max_nodes"], self.catalog["limits"]["max_text_chars"]]
        budget[0] -= 1
        if depth > self.catalog["limits"]["max_depth"] or budget[0] < 0:
            return "<omitted: structure limit>"
        if self.credential_name(path):
            return self.credentials["replacement"]
        typ = type(value)
        if typ is dict:
            if len(value) > budget[0]:
                return "<omitted: structure limit>"
            # Only plain keys/values are inspected. Subclasses and unknown values
            # become type names; never run user __str__, getters or iterators.
            names = [(name if type(name) is str else type.__getattribute__(type(name), "__name__"), item)
                     for name, item in dict.items(value)]
            budget[1] -= sum(len(name) for name, _ in names)
            if budget[1] < 0:
                return "<omitted: text limit>"
            hints = self.index_hints(names, path, hints)
            out = {}
            for name, item in names:
                child_path = path + "." + name if path else name
                out[self.text(name, credentials, framed=True)] = self.scrub(item, credentials, child_path, depth + 1, budget, hints)
                if budget[0] <= 0:
                    break
            return out
        if typ in (str, list, tuple):
            if typ is str:
                if len(value) > budget[1]:
                    return "<omitted: text limit>"
                budget[1] -= len(value)
            elif len(value) > budget[0]:
                return "<omitted: structure limit>"
        replacement = self.media_value(value, path, hints, framed)
        if replacement is not None:
            return replacement
        if typ in (list, tuple):
            out = []
            for index, item in enumerate(value):
                out.append(self.scrub(item, credentials, path + "." + str(index), depth + 1, budget, hints))
                if budget[0] <= 0:
                    break
            return out
        if typ is float:
            return value if math.isfinite(value) else ("NaN" if math.isnan(value) else "Infinity" if value > 0 else "-Infinity")
        if typ is int:
            return value if -(1 << 63) <= value < (1 << 63) else (str(value) if value.bit_length() < 256 else "<omitted: integer limit>")
        if typ in (bool, type(None)):
            return value
        if typ is not str:
            return self.scrub(type.__getattribute__(typ, "__name__"), credentials, depth=depth + 1, budget=budget)
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
                # Preserve the outer payload ancestry for encoded JSON too.
                cleaned = self.scrub(parsed, credentials, path, depth + 1, budget, hints)
                if cleaned != parsed:
                    value = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            except (ValueError, RecursionError):
                return "<omitted: invalid structured content>"
        return self.text(value, credentials, framed=framed)


PRIVACY = PrivacyCatalog(CATALOG)
credential_name = PRIVACY.credential_name
media_value = PRIVACY.media_value
