"""The shared privacy catalog, applied to the copy of a span the zero-code agent sends to Future AGI.

``wire/privacy.json`` is a byte-identical copy of the privacy catalog that every traceAI zero-code
agent ships (``spec/auto-instrumentation/privacy.json`` in the repository). It is loaded once, on
first use. One credential-name predicate and one media predicate come from it, and the copy for
Future AGI goes through them everywhere it carries content: attribute keys and values (also the
values inside JSON text in an attribute), span and event names, the status description, event and
link attributes, resource and instrumentation scope attributes, and tracestate entries.

- A value whose name is credential-like (``api_key``, ``authorization``, ``password``, ``secret``,
  ``token``, ``cookie``, ``session``, ...) becomes ``<redacted>``. Token counts, limits and
  timings keep their values.
- In URLs, in any text, credential and signed-URL query values and ``user:password@`` become
  ``<redacted>``.
- Data URIs, base64 media recognized by its first bytes, and media or audio payloads become
  ``<omitted: ...>`` markers.
- Credential-shaped values and known secrets (the Future AGI keys and the values of
  credential-named environment variables, registered when the agent starts) become ``<redacted>``
  wherever they appear.

The application's own processors and exporters never see this copy. Work is bounded by the
catalog's depth, node and text limits. A text scan reads each run of characters a bounded number of
times: two of the catalog's value shapes, the provider key and the JWT, would make Python's
backtracking engine walk a long run back from every start in it, so they are matched in an
equivalent form whose failed starts never rescan their run. Content that cannot be checked within
those limits, or whose type is not plain data, is dropped without being turned into text; a span
that cannot be checked at all keeps only its identity and timing.
"""

import base64
import binascii
import json
import logging
import os
import re
import threading
from collections import OrderedDict
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, NamedTuple, Optional, Tuple
from urllib.parse import unquote, unquote_plus

from opentelemetry import trace as trace_api
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import Link, Status

logger = logging.getLogger("fi_instrumentation.auto")

#: The catalog the predicates come from, shipped next to this module (not an importable package).
CATALOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wire", "privacy.json")
#: What the TraceConfig hide settings write (``REDACTED_VALUE``). It is a stronger policy's result
#: and is never treated as media, although its spelling decodes like the start of an audio frame.
HIDDEN = "__REDACTED__"
#: The key traceAI's context helpers and integrations record a session id under.
SESSION_ID = "session.id"
#: The process-wide known secrets: at most this many values (least recently registered go first),
#: each at least this long.
SECRET_LIMIT = 1024
SECRET_MIN_LENGTH = 8

_REDACTED = "<redacted>"
# The name of a span whose content could not be checked.
_UNCHECKED = "traceai span"

# The lexical rules of the catalog's reference recognizer (generate_privacy_fixtures.py in the spec).
_BASE64 = re.compile(r"[A-Za-z0-9+/=_\-\r\n]+\Z")
# Consumes the whole token even without a comma, so a failed data: prefix is never rescanned.
_DATA = re.compile(r'''(?<![A-Za-z0-9+.\-_:])data:[^\s"'<>\\]*(?:[\r\n]+[A-Za-z0-9+/_=\-]+)*''', re.I)
_OMISSION = re.compile(r"<omitted: (?:[0-9]+ base64 chars|media|data URI)>\Z")
# One URI token, including stable replacement markers. A relative reference starts at a text
# boundary; apostrophes are legal URI characters.
_URL = re.compile(
    r'''(?:https?://|(?<!\S)[/?])(?:<redacted>|<omitted: (?:[0-9]+ base64 chars|media|data URI)>|[^\s"<>\\])+''',
    re.I)
# URL framing goes first, so a literal data URI in a query cannot swallow later parameters.
_TEXT = re.compile(rf"(?P<url>{_URL.pattern})|(?P<data>{_DATA.pattern})", re.I)
_URL_START = re.compile(r"(?:https?://|[/?])", re.I)
_SCHEME = re.compile(r"https?://", re.I)
_PARTS = re.compile(r"[.\[\]]+")
_URL_SAFE = str.maketrans("-_", "+/")
# For JSON-like text that does not parse.
_QUOTE = re.compile(r"[\"']")
_SPACE = re.compile(r"[ \t\r\n]*")
_BARE = re.compile(r"[^,}\]\s]*")
_STRUCTURE = re.compile(r"[\"'{}\[\]]")
# Text a value length limit may have cut, read as Go's agent reads it (privacy_truncated.go):
# replacement markers stay, a data: token becomes its replacement, and a base64 run of at least
# _CUT_RUN characters is omitted. A run starts at a base64 character other than padding or a line
# break, takes line breaks with it, and ends after at most two padding characters.
_CUT = re.compile(
    r"<redacted>|<omitted: (?:[0-9]{1,40} base64 chars|media|data URI)>"
    r"|(?P<data>(?<![A-Za-z0-9+.\-_:])(?i:data:)[^ \t\r\n\v\f\"'<>\\]*(?:[\r\n]+[A-Za-z0-9+/=_\-]+)*)"
    r"|(?P<run>[A-Za-z0-9+/_\-][A-Za-z0-9+/_\-\r\n]*={0,2})")
_CUT_RUN = 8
# The token run of the value shapes below: letters, digits, underscores and hyphens.
_RUN = "[A-Za-z0-9_-]"
# Catalog value shapes that Python's backtracking engine would retry from every start in a long
# token run that cannot match: 'eyJ-' repeated makes the JWT shape quadratic in the run. Each is the
# same language here, matched from the start of its run. A lookahead group is atomic in Python's
# engine, so the first start in the run is found once, and a start that fails never rescans the run.
# Every later start in a run reaches the same run end with less of it left, so it can only match
# where the first one does: one attempt per run finds exactly the match the catalog's expression
# finds. Group 1 is the part of the run before that start; the match keeps it.
_BOUNDED_SHAPES = {
    r"\bsk-[A-Za-z0-9_-]{20,}\b":
        rf"(?<!{_RUN})(?=({_RUN}*?)\bsk-)\1sk-{_RUN}{{20,}}\b",
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b":
        rf"(?<!{_RUN})(?=({_RUN}*?)\beyJ)\1eyJ(?=({_RUN}{{8,}}))\2\.(?=({_RUN}{{8,}}))\3\.{_RUN}{{8,}}\b",
}
# W3C tracestate members, as the OpenTelemetry API accepts them.
_TRACESTATE_KEY = re.compile(r"[a-z][_0-9a-z\-*/]{0,255}|[a-z0-9][_0-9a-z\-*/]{0,240}@[a-z][_0-9a-z\-*/]{0,13}")
_TRACESTATE_VALUE = re.compile(r"[\x20-\x2b\x2d-\x3c\x3e-\x7e]{0,255}[\x21-\x2b\x2d-\x3c\x3e-\x7e]")

_NO_HINT = (False, False, False)  # media, audio, image
_SAFE_INTEGER = 2 ** 53 - 1
_PLAIN = (str, bool, int, float, type(None))
_NUMBERS = (int, float, bool)
_SCALARS = (bool, int, float, type(None))
_DROP = object()
_UNPARSED = object()


class _Budget(ValueError):
    """The catalog's depth, node or text limit was reached: the content is not checked, so not sent."""


class _Catalog:
    """One loaded catalog: its credential-name predicate and its media predicate."""

    def __init__(self, catalog: Mapping[str, Any]) -> None:
        normalization = catalog["normalization"]
        if normalization["case"] != "ascii_lowercase":
            raise ValueError("unsupported privacy catalog normalization")
        self._fold = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz",
                                   "".join(normalization["remove"]))
        credentials = catalog["credentials"]
        self.redacted = credentials["replacement"]
        self.patterns = tuple(item["contains"] for item in credentials["patterns"])
        by_id = {item["id"]: item["contains"] for item in credentials["patterns"]}
        self.exclusions = tuple(
            (re.compile(item["regex"]), tuple(by_id[name] for name in item.get("unless_contains_patterns", ())))
            for item in credentials["exclusions"])
        self.contextual = tuple(re.compile(item["path_regex"]) for item in credentials["contextual_exclusions"])
        self.shapes = tuple(self._shape(item) for item in credentials["value_shapes"])
        self.signed = frozenset(self.normalize(name) for name in catalog["signed_url"]["parameters"])
        media = catalog["media"]
        self.prefix_bytes = media["base64_prefix_decoded_bytes"]
        self.threshold = media["unrecognized_base64_min_chars_exclusive"]
        self.base64_fields = frozenset(media["unrecognized_base64_fields"])
        self.base64_marker = media["replacement"]
        self.media_marker = media["other_replacement"]
        self.data_base64_marker = media["data_uri"]["base64_replacement"]
        self.data_marker = media["data_uri"]["other_replacement"]
        self.signatures = tuple(
            (rule["id"], tuple((test["offset"], bytes.fromhex(test["hex"]),
                                bytes.fromhex(test.get("mask", "ff" * (len(test["hex"]) // 2))))
                               for test in rule["tests"]))
            for rule in media["magic"])
        mime = media["mime_siblings"]
        self.mime_names = frozenset(mime["names"])
        self.mime_prefixes = tuple(mime["prefixes"])
        self.mime_exact = frozenset(mime["exact"])
        self.mime_payload = frozenset(mime["payload_fields"])
        image, video = media["image"], media["video"]
        self.image_types = frozenset(image["type_names"])
        self.image_modalities = frozenset(image["modality_names"])
        self.image_payload_fields = frozenset(image["payload_fields"])
        self.video_types = frozenset(video["type_names"])
        self.video_modalities = frozenset(video["modality_names"])
        audio = media["audio"]
        self.audio_fields = frozenset(audio["fields"])
        self.audio_payload = frozenset(audio["payload_fields"])
        self.audio_types = frozenset(audio["type_names"])
        self.audio_metadata = frozenset(audio["metadata_fields"])
        metrics = catalog["scalar_metrics"]
        # Declared counter key -> whether it accepts only nonnegative safe integers.
        self.metrics: Dict[str, bool] = {}
        for container in metrics["containers"]:
            typed = container.get("value_type")
            if typed not in (None, "nonnegative_safe_integer"):
                raise ValueError("unsupported scalar metric type")
            for key in container["keys"]:
                self.metrics[key] = self.metrics.get(key, False) or typed is not None
        decimal = metrics["canonical_decimal"]
        self.decimal = re.compile(decimal["regex"])
        self.decimal_keys = frozenset(decimal["keys"])
        self.decimal_max = decimal["max_chars"]
        self.decimal_base64 = decimal["base64_min_chars"]
        limits = catalog["limits"]
        self.max_depth = limits["max_depth"]
        self.max_nodes = limits["max_nodes"]
        self.max_text = limits["max_text_chars"]
        self._names: Dict[str, bool] = {}

    def _shape(self, item: Mapping[str, Any]) -> Tuple["re.Pattern[str]", Any]:
        """One value shape, and what replaces a match of it."""
        bounded = None if item["flags"] else _BOUNDED_SHAPES.get(item["regex"])
        if bounded is None:
            return re.compile(item["regex"], re.I if "i" in item["flags"] else 0), self.redacted
        redacted = self.redacted
        return re.compile(bounded), lambda match: match.group(1) + redacted

    # Names -------------------------------------------------------------------------------------

    def normalize(self, name: str) -> str:
        return name.translate(self._fold)

    def components(self, path: str) -> List[str]:
        return [part for part in _PARTS.split(path) if part and not part.isdecimal()]

    def leaf(self, path: str) -> str:
        parts = self.components(path)
        return self.normalize(parts[-1]) if parts else ""

    def excluded(self, name: str) -> bool:
        folded = self.normalize(name)
        return any(regex.fullmatch(folded) and not any(marker in folded for marker in guards)
                   for regex, guards in self.exclusions)

    def credential_name(self, name: str) -> bool:
        """The one credential-name predicate: every component of a path, then the folded whole name."""
        known = self._names.get(name)
        if known is None:
            known = self._credential_name(name)
            if len(name) <= 256:
                if len(self._names) >= 8192:
                    self._names.clear()
                self._names[name] = known
        return known

    def _credential_name(self, name: str) -> bool:
        parts = [self.normalize(part) for part in self.components(name)]
        excluded = False
        # An exclusion covers its own component only, never another credential component.
        for index, folded in enumerate(parts):
            context = ".".join(parts[:index + 1])
            if self.excluded(folded) or any(regex.fullmatch(context) for regex in self.contextual):
                excluded = True
            elif any(pattern in folded for pattern in self.patterns):
                return True
        if excluded:
            return False
        folded = self.normalize(name)
        return not self.excluded(name) and any(pattern in folded for pattern in self.patterns)

    def query_name(self, name: str) -> bool:
        return self.normalize(name) in self.signed or self.credential_name(name)

    # Media -------------------------------------------------------------------------------------

    def signature(self, value: str) -> Optional[str]:
        """The media signature at the start of base64 text. Decodes at most 32 bytes, never the media."""
        compact = value.replace("\r", "").replace("\n", "")
        prefix = compact[:(self.prefix_bytes * 8 + 5) // 6]
        try:
            decoded = base64.b64decode(prefix.translate(_URL_SAFE) + "=" * (-len(prefix) % 4),
                                       validate=True)[:self.prefix_bytes]
        except (ValueError, binascii.Error):
            return None
        for rule, tests in self.signatures:
            for offset, expected, mask in tests:
                actual = decoded[offset:offset + len(expected)]
                if len(actual) != len(expected) or any((a & m) != (e & m) for a, e, m in zip(actual, expected, mask)):
                    break
            else:
                return rule
        return None

    def hint(self, name: str, value: Any) -> Tuple[bool, bool, bool]:
        """The (media, audio, image) context a MIME, type or modality value gives its parent object."""
        folded = self.leaf(name)
        if folded not in self.mime_names and folded not in ("type", "modality"):
            return _NO_HINT
        if type(value) is list:
            found = [self._hint(folded, item) for item in value if type(item) is str]
            return any(h[0] for h in found), any(h[1] for h in found), any(h[2] for h in found)
        if type(value) is not str:
            return _NO_HINT
        return self._hint(folded, value)

    def _hint(self, folded: str, value: str) -> Tuple[bool, bool, bool]:
        mime = value.split(";", 1)[0].strip().lower()
        if folded in self.mime_names:
            media = mime in self.mime_exact or any(mime.startswith(prefix) for prefix in self.mime_prefixes)
            return media, mime.startswith("audio/"), mime.startswith("image/")
        if folded == "type":
            # Image and video types establish media context; audio types establish audio context.
            kind = self.normalize(mime)
            image = kind in self.image_types
            return image or kind in self.video_types, self.normalize(value) in self.audio_types, image
        image = mime in self.image_modalities
        return image or mime in self.video_modalities, False, image

    @staticmethod
    def hint_parent(path: str) -> str:
        parts = [part for part in _PARTS.split(path) if part]
        while parts and parts[-1].isdecimal():
            parts.pop()
        return ".".join(parts[:-1])

    def index_hints(self, items: Iterable[Tuple[str, Any]], path: str = "",
                    hints: Optional[Dict[str, Tuple[bool, bool, bool]]] = None) -> Dict[str, Tuple[bool, bool, bool]]:
        """One hint index for native and flattened objects, attributes and query pairs.

        Pairs rather than a mapping, so duplicate query hints combine instead of the last one winning.
        """
        indexed = dict(hints or {})
        for name, item in items:
            found = self.hint(name, item)
            if found[0] or found[1]:
                parent = self.hint_parent(path + "." + name if path else name)
                previous = indexed.get(parent, _NO_HINT)
                indexed[parent] = (previous[0] or found[0], previous[1] or found[1], previous[2] or found[2])
        return indexed

    def context(self, path: str, media: bool = False, audio: bool = False,
                hints: Optional[Dict[str, Tuple[bool, bool, bool]]] = None) -> Tuple[bool, bool, bool]:
        """(media, audio, payload) at ``path``.

        Hints apply at their exact indexed parent; metadata fields reset inherited context. An audio
        field's own samples are payload, its named children are not, unless an explicit payload
        field starts payload ancestry, which only a metadata field ends.
        """
        hints = hints or {}
        root = hints.get("", _NO_HINT)
        media, audio = media or root[0], audio or root[1]
        payload = direct_audio = False
        prefix = ""
        for part in _PARTS.split(path):
            if not part:
                continue
            prefix = prefix + "." + part if prefix else part
            if not part.isdecimal():
                name = self.normalize(part)
                if name in self.audio_metadata:
                    media = audio = payload = False
                direct_audio = name in self.audio_fields
                audio = audio or direct_audio
                if (media and name in self.mime_payload) or (audio and name in self.audio_payload):
                    payload = True
            found = hints.get(prefix, _NO_HINT)
            media, audio = media or found[0], audio or found[1]
        return media, audio, payload or direct_audio

    def image_payload(self, path: str, hints: Optional[Dict[str, Tuple[bool, bool, bool]]] = None) -> bool:
        """A catalog image payload field under an image type, modality or MIME declaration."""
        hints = hints or {}
        image = hints.get("", _NO_HINT)[2]
        payload = False
        prefix = ""
        for part in _PARTS.split(path):
            if not part:
                continue
            prefix = prefix + "." + part if prefix else part
            if not part.isdecimal():
                name = self.normalize(part)
                field = name in self.image_payload_fields
                if name in self.audio_metadata and not field:
                    image = payload = False
                if image and field:
                    payload = True
            image = image or hints.get(prefix, _NO_HINT)[2]
        return payload

    def scalar_metric(self, value: Any, path: str, media: bool = False, audio: bool = False,
                      hints: Optional[Dict[str, Tuple[bool, bool, bool]]] = None) -> bool:
        """A declared usage counter's own value: the media its spelling implies (audio, bytes) does not apply.

        Only the exact complete key, a valid scalar for it, and no MIME or type hint, inherited or on
        any ancestor. Signatures, data URIs and credential rules still apply.
        """
        typed = self.metrics.get(path)
        if typed is None or media or audio:
            return False
        hints = hints or {}
        found = hints.get("", _NO_HINT)
        if found[0] or found[1]:
            return False
        prefix = ""
        for part in _PARTS.split(path):
            if not part:
                continue
            prefix = prefix + "." + part if prefix else part
            found = hints.get(prefix, _NO_HINT)
            if found[0] or found[1]:
                return False
        kind = type(value)
        if not typed:
            return kind in (bool, int, float, str)
        if kind is int:
            return 0 <= value <= _SAFE_INTEGER
        if kind is float:
            return 0 <= value <= _SAFE_INTEGER and value == int(value)
        if kind is str and path in self.decimal_keys:
            return (0 < len(value) <= self.decimal_max and self.decimal.fullmatch(value) is not None
                    and not (len(value) >= self.decimal_base64 and _BASE64.fullmatch(value))
                    and int(value) <= _SAFE_INTEGER)
        return False

    def omitted(self, value: str) -> str:
        return self.base64_marker.replace("{length}", str(len(value)))

    def media_string(self, value: str, path: str, media: bool = False, audio: bool = False,
                     hints: Optional[Dict[str, Tuple[bool, bool, bool]]] = None, framed: bool = True) -> Optional[str]:
        """The one media predicate for text: its replacement, or None when it is not media."""
        if value == self.redacted or value == HIDDEN or _OMISSION.fullmatch(value):
            return None
        if framed:
            replacement = self.data_uri(value)
            if replacement is not None:
                return replacement
        encoded = _BASE64.fullmatch(value) is not None
        payload = self.context(path, media, audio, hints)[2] and not self.scalar_metric(value, path, media, audio, hints)
        if (encoded and self.signature(value)) or payload:
            return self.omitted(value) if encoded else self.media_marker
        if encoded and self.image_payload(path, hints):
            return self.omitted(value)
        if encoded and self.leaf(path) in self.base64_fields and len(value) > self.threshold:
            return self.omitted(value)
        return None

    def data_uri(self, value: str) -> Optional[str]:
        """A whole value that begins with data: and holds a comma is one data URI."""
        if value[:5].lower() != "data:":
            return None
        metadata, comma, payload = value[5:].partition(",")
        if not comma:
            return None
        payload = unquote(payload)  # '+' is a base64 character here, not a space.
        if metadata.lower().endswith(";base64") and (not payload or _BASE64.fullmatch(payload)):
            return self.data_base64_marker.replace("{length}", str(len(payload)))
        return self.data_marker

    def data_text(self, value: str, framed: bool = False) -> str:
        """Data URIs in text; only free text needs delimiter-limited, nonrecursive scanning."""
        if framed:
            replacement = self.data_uri(value)
            if replacement is not None:
                return replacement
        return _DATA.sub(lambda match: self.data_uri(match.group(0)) or match.group(0), value)

    # URLs and text -----------------------------------------------------------------------------

    def userinfo(self, base: str) -> str:
        """``http(s)://user:password@`` becomes ``http(s)://<redacted>@``, read in one pass."""
        scheme = _SCHEME.match(base)
        if scheme is None:
            return base
        rest = base[scheme.end():]
        at = rest.find("@")
        if at < 0:
            return base
        info = rest[:at]
        if "/" in info or ":" not in info[1:-1] or any(char.isspace() for char in info):
            return base
        return base[:scheme.end()] + self.redacted + "@" + rest[at + 1:]

    def url_value(self, original: str) -> str:
        """One already-framed URL; its query values are never delimited as prose."""
        target, hashmark, fragment = original.partition("#")
        base, question, raw_query = target.partition("?")
        base = self.userinfo(base)
        parsed = []
        for pair in raw_query.split("&"):
            raw_name, equals, raw_value = pair.partition("=")
            parsed.append((raw_name, equals, raw_value, unquote_plus(raw_name), unquote_plus(raw_value)))
        # The query is one flat object: index every hint before any value is scrubbed.
        hints = self.index_hints((name, decoded) for _, equals, _, name, decoded in parsed if equals)
        pairs = []
        for raw_name, equals, raw_value, name, decoded in parsed:
            if equals:
                if self.query_name(name):
                    raw_value = self.redacted
                else:
                    replacement = self.media_string(decoded, name, hints=hints)
                    if replacement is None:
                        # One serialization layer, not a recursive scan of nested encoded URLs.
                        cleaned = self.data_text(decoded, framed=True)
                        if cleaned != decoded:
                            replacement = cleaned
                    if replacement is not None:
                        raw_value = replacement
            pairs.append(raw_name + equals + raw_value)
        return self.data_text(base) + question + "&".join(pairs) + hashmark + self.data_text(fragment)

    def is_url_value(self, value: str) -> bool:
        """The whole value is one URL, not prose that starts with one.

        A literal data URI in the query owns its delimiters, so only those values are masked for
        this check; credential and audio redaction never are, since they could hide later prose.
        """
        if _URL.fullmatch(value):
            return True
        if not _URL_START.match(value):
            return False
        target, hashmark, fragment = value.partition("#")
        base, question, raw_query = target.partition("?")
        if not question:
            return False
        pairs = []
        for pair in raw_query.split("&"):
            raw_name, equals, raw_value = pair.partition("=")
            if equals and self.data_uri(unquote_plus(raw_value)) is not None:
                raw_value = self.data_marker
            pairs.append(raw_name + equals + raw_value)
        candidate = base + question + "&".join(pairs) + hashmark + fragment
        return candidate != value and _URL.fullmatch(candidate) is not None

    def text(self, value: str, secrets: Tuple[str, ...], framed: bool = False) -> str:
        """URLs and data URIs in text, then known secrets (longest first), then credential shapes."""
        def token(match: "re.Match[str]") -> str:
            original = match.group(0)
            if match.lastgroup == "data":
                return self.data_text(original)
            # Keep enclosing single quotes in prose; apostrophes inside a URL are URL characters.
            closing_quote = ""
            if match.start() and value[match.start() - 1] == "'" and original.endswith("'"):
                original, closing_quote = original[:-1], "'"
            return self.url_value(original) + closing_quote

        if framed and self.is_url_value(value):
            value = self.url_value(value)
        else:
            value = _TEXT.sub(token, value)
        for secret in secrets:
            if secret in value:
                value = self.redacted.join(part.replace(secret, self.redacted) for part in value.split(self.redacted))
        for shape, replacement in self.shapes:
            value = shape.sub(replacement, value)
        return value

    def members(self, text: str) -> str:
        """Credential-named members of JSON-like text that does not parse become ``<redacted>``.

        A JSON body cut by a length limit, or the repr of a Python mapping, still names its
        credentials. One linear pass; every other character is kept as it was.
        """
        out: List[str] = []
        last = 0
        size = len(text)
        match = _QUOTE.search(text)
        while match is not None:
            start = match.start()
            end = _quoted_end(text, start)
            if end is None:
                break
            colon = _SPACE.match(text, end).end()
            if colon < size and text[colon] == ":" and self.credential_name(text[start + 1:end - 1]):
                value = _SPACE.match(text, colon + 1).end()
                stop = _value_end(text, value)
                out.append(text[last:value])
                out.append(text[start] + self.redacted + text[start])
                last = end = stop
            match = _QUOTE.search(text, end)
        if not out:
            return text
        out.append(text[last:])
        return "".join(out)

    def quoted_media(self, text: str) -> str:
        """Media in JSON-like text that does not parse, found in one pass over its quoted strings.

        A body cut by a length limit, or the repr of a Python mapping, keeps its quoted strings and
        member names but no structure to walk. A quoted base64 value there is omitted when it starts
        with a catalog media signature, when it is longer than the catalog's threshold for
        unrecognized base64, or when it is a payload by the catalog's names: the value of an audio
        field; or of a MIME payload field, an audio payload field or an image payload field, in an
        object that a MIME, type or modality value earlier in it, or an audio field above it, gave
        that context. A hint written after its payload is not seen. Every other character is kept.
        """
        out: List[str] = []
        last = 0
        size = len(text)
        # One frame per open object or array: its bracket, the member it is the value of, and its
        # media, audio and image context. Brackets past the depth limit share the deepest frame.
        frames: List[List[Any]] = [["{", "", False, False, False]]
        deeper = 0
        member = ""  # The member the next value in the innermost object belongs to.
        match = _STRUCTURE.search(text)
        while match is not None:
            start = match.start()
            char = text[start]
            if char == "{" or char == "[":
                parent = frames[-1]
                owner = member if parent[0] == "{" else parent[1]
                media, audio, image = parent[2], parent[3], parent[4]
                if owner in self.audio_metadata:
                    media = audio = False
                    image = image and owner in self.image_payload_fields
                audio = audio or owner in self.audio_fields
                if len(frames) <= self.max_depth:
                    frames.append([char, owner, media, audio, image])
                else:
                    deeper += 1
                member = ""
                match = _STRUCTURE.search(text, start + 1)
                continue
            if char == "}" or char == "]":
                if deeper:
                    deeper -= 1
                elif len(frames) > 1:
                    frames.pop()
                member = ""
                match = _STRUCTURE.search(text, start + 1)
                continue
            end = _quoted_end(text, start)
            if end is not None:
                colon = _SPACE.match(text, end).end()
                if colon < size and text[colon] == ":":
                    member = self.leaf(text[start + 1:end - 1])
                    match = _STRUCTURE.search(text, end)
                    continue
            # A value; a string a cut left open runs to the end of the text.
            stop = size if end is None else end - 1
            frame = frames[-1]
            name = member if frame[0] == "{" else frame[1]
            value = _unescaped(text[start + 1:stop])
            if frame[0] == "{" and (name in self.mime_names or name == "type" or name == "modality"):
                found = self._hint(name, value)
                frame[2], frame[3], frame[4] = frame[2] or found[0], frame[3] or found[1], frame[4] or found[2]
            payload = (name in self.audio_fields or (frame[2] and name in self.mime_payload)
                       or (frame[3] and name in self.audio_payload) or (frame[4] and name in self.image_payload_fields))
            if (value and value != HIDDEN and _BASE64.fullmatch(value) is not None
                    and (payload or len(value) > self.threshold or self.signature(value))):
                out.append(text[last:start + 1])
                out.append(self.omitted(value))
                last = stop
            if end is None:
                break
            match = _STRUCTURE.search(text, end)
        if not out:
            return text
        out.append(text[last:])
        return "".join(out)

    def cut_text(self, text: str) -> str:
        """Text that a value length limit may have cut, already checked as complete text.

        A cut value no longer shows where media begins or ends, so, as Go's agent does, every base64
        run of eight or more characters is omitted, words and numbers included, and a data: token
        becomes its replacement even without its comma. Replacement markers stay.
        """
        def token(match: "re.Match[str]") -> str:
            original = match.group(0)
            if match.lastgroup == "data":
                return self.data_uri(original) or self.data_marker
            if match.lastgroup == "run" and len(original) >= _CUT_RUN:
                return self.omitted(original)
            return original

        return _CUT.sub(token, text)

    # The walk ----------------------------------------------------------------------------------

    def scrub(self, value: Any, path: str = "", siblings: Optional[Mapping[str, Any]] = None,
              secrets: Tuple[str, ...] = (), _media: bool = False, _audio: bool = False, _depth: int = 0,
              _budget: Optional[List[int]] = None, _hints: Optional[Dict[str, Tuple[bool, bool, bool]]] = None,
              framed: bool = True, _cut: bool = False) -> Any:
        """One plain-data value through the catalog, with ``path`` as its structural key.

        ``secrets`` is ordered longest first. ``_cut`` marks a value a value length limit may have
        cut: each text in it then also goes through cut_text. Raises _Budget past the catalog's
        limits, and TypeError for a value that is not plain data; nothing is ever turned into text.
        """
        budget = _budget if _budget is not None else [self.max_nodes, self.max_text]
        budget[0] -= 1
        if _depth > self.max_depth or budget[0] < 0:
            raise _Budget("privacy structure budget")
        if self.credential_name(path):
            return self.redacted
        # Hints are indexed once per object; context is recomputed in path order, so a metadata
        # field resets ancestry and a later local MIME hint starts its own scope.
        hints = _hints if _hints is not None else {}
        if siblings:
            hints = self.index_hints(siblings.items(), hints=hints)
        kind = type(value)
        if kind is dict:
            if len(value) > budget[0]:
                raise _Budget("privacy structure budget")
            for name in value:
                if type(name) is not str:
                    raise TypeError("privacy: object keys must be text")
                budget[1] -= len(name)
            if budget[1] < 0:
                raise _Budget("privacy text budget")
            hints = self.index_hints(value.items(), path, hints)
            return {name: self.scrub(item, path + "." + name if path else name, None, secrets, _media, _audio,
                                     _depth + 1, budget, hints, _cut=_cut)
                    for name, item in value.items()}
        media, audio, payload = self.context(path, _media, _audio, hints)
        if kind is list or kind is tuple:
            if len(value) > budget[0]:
                raise _Budget("privacy structure budget")
            if payload and value and all(type(item) in _NUMBERS for item in value):
                return self.media_marker
            if (not payload and all(type(item) in _SCALARS for item in value)
                    and not self.credential_name(path + ".0")):
                # Numbers outside a payload stay as they are, as walking each one would leave them;
                # every index path has the same name verdict. Spend what that walk would.
                budget[0] -= len(value)
                if value and (_depth + 1 > self.max_depth or budget[0] < 0):
                    raise _Budget("privacy structure budget")
                return list(value)
            return [self.scrub(item, path + "." + str(index), None, secrets, _media, _audio, _depth + 1, budget, hints,
                               _cut=_cut)
                    for index, item in enumerate(value)]
        if kind is not str:
            if kind not in _SCALARS:
                raise TypeError("privacy: not plain data")
            if value is not None and payload and not self.scalar_metric(value, path, _media, _audio, hints):
                return self.media_marker
            return value
        budget[1] -= len(value)
        if budget[1] < 0:
            raise _Budget("privacy text budget")
        cut = _cut and value != HIDDEN
        replacement = self.media_string(value, path, _media, _audio, hints, framed)
        if replacement is not None:
            return replacement
        if value.lstrip().startswith(("{", "[")):
            parsed, duplicated = _parse_json(value)
            if parsed is _UNPARSED:
                # Cut JSON or a Python repr: its credential members, then the media in its strings.
                value = self.quoted_media(self.members(value))
            else:
                clean = self.scrub(parsed, "", None, secrets, media, audio, _depth + 1, budget)
                # A duplicate member's discarded value was never checked: render the checked copy.
                if duplicated or clean != parsed:
                    value = json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
        value = self.text(value, secrets, framed=framed)
        # A cut value is checked as complete text first, then lexically, as the whole text it is now.
        return self.cut_text(value) if cut else value


def _quoted_end(text: str, start: int) -> Optional[int]:
    """The index after the closing quote of the quoted token at ``start``; None when it never closes."""
    quote = text[start]
    index = start + 1
    while True:
        close = text.find(quote, index)
        if close < 0:
            return None
        # An odd run of backslashes right before the quote escapes it.
        slashes = 0
        probe = close - 1
        while probe > start and text[probe] == "\\":
            slashes += 1
            probe -= 1
        if slashes % 2 == 0:
            return close + 1
        index = close + 1


def _unescaped(raw: str) -> str:
    """A quoted token's text with the escapes that base64 or a MIME type can carry undone."""
    if "\\" not in raw:
        return raw
    return raw.replace("\\/", "/").replace("\\n", "\n").replace("\\r", "\r")


def _value_end(text: str, start: int) -> int:
    """The end of the JSON-like value at ``start``: a quoted token, a bracketed group or a bare word."""
    size = len(text)
    if start >= size:
        return size
    char = text[start]
    if char == '"' or char == "'":
        end = _quoted_end(text, start)
        return size if end is None else end
    if char == "{" or char == "[":
        depth = 0
        index = start
        while True:
            found = _STRUCTURE.search(text, index)
            if found is None:
                return size
            index = found.start()
            char = text[index]
            if char == '"' or char == "'":
                end = _quoted_end(text, index)
                if end is None:
                    return size
                index = end
                continue
            if char == "{" or char == "[":
                depth += 1
            else:
                depth -= 1
                if depth == 0:
                    return index + 1
            index += 1
    return _BARE.match(text, start).end()


def _parse_json(value: str) -> Tuple[Any, bool]:
    """Parsed JSON and whether an object repeated a member name; _UNPARSED when it is not JSON."""
    duplicated: List[bool] = []

    def members(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        found: Dict[str, Any] = {}
        for name, item in pairs:
            if name in found:
                duplicated.append(True)
            found[name] = item
        return found

    try:
        return json.loads(value, object_pairs_hook=members), bool(duplicated)
    except ValueError:
        return _UNPARSED, False


# Known secrets -----------------------------------------------------------------------------------


def _ordered(values: Iterable[Any]) -> Tuple[str, ...]:
    """Known secrets longest first, then by text: at least eight characters, never the marker itself."""
    return tuple(sorted({value for value in values
                         if type(value) is str and len(value) >= SECRET_MIN_LENGTH and value != _REDACTED},
                        key=lambda value: (-len(value), value)))


class _KnownSecrets:
    """The process-wide known secrets, replaced in every string the agent sends to Future AGI."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: "OrderedDict[str, None]" = OrderedDict()
        self._snapshot: Optional[Tuple[str, ...]] = ()

    def register(self, value: Any) -> None:
        if type(value) is not str or len(value) < SECRET_MIN_LENGTH or value == _REDACTED:
            return
        with self._lock:
            if value in self._values:
                self._values.move_to_end(value)
                return
            self._values[value] = None
            while len(self._values) > SECRET_LIMIT:
                self._values.popitem(last=False)
            self._snapshot = None

    def snapshot(self) -> Tuple[str, ...]:
        snapshot = self._snapshot
        if snapshot is None:
            with self._lock:
                snapshot = self._snapshot
                if snapshot is None:
                    snapshot = self._snapshot = _ordered(self._values)
        return snapshot


_KNOWN = _KnownSecrets()


def register_secret(value: Any) -> None:
    """Redact ``value`` (at least eight characters) in everything the agent sends to Future AGI."""
    _KNOWN.register(value)


def seed_known_secrets(api_key: Optional[str] = None, secret_key: Optional[str] = None) -> None:
    """Register the Future AGI keys and the value of every credential-named environment variable.

    Python has no transport capture, so these are the only values it registers. A key passed to a
    client in code, or a credential a client builds while it runs, is caught only by the name it is
    recorded under or by its shape.
    """
    known = _KNOWN
    try:
        catalog = _catalog()
        for name, value in list(os.environ.items()):
            if catalog.credential_name(name):
                known.register(value)
    except Exception:
        logger.debug("traceAI: could not read credential-named environment values", exc_info=True)
    known.register(api_key)
    known.register(secret_key)


# The catalog -------------------------------------------------------------------------------------

_catalog_lock = threading.Lock()
_loaded: Optional[_Catalog] = None
_load_error: Optional[str] = None


def _catalog() -> _Catalog:
    """The catalog, loaded once. When it cannot be loaded, every check fails, so no content is sent."""
    global _loaded, _load_error
    catalog = _loaded
    if catalog is not None:
        return catalog
    with _catalog_lock:
        if _loaded is None:
            if _load_error is not None:
                raise RuntimeError(_load_error)
            try:
                with open(CATALOG_PATH, "rb") as handle:
                    _loaded = _Catalog(json.loads(handle.read().decode("utf-8")))
            except Exception as error:
                _load_error = f"the privacy catalog could not be loaded ({type(error).__name__})"
                raise RuntimeError(_load_error) from None
        return _loaded


def scrub(value: Any, path: str = "", siblings: Optional[Mapping[str, Any]] = None,
          secrets: Iterable[str] = (), framed: bool = True, cut: bool = False) -> Any:
    """``value`` through the catalog, as the export sanitizer checks one attribute value.

    ``path`` is its key, ``siblings`` maps the other keys of its object to their values (MIME and
    type hints), ``secrets`` are known secrets, ``framed`` is False only for free text, and ``cut``
    is True for a value that a value length limit may have cut.
    """
    return _catalog().scrub(value, path, siblings, _ordered(secrets), framed=framed, _cut=cut)


class CutKeys(NamedTuple):
    """The keys whose value a value length limit may have cut, per attribute object of one span."""

    span: FrozenSet[str] = frozenset()
    events: Tuple[FrozenSet[str], ...] = ()
    links: Tuple[FrozenSet[str], ...] = ()


def cut_keys(attributes: Optional[Mapping[str, Any]], limit: Optional[int]) -> FrozenSet[str]:
    """The keys of an attribute object that a value length limit of ``limit`` may have cut.

    The SDK cuts a text value, or each text in a sequence value, to exactly ``limit`` characters, so
    a text at least that long may have been longer. Read without calling application code.
    """
    if limit is None or not attributes:
        return frozenset()
    found = set()
    for key, value in attributes.items():
        if type(key) is not str:
            if not isinstance(key, str):
                continue
            key = str.__str__(key)
        values = value if type(value) is list or type(value) is tuple else (value,)
        if any(isinstance(item, str) and str.__len__(item) >= limit for item in values):
            found.add(key)
    return frozenset(found)


# Spans -------------------------------------------------------------------------------------------

# Sanitized resources by identity: a route sends the same resource with every span.
_resources: Dict[int, Tuple[Any, Tuple[str, ...], Any]] = {}
_warned = False


def _warn() -> None:
    global _warned
    if not _warned:
        _warned = True
        logger.warning("traceAI: the content of a span could not be checked for Future AGI, so only its "
                       "ids, timing and status code were sent")
    logger.debug("traceAI: privacy check failed", exc_info=True)


def _plain_text(value: Any) -> str:
    kind = type(value)
    if kind is str:
        return value
    if issubclass(kind, str):
        return str.__str__(value)
    raise TypeError("privacy: not text")


def _attribute(value: Any) -> Any:
    """An attribute value OpenTelemetry can export, or _DROP."""
    kind = type(value)
    if kind is str or kind is bool or kind is int or kind is float or kind is dict:
        return value
    if kind is list:
        kinds = {type(item) for item in value if item is not None}
        if len(kinds) > 1 or not kinds <= {str, bool, int, float}:
            return _DROP
        return tuple(value)
    return _DROP


class _Walker:
    """The sanitizer for one span: the catalog and a snapshot of the known secrets."""

    def __init__(self, catalog: _Catalog, secrets: Tuple[str, ...]) -> None:
        self.catalog = catalog
        self.secrets = secrets

    def budget(self) -> List[int]:
        return [self.catalog.max_nodes, self.catalog.max_text]

    def native(self, value: Any, depth: int = 0) -> Any:
        """Plain data for the walk, read without calling application code; _DROP for anything else."""
        kind = type(value)
        if kind in _PLAIN:
            return value
        if depth >= self.catalog.max_depth:
            return _DROP
        if kind is list or kind is tuple:
            items = []
            for item in value:
                item = self.native(item, depth + 1)
                if item is _DROP:
                    return _DROP
                items.append(item)
            return items
        if kind is dict:
            out = {}
            for key, item in value.items():
                item = self.native(item, depth + 1)
                if type(key) is not str or item is _DROP:
                    return _DROP
                out[key] = item
            return out
        if issubclass(kind, (bytes, bytearray, memoryview)):
            return self.catalog.media_marker
        # Subclasses of plain types: their base value, without their own methods.
        if issubclass(kind, str):
            return str.__str__(value)
        if issubclass(kind, int):
            return int.__int__(value)
        if issubclass(kind, float):
            return float.__float__(value)
        return _DROP

    def text(self, value: Any) -> str:
        clean = self.catalog.scrub(_plain_text(value), "", None, self.secrets, _budget=self.budget())
        if type(clean) is not str:
            raise TypeError("privacy: text became structure")
        return clean

    def attributes(self, attributes: Optional[Mapping[str, Any]], session_id: Optional[str] = None,
                   cut: FrozenSet[str] = frozenset()) -> Dict[str, Any]:
        """One attribute object: every value checked with its key as path and the others as siblings.

        An attribute that cannot be checked is dropped alone. ``session_id`` is the session.id value
        that keeps its value: it skips only the name rule. ``cut`` holds the keys whose value a value
        length limit may have cut.
        """
        if not attributes:
            return {}
        native: Dict[str, Any] = {}
        for key, value in attributes.items():
            if type(key) is not str:
                if not issubclass(type(key), str):
                    continue
                key = str.__str__(key)
            item = self.native(value)
            if item is not _DROP:
                native[key] = item
        catalog = self.catalog
        hints = catalog.index_hints(native.items())
        out: Dict[str, Any] = {}
        for key, item in native.items():
            try:
                budget = self.budget()
                name = catalog.scrub(key, "", None, self.secrets, _budget=budget)
                was_cut = key in cut
                if session_id is not None and key == SESSION_ID and item == session_id:
                    clean = catalog.scrub(item, "", None, self.secrets, _budget=budget, _cut=was_cut)
                else:
                    clean = catalog.scrub(item, key, None, self.secrets, _depth=1, _budget=budget, _hints=hints,
                                          _cut=was_cut)
            except Exception as error:
                logger.debug("traceAI: an attribute could not be checked for Future AGI and was not sent (%s)",
                             type(error).__name__)
                continue
            clean = _attribute(clean)
            if clean is not _DROP and type(name) is str and name not in out:
                out[name] = clean
        return out

    def context(self, context: Optional[trace_api.SpanContext]) -> Optional[trace_api.SpanContext]:
        """The span context with its tracestate entries checked; an entry that cannot be kept is left out."""
        if context is None or not context.trace_state:
            return context
        entries: List[Tuple[str, str]] = []
        seen = set()
        changed = False
        for key, value in context.trace_state.items():
            clean_key = self.text(key)
            clean = self.catalog.scrub(_plain_text(value), key, None, self.secrets, _budget=self.budget())
            if clean_key != key or clean != value:
                changed = True
            if (type(clean) is not str or clean_key in seen or not _TRACESTATE_KEY.fullmatch(clean_key)
                    or not _TRACESTATE_VALUE.fullmatch(clean)):
                changed = True
                continue
            seen.add(clean_key)
            entries.append((clean_key, clean))
        if not changed:
            return context
        return trace_api.SpanContext(context.trace_id, context.span_id, context.is_remote, context.trace_flags,
                                     trace_api.TraceState(entries))

    def resource(self, resource: Optional[Resource]) -> Optional[Resource]:
        """The resource checked once per resource object; an unchanged one is passed on as it is."""
        if resource is None:
            return None
        cached = _resources.get(id(resource))
        if cached is not None and cached[0] is resource and cached[1] is self.secrets:
            return cached[2]
        attributes = dict(resource.attributes)
        clean = self.attributes(attributes)
        schema = resource.schema_url
        clean_schema = self.text(schema) if schema else schema
        result = resource if clean == attributes and clean_schema == schema else Resource(clean, clean_schema)
        if len(_resources) >= 16:
            _resources.clear()
        _resources[id(resource)] = (resource, self.secrets, result)
        return result

    def scope(self, scope: Optional[InstrumentationScope]) -> Optional[InstrumentationScope]:
        if scope is None:
            return None
        name = self.text(scope.name)
        version = self.text(scope.version) if scope.version else scope.version
        schema = self.text(scope.schema_url) if scope.schema_url else scope.schema_url
        raw = getattr(scope, "attributes", None)
        attributes = dict(raw) if raw else None
        clean = self.attributes(attributes) if attributes else attributes
        if name == scope.name and version == scope.version and schema == scope.schema_url and clean == attributes:
            return scope
        if clean is None:
            return InstrumentationScope(name, version, schema)
        return InstrumentationScope(name, version, schema, clean)

    def span(self, span: ReadableSpan, session_id: Optional[str] = None, cut: Optional[CutKeys] = None) -> ReadableSpan:
        cut = cut or CutKeys()

        def nth(keys: Tuple[FrozenSet[str], ...], index: int) -> FrozenSet[str]:
            return keys[index] if index < len(keys) else frozenset()

        status = span.status
        if status.description is not None:
            status = Status(status.status_code, self.text(status.description))
        return ReadableSpan(
            name=self.text(span.name), context=self.context(span.context), parent=self.context(span.parent),
            resource=self.resource(span.resource), attributes=self.attributes(span.attributes, session_id, cut.span),
            events=[Event(self.text(event.name), self.attributes(event.attributes, cut=nth(cut.events, index)),
                          timestamp=event.timestamp)
                    for index, event in enumerate(span.events)],
            links=[Link(self.context(link.context), self.attributes(link.attributes, cut=nth(cut.links, index)))
                   for index, link in enumerate(span.links)],
            kind=span.kind, status=status, start_time=span.start_time, end_time=span.end_time,
            instrumentation_scope=self.scope(span.instrumentation_scope),
        )


def _bare(context: Optional[trace_api.SpanContext]) -> Optional[trace_api.SpanContext]:
    if context is None or not context.trace_state:
        return context
    return trace_api.SpanContext(context.trace_id, context.span_id, context.is_remote, context.trace_flags)


def _unchecked(span: ReadableSpan, walker: Optional[_Walker]) -> Optional[ReadableSpan]:
    """Identity and timing only: ids without tracestate, kind, times and the status code."""
    try:
        resource = Resource.get_empty()
        if walker is not None:
            try:
                resource = walker.resource(span.resource) or resource
            except Exception:
                resource = Resource.get_empty()
        return ReadableSpan(
            name=_UNCHECKED, context=_bare(span.context), parent=_bare(span.parent), resource=resource,
            attributes={}, events=[], links=[], kind=span.kind, status=Status(span.status.status_code),
            start_time=span.start_time, end_time=span.end_time, instrumentation_scope=None,
        )
    except Exception:
        return None


def sanitize_span(span: ReadableSpan, session_id: Optional[str] = None,
                  cut: Optional[CutKeys] = None) -> Optional[ReadableSpan]:
    """The copy of ``span`` that Future AGI receives, with the catalog applied.

    ``span`` is already the agent's detached copy, after the TraceConfig hide settings; it is not
    changed. ``session_id`` is the session.id value that keeps its value although its name is
    credential-like; it is still checked for media, URLs, known secrets and credential shapes.
    ``cut`` holds the keys whose value the provider's value length limit may have cut. When the span
    cannot be checked, the copy keeps only its identity and timing; None when even that cannot be
    built.
    """
    try:
        walker = _Walker(_catalog(), _KNOWN.snapshot())
    except Exception:
        _warn()
        return _unchecked(span, None)
    try:
        return walker.span(span, session_id, cut)
    except Exception:
        _warn()
        return _unchecked(span, walker)


def _after_fork() -> None:
    """No thread survives fork, so none of its locks may stay held in the child."""
    global _catalog_lock
    _catalog_lock = threading.Lock()
    _KNOWN._lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)
