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
  ``<redacted>``, and so do credential-named pairs after ``#``, after a second ``?`` and in path
  segments.
- In any text, the value of every credential-named pair (``name=value``, ``name: value``, a quoted
  name, ``--name value``) becomes ``<redacted>`` to the true end of the value, after known secrets
  and credential shapes (the shared spec's pair rule; a name that cannot be read within its bounds
  counts as a credential name). Object and attribute keys go through the text rules too.
- Data URIs, base64 media recognized by its first bytes, and media or audio payloads become
  ``<omitted: ...>`` markers.
- Credential-shaped values and known secrets (the Future AGI keys and the values of
  credential-named environment variables, registered when the agent starts) become ``<redacted>``
  wherever they appear.

The application's own processors and exporters never see this copy. Work is bounded by the
catalog's depth, node and text limits, and by a limit on the path of a value (its attribute key and
the member names above it): the name checks at each value read its whole path, so a path longer
than PATH_LIMIT characters or with more than PATH_PARTS components is not checked, and its
attribute is left out. A name of more than the catalog's max_depth components (a path, a key, a hint
name or a pair name in text) makes the whole value ``<redacted>``. The checks of a URL query value read the parameter's name as its path, so a
query name past the same limits is not checked either, and its value becomes ``<redacted>``.
A text scan reads each run of characters a bounded number of times: the catalog's JWT shape would
make Python's backtracking engine walk a long run back from every start in it, so it is scanned
by segments in one pass over each run, whatever the length of the token. Content that cannot be checked within
those limits, or whose type is not plain data, is dropped without being turned into text; a span
that cannot be checked at all keeps only its identity and timing.
"""

import base64
import binascii
import bisect
import json
import logging
import os
import re
import threading
import unicodedata
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
#: The longest path, in characters and in components, whose names are checked. The checks at each
#: value read its whole path, so without this limit a long member name above many values would be
#: read once per value. A value past it fails its attribute: the attribute is left out. A URL
#: query parameter's name is the path of its value: past the limit, that value becomes <redacted>.
PATH_LIMIT = 1024
PATH_PARTS = 64

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
_SPACE = re.compile(r"[ \t\r\n]*")
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
# The catalog's jwt shape, which Python's backtracking engine would retry from every start in a long
# run of 'eyJ-' that cannot match, so it is scanned by _JwtShape; and the run that scan reads.
_JWT_SHAPE = r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
_JWT_RUN = re.compile(r"[A-Za-z0-9_.-]+")


class _JwtShape:
    """The catalog's jwt shape, found in one pass over each run of ``[A-Za-z0-9_.-]``, with the same
    matches ``re.sub`` gives it.

    The token classes exclude the dot, so each segment of a run (split at its dots) is taken whole.
    A match is three consecutive segments: the first holds an ``eyJ`` that starts at a word boundary
    (the start of the segment after a dot, or after a hyphen; at the start of a run, only when the
    character before it is not a word character, as ``\\b`` is defined for ``str``) with at least eight
    characters after it, the other two have at least eight characters. Only the leftmost ``eyJ`` of the
    first segment can have enough characters after it. A failed triple moves on by one segment and a
    match by three, so every segment is read a constant number of times and the scan is linear.
    """

    @staticmethod
    def _start(segment: str, boundary: bool) -> int:
        """The index of the leftmost ``eyJ`` of a segment that starts a token, or -1."""
        at = segment.find("eyJ")
        while at >= 0:
            if boundary if at == 0 else segment[at - 1] == "-":
                return at if len(segment) - at - 3 >= 8 else -1
            at = segment.find("eyJ", at + 1)
        return -1

    def sub(self, replacement: str, text: str) -> str:
        found = self.spans(text)
        if not found:
            return text
        out: List[str] = []
        done = 0  # text before this index is already in `out`
        for start, end in found:
            out.append(text[done:start])
            out.append(replacement)
            done = end
        out.append(text[done:])
        return "".join(out)

    def spans(self, text: str) -> List[Tuple[int, int]]:
        """The (start, end) of every match, left to right, as ``re.finditer`` gives them."""
        spans: List[Tuple[int, int]] = []
        if "eyJ" not in text:
            return spans
        for run in _JWT_RUN.finditer(text):
            body = run.group()
            if "eyJ" not in body:
                continue
            at = base = run.start()
            segments: List[Tuple[int, int]] = []
            for part in body.split("."):
                segments.append((at, at + len(part)))
                at += len(part) + 1
            before = text[base - 1] if base else ""
            first_ok = not (before.isalnum() or before == "_")
            i = 0
            while i + 2 < len(segments):
                (a, b), (c, d), (e, f) = segments[i:i + 3]
                found = -1
                if d - c >= 8 and f - e >= 8:
                    found = self._start(text[a:b], first_ok if i == 0 else True)
                if found < 0:
                    i += 1
                    continue
                spans.append((a + found, f))
                i += 3
        return spans


def _shape_spans(shape: Any, text: str) -> List[Tuple[int, int]]:
    """The (start, end) of every match of a value shape, left to right."""
    if type(shape) is _JwtShape:
        return shape.spans(text)
    return [match.span() for match in shape.finditer(text)]


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


# The credential pair rule ------------------------------------------------------------------------
#
# A port of the shared spec's reference recognizer (generate_privacy_fixtures.py, class Reference), kept
# close to it so that both read every text the same way.


def _normalize(name):
    return name.translate(_PAIR_FOLD)


_PAIR_FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz", "-_.")

_JSON_SCALAR_CHARS = frozenset("0123456789+-.eEtrufalsn")
_JSON_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")

# The credential pair rule. Every exported string passes known-secret replacement, then the catalog's value shapes, then this rule
# (CredentialPairs at lane/traceai/java-shared-pair-rule-r3 is the reference shape): the value of every credential-named name=value,
# name: value, "name": value or --name value runs to the true end of its value and becomes the placeholder. The value end, the name
# predicate and the name caps follow that source; the rule is written here from the ruling, never from Java output.
#
# Space, for word and value ends, is Java's Character.isWhitespace: U+0009-U+000D, U+001C-U+001F and every Unicode space, line or
# paragraph separator (Zs, Zl, Zp) except the no-break spaces U+00A0, U+2007 and U+202F. U+0085 is not space. The next-pair look
# for an equals sign that is padding uses Java's \s, which is ASCII only: U+0020 and U+0009-U+000D. Positions and the bounded windows
# (8 spaces, 128 name characters, 130 for a quoted name, 512 and 1024 for user information) count code points here and UTF-16 units
# in Java: they differ only when a character outside the BMP falls inside such a window.
_PAIR_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-[]")
_PAIR_NAME_START = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_PAIR_PADDING_FOLLOWERS = frozenset("= \t\n\x0b\x0c\r,;)]}\"'")
_PAIR_LIST_HEADERS = frozenset(("cookie", "setcookie", "authorization", "proxyauthorization"))
# A name, wherever its length is bounded (a pair rule name, a quoted name, a --name, a name after a second '?'), is read through its
# last NAME_CAP characters; when the name runs on past that window the name is not decided and the pair is replaced (fail closed).
_NAME_CAP = 128
# A name after a '?' that holds more '?' marks than this is not decided either (fail closed): each '=' then reads at most
# QUESTION_CAP names of at most NAME_CAP characters, so the second '?' read stays linear with a small constant.
_QUESTION_CAP = 8
# The longest readable gap between a pair name and its separator: plain spaces only.
_GAP_CAP = 8
# The quote spellings of a quoted name: a quote or apostrophe, an escaped quote inside a JSON string, an HTML entity quote.
_ESCAPED_QUOTE, _ENTITY_QUOTE = '\\"', "&quot;"
# '=' percent-encoded, after its '%': the pair rule and the URL path pair rule read it as a separator.
_ENCODED_EQUALS = frozenset(("3D", "3d"))
# The separators between pairs inside one URL path segment.
_PATH_PAIR_SEPARATORS = frozenset(";,&")
# Whitespace spelled as a percent escape (any hex case) and the JSON string escapes of whitespace: an encoded gap between a name and
# its separator is as unreadable as the whitespace it stands for.
_ENCODED_SPACES = frozenset(("20", "09", "0A", "0B", "0C", "0D"))
_ENCODED_NBSP = "%C2%A0"
_JSON_SPACE_ESCAPES = frozenset("tnrfb")
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
# A line terminator that Java's '$' (no MULTILINE) accepts before the end of the text.
_JAVA_FINAL_TERMINATORS = ("\r\n", "\n", "\r", "\u0085", "\u2028", "\u2029")
# Phase 1e (I1): a line break is never part of a --name gap; it ends the --name on its line, and the next line is read fresh.
_LINE_BREAKS = frozenset("\n\r\u0085\u2028\u2029\x0c\x0b")
# Phase 1e r2: a quoted name takes '=' and %3D as well as ':' (the Java comparison switches this off to attribute a difference).
_QUOTED_EQUALS = True


def _java_space(c):
    if c in "\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f":
        return True
    if c in "\u00a0\u2007\u202f":
        return False
    return unicodedata.category(c) in ("Zs", "Zl", "Zp")


def _java_letter_or_digit(c):
    return unicodedata.category(c) in ("Lu", "Ll", "Lt", "Lm", "Lo", "Nd")


def _java_lower(c):
    # Java's Character.toLowerCase is the simple one-to-one mapping; Python's lower() is the full mapping (U+0130 becomes two).
    lowered = c.lower()
    return lowered if len(lowered) == 1 else ("i" if c == "\u0130" else c)


def _next_pair(text, start, spaced):
    """Whether another pair starts at start: [ \\t]{0,8} (spaced only), -{0,2}, a name of 1 to 128 characters that starts with a letter
    or underscore, [ ]{0,8} (spaced only), then ':' or an '=' that is not padding or an empty value. Every part is anchored and
    bounded; the parts cannot overlap, so one forward read decides it with no backtracking."""
    length, i = len(text), start
    if spaced:
        while i < length and text[i] in " \t" and i - start <= 8:
            i += 1
        if i - start > 8:
            return False
    dashes = i
    while i < length and text[i] == "-" and i - dashes <= 2:
        i += 1
    if i - dashes > 2 or i >= length or text[i] not in _PAIR_NAME_START:
        return False
    name = i
    i += 1
    while i < length and text[i] in _PAIR_NAME_CHARS and i - name < 128:
        i += 1
    if i < length and text[i] in _PAIR_NAME_CHARS:
        return False
    if spaced:
        gap = i
        while i < length and text[i] == " " and i - gap <= 8:
            i += 1
        if i - gap > 8:
            return False
    if i >= length:
        return False
    if text[i] == ":":
        return True
    if text[i] != "=":
        return False
    rest = i + 1
    # Only the last one or two characters can be a final terminator; never copy the rest of the text to find out.
    return not (rest >= length or text[rest] in _PAIR_PADDING_FOLLOWERS
                or (length - rest <= 2 and text[rest:] in _JAVA_FINAL_TERMINATORS))


def _pair_value_end(text, start):
    """The true end of a name=value value: a pair boundary with no bracket open, or the end of the text. A value that starts with a
    private key header runs to the key's end line; a quoted value is read to its closing quote and then on like any other."""
    length = len(text)
    if start >= length:
        return length
    if text.startswith("-----BEGIN", start):
        end = text.find("-----END", start)
        if end < 0:
            return length
        close = text.find("-----", end + 8)
        return length if close < 0 else close + 5
    first, i = text[start], start
    if first in "\"'":
        i = start + 1
        while i < length and text[i] != first:
            i += 2 if text[i] == "\\" else 1
        if i >= length:
            return length
        i += 1
    depth = 0
    while i < length:
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth = max(depth - 1, 0)
        elif depth == 0:
            if c in ",;\n\r" and _next_pair(text, i + 1, True):
                return i
            if c == " " and _next_pair(text, i + 1, False):
                return i
        i += 1
    return length


def _encoded_space_width(text, end, floor):
    """The width of the encoded whitespace that ends at end, read back no further than floor, or 0: a percent escape (%20, %09,
    %0A, %0B, %0C, %0D or %C2%A0, any hex case) or a JSON string escape (\\t, \\n, \\r, \\f, \\b, or \\u and four hex digits of a
    whitespace character). A backslash that an earlier backslash escapes starts no escape. The only look back is over the backslashes
    right before the escape, which no other look reads."""
    if end - 3 >= floor and text[end - 3] == "%" and text[end - 2:end].upper() in _ENCODED_SPACES:
        return 3
    if end - 6 >= floor and text[end - 6:end].upper() == _ENCODED_NBSP:
        return 6
    width = 0
    if end - 2 >= floor and text[end - 2] == "\\" and text[end - 1] in _JSON_SPACE_ESCAPES:
        width = 2
    elif end - 6 >= floor and text[end - 6:end - 4] == "\\u" and all(c in _HEX_DIGITS for c in text[end - 4:end])\
            and chr(int(text[end - 4:end], 16)).isspace():
        width = 6
    if not width:
        return 0
    slashes = 1
    while end - width - slashes >= floor and text[end - width - slashes] == "\\":
        slashes += 1
    return width if slashes % 2 else 0


def _gap_start(text, end, floor):
    """The start of the gap that ends at end, read back no further than floor: whitespace (str.isspace, any length) and encoded
    whitespace (encoded_space_width), in any order. Each gap is read by the one separator after it, so the reads stay linear in
    the text."""
    while end > floor:
        if text[end - 1].isspace():
            end -= 1
            continue
        width = _encoded_space_width(text, end, floor)
        if not width:
            break
        end -= width
    return end


def _escape_before(text, end, floor):
    """Whether a percent escape (a '%' and two hex digits) ends at end: the read-back of a name stops there."""
    return end - 3 >= floor and text[end - 3] == "%" and text[end - 2] in _HEX_DIGITS and text[end - 1] in _HEX_DIGITS


_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")


def _escaped_name(text, name_start, name_end, floor):
    """Phase 1e, I2: the name before a separator that ends at a percent escape, read back over name characters and %XX escapes (an
    encoded separator, %3D or %3d, ends it) and percent-decoded ONCE: (decoded, undecided), or None when the name does not end at an
    escape. name_start is where the plain read-back (which stops at any escape, 1d item 1) stopped. The name ends at an escape when a
    %XX other than %3D / %3d is right before name_end, or when the plain read-back stopped at a %25 escape (double encoding: "%255D"). The pair is undecided
    when a %XX remains after the one decode, when the read found no letter, digit or underscore, or when the name runs on past
    NAME_CAP characters."""
    if not ((_escape_before(text, name_end, floor) and text[name_end - 2:name_end] not in _ENCODED_EQUALS) or
            (name_start > floor and _escape_before(text, name_start, floor) and text[name_start - 2:name_start] == "25")):
        # No name ends here (an encoded separator right before this separator, as in %3D%3D, leaves no name at all).
        return None
    start = name_end
    while start > floor and name_end - start < _NAME_CAP:
        if _escape_before(text, start, floor):
            if text[start - 2:start] in _ENCODED_EQUALS:
                break
            start -= 3
        elif text[start - 1] in _PAIR_NAME_CHARS:
            start -= 1
        else:
            break
    overflow = name_end - start >= _NAME_CAP and start > floor and (text[start - 1] in _PAIR_NAME_CHARS or _escape_before(text, start, floor))
    decoded = unquote(text[start:name_end])
    undecided = overflow or bool(_PERCENT_ESCAPE.search(decoded)) or not any(ch.isalnum() or ch == "_" for ch in decoded)
    return decoded, undecided


_DATA_LINE = re.compile(r"[\r\n]+([A-Za-z0-9+/_=\-]+)")
_BASE64_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/_-")


_BASE64_INDEX = {c: k for k, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")}
_BASE64_INDEX.update({"-": 62, "_": 63})


def _padding(body, pad, base64, padded, count):
    """Whether pad '=' characters after body are trailing base64 padding (phase 1e, I4): the URI is ;base64, no padding came before,
    one or two '=', the base64 characters read so far, body and the padding are a multiple of four, the last character before the
    padding is a canonical one (its unused low bits are zero) and the line is no single-dash flag ("-api_key=")."""
    if not base64 or padded or pad > 2 or not body or body.startswith("-") or (count + len(body) + pad) % 4:
        return False
    return _BASE64_INDEX[body[-1]] % (4 if pad == 1 else 16) == 0


def _data_uri_end(text, start, end):
    """Phase 1e, I4: the end of the data URI that DATA matched at text[start:end]. A base64 continuation line belongs to it only if its
    '=' characters are trailing base64 padding: the URI is ;base64, the '=' run (one or two) ends the line, no padding came before,
    and the base64 characters read so far and the padding are a multiple of four. A line followed by ':' or %3D is a pair name, never
    base64 (no rule leaves a separator unread at the end of the text it consumed). The match is cut before the first line that does
    not belong; the text after the cut is read as any other text."""
    head = start + 5
    while head < end and text[head] not in "\r\n":
        head += 1
    metadata, comma, payload = text[start + 5:head].partition(",")
    base64 = bool(comma) and metadata.lower().endswith(";base64")
    decoded = unquote(payload)
    count, padded = sum(1 for ch in decoded if ch in _BASE64_ALPHABET), "=" in decoded
    position = head
    while position < end:
        line = _DATA_LINE.match(text, position, end)
        if line is None:
            break
        after, gap = line.end(), line.end()
        while gap < len(text) and (_dashes_space(text[gap]) or _encoded_space_ahead(text, gap)):
            gap += _encoded_space_ahead(text, gap) or 1
        if gap < len(text) and (text[gap] in "=:" or text[gap:gap + 3] in ("%3D", "%3d")):
            break   # a name, a gap and a separator: a pair, never base64
        body = line.group(1).rstrip("=")
        pad = len(line.group(1)) - len(body)
        if body.startswith("--") or "=" in body or (pad and not _padding(body, pad, base64, padded, count)):
            break   # a --name line, an '=' inside the line, or an '=' that is not trailing base64 padding
        count, padded, position = count + len(body), padded or bool(pad), after
    return position


def _scan_text(pattern, text):
    """The matches of pattern (DATA, or TEXT with its data group) in text, left to right and not overlapping, as re.sub and
    re.finditer read them, with each data URI cut by data_uri_end (I4): (start, end, group name)."""
    position = 0
    while position <= len(text):
        match = pattern.search(text, position)
        if match is None:
            return
        name = match.lastgroup if pattern.groupindex else "data"
        end = _data_uri_end(text, match.start(), match.end()) if name == "data" else match.end()
        yield match.start(), end, name
        position = end if end > match.start() else match.start() + 1


def _substitute(pattern, text, replace):
    """re.sub over scan_text: replace(original, group name) gives each match's text."""
    out, copied = [], 0
    for start, end, name in _scan_text(pattern, text):
        out.append(text[copied:start])
        out.append(replace(text[start:end], name, start))
        copied = end
    out.append(text[copied:])
    return "".join(out)


def _dashes_space(c):
    """Whether c belongs to the gap after a --name (phase 1e, I1): any str.isspace character that is not a line break, NBSP included.
    A line break ends the --name on its line."""
    return c.isspace() and c not in _LINE_BREAKS


def _word_space(c):
    """Whether c ends a --name's value word (phase 1e, I1): any str.isspace character. A small function so that the Java comparison
    can switch the item off (the agent ends the word at Character.isWhitespace, java_space)."""
    return c.isspace()


def _encoded_control(text, start, width):
    """Whether the encoded whitespace of width at start (encoded_space_ahead) stands for a control character (phase 1e, I5): %09,
    %0A, %0B, %0C and %0D in any hex case, the JSON escapes \\t \\n \\r \\f \\b, and a \\u escape of a control. %20, %C2%A0 and a \\u
    escape of a space or separator are encoded spaces, not controls."""
    if text[start] == "%":
        return width == 3 and text[start + 1:start + 3] != "20"
    if width == 2:
        return True
    return unicodedata.category(chr(int(text[start + 2:start + 6], 16))) == "Cc"


def _separator_at(text, k):
    """The width of the pair separator at k ('=', ':' that does not open "://", or %3D / %3d), or 0 (phase 1e, I1)."""
    c = text[k]
    if c == "=" or (c == ":" and not text.startswith("//", k + 1)):
        return 1
    return 3 if c == "%" and text[k + 1:k + 3] in _ENCODED_EQUALS else 0


def _separator_value_end(text, k, size):
    """The true end of the value of the pair whose separator (of size) is at k, as the separator branch reads it: up to 8 spaces after
    the separator, then pair_value_end, or the line end after a list header name (read back over name characters)."""
    value = k + size
    while value < len(text) and value - k - size < 8 and text[value] == " ":
        value += 1
    name = k
    while name > 0 and k - name < _NAME_CAP and text[name - 1] in _PAIR_NAME_CHARS:
        name -= 1
    if _normalize(text[name:k]) in _PAIR_LIST_HEADERS:
        while value < len(text) and text[value] not in "\n\r":
            value += 1
        return value
    return _pair_value_end(text, value)


def _consumed_end(text, start, stop, decide):
    """Phase 1e, I1 (general invariant): no rule leaves an '=', ':' or %3D unread at the end of the text it consumed. While
    text[start:stop] ends at a separator that is a credential or undecided pair (decide(k, size, floor) gives that pair's value end,
    or -1), the replacement runs on to that pair's value end. Each step moves forward."""
    while stop > start:
        if text[stop - 1] in "=:" and _separator_at(text, stop - 1):
            size = 1
        elif stop - 3 >= start and _separator_at(text, stop - 3) == 3:
            size = 3
        else:
            return stop
        end = decide(stop - size, size, start)
        if end <= stop:
            return stop
        stop = end
    return stop


def _dashes_colon_at(text, value):
    """The index of the ':' that ends a --name's gap and starts its value, or -1: that ':' is the pair's separator, never the start of
    "://" user information."""
    return value if text[value] == ":" else -1


def _encoded_space_ahead(text, start):
    """The width of the encoded whitespace (percent escape or JSON string escape, as encoded_space_width) that starts at start, or 0."""
    if text.startswith("%", start):
        if text[start + 1:start + 3].upper() in _ENCODED_SPACES:
            return 3
        return 6 if text[start:start + 6].upper() == _ENCODED_NBSP else 0
    if text.startswith("\\", start):
        if text[start + 1:start + 2] in _JSON_SPACE_ESCAPES and text[start + 1:start + 2]:
            return 2
        digits = text[start + 2:start + 6]
        if text[start + 1:start + 2] == "u" and len(digits) == 4 and all(c in _HEX_DIGITS for c in digits) and chr(int(digits, 16)).isspace():
            return 6
    return 0


def _dashes_separator(text, start):
    """Whether the word after a --name and its gap starts with a separator ('=', ':' or %3D): the pair is then read at the separator,
    as name<gap>=value with the gap rule, not by the --name rule."""
    return start < len(text) and (text[start] in "=:" or (text[start] == "%" and text[start + 1:start + 3] in _ENCODED_EQUALS))


def _flag_dashes(text, i):
    """The dashes of a flag that starts at i (phase 1e r2): 2 for "--", 1 for a single dash followed by a name start (a letter or an
    underscore), else 0. Whether a single-dash flag is a pair is decided by its name: a full credential name of at least two
    characters, never a short flag."""
    if text.startswith("--", i):
        return 2
    return 1 if text.startswith("-", i) and text[i + 1:i + 2] in _PAIR_NAME_START and text[i + 1:i + 2] else 0


def _readable_gap(text, start, end):
    """Whether the gap text[start:end] between a name and its separator is readable: 0 to GAP_CAP plain spaces (U+0020). Any other
    whitespace, encoded whitespace included, or more spaces, leaves the name undecided (its value is replaced)."""
    return end - start <= _GAP_CAP and all(text[k] == " " for k in range(start, end))


def _quote_before(text, end):
    """The quote spelling that ends at end: the HTML entity &quot;, an escaped quote (a quote after an odd run of backslashes, read
    back at most NAME_CAP characters), a quote or an apostrophe; or None."""
    if end >= len(_ENTITY_QUOTE) and text.startswith(_ENTITY_QUOTE, end - len(_ENTITY_QUOTE)):
        return _ENTITY_QUOTE
    if end < 1 or text[end - 1] not in "\"'":
        return None
    if text[end - 1] == '"':
        slashes = 0
        while end - 2 - slashes >= 0 and text[end - 2 - slashes] == "\\" and slashes < _NAME_CAP:
            slashes += 1
        if slashes % 2:
            return _ESCAPED_QUOTE
    return text[end - 1]


def _json_name(text, colon):
    """A quoted name that ends, after a whitespace gap, before the colon: (opening, quote, close, overflow), or None.

    quote is the spelling that closes the name at close (see quote_before); the name is text[opening + len(quote):close]. The opening
    quote is looked for only within the 130 characters before the closing one, never back to the start of the text. When that window
    ends inside the text without one, the name is longer than the cap and is not read: overflow is true and opening is -1. A gap
    that is not readable (readable_gap: a tab or any other whitespace but U+0020, or more than GAP_CAP spaces) leaves the name
    undecided: overflow is true too, and the value is replaced."""
    end = _gap_start(text, colon, 0)
    quote = _quote_before(text, end)
    if quote is None or (len(quote) == 1 and end < 3):
        return None
    close = end - len(quote)
    floor = max(0, close - 130)
    opening = text.rfind(quote, floor, close)
    if opening >= 0:
        return opening, quote, close, not _readable_gap(text, end, colon)
    return (-1, quote, close, True) if floor > 0 else None


def _json_string_end(text, start):
    """The index after a quoted string that starts at start (a backslash escapes the next character), or the end of the text."""
    quote = text[start]
    i = start + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            return i + 1
        i += 1
    return len(text)


def _json_member_boundary(text, end):
    """Whether only optional space and then ',', '}', ']' or the end of the text follow end."""
    while end < len(text) and _java_space(text[end]):
        end += 1
    return end >= len(text) or text[end] in ",}]"


def _escaped_string_end(text, start):
    """The index after an escaped JSON string inside a JSON string (\\"...\\") that starts at start, or the end of the text. The text is
    read one escape level down: \\\\ is a backslash of the inner string, which escapes the inner character after it, and the first \\"
    that no inner backslash escapes closes the string."""
    length, i, inner_escape = len(text), start + len(_ESCAPED_QUOTE), False
    while i < length:
        if text[i] == "\\" and i + 1 < length:
            outer, i = text[i + 1], i + 2
            if inner_escape:
                inner_escape = False
            elif outer == "\\":
                inner_escape = True
            elif outer == '"':
                return i
        else:
            i += 1
            inner_escape = False
    return length


def _entity_string_end(text, start):
    """The index after a string quoted with &quot; that starts at start (a backslash escapes the next character or &quot;), or the end
    of the text."""
    length, i, size = len(text), start + len(_ENTITY_QUOTE), len(_ENTITY_QUOTE)
    while i < length:
        if text[i] == "\\":
            i += 1 + (size if text.startswith(_ENTITY_QUOTE, i + 1) else 1)
        elif text.startswith(_ENTITY_QUOTE, i):
            return i + size
        else:
            i += 1
    return length


def _json_value_end(text, start, list_header=False, quote='"'):
    """The end of the value of a quoted credential name (the JSON quoted-name ruling).

    (a) A quoted string ends at its closing quote; under a name quoted with &quot; or with an escaped quote (\\"), a value that opens
    with the same spelling is a string that it closes. (b) A number, true, false or null ends there only when optional space and then
    ',', '}', ']' or the end of the text follow it. (c) Anything else (an object or array included) is prose: the value runs to its
    true end, as for name=value, which for a list header (cookie, set-cookie, authorization, proxy-authorization) is the line end."""
    length = len(text)
    if start >= length:
        return start
    if quote == _ENTITY_QUOTE and text.startswith(_ENTITY_QUOTE, start):
        return _entity_string_end(text, start)
    if quote == _ESCAPED_QUOTE and text.startswith(_ESCAPED_QUOTE, start):
        return _escaped_string_end(text, start)
    first = text[start]
    if first in "\"'":
        return _json_string_end(text, start)
    # The scalar is read only over characters a number or literal can hold, so the probe never runs past this pair's value; Java's
    # scalar runs to the next of ',', '}', ']', space, tab, CR or LF, so anything else after the run makes it no JSON scalar.
    end = start
    while end < length and text[end] in _JSON_SCALAR_CHARS:
        end += 1
    if (end >= length or text[end] in ",}] \t\r\n") and _json_member_boundary(text, end):
        scalar = text[start:end]
        if scalar in ("true", "false", "null") or _JSON_NUMBER.match(scalar):
            return end
    if list_header:
        while start < length and text[start] not in "\n\r":
            start += 1
        return start
    return _pair_value_end(text, start)



class _PathBudget(_Budget):
    """A name of more than limits.max_depth components: the whole exported value becomes the placeholder (fail closed)."""


class _Span(tuple):
    """A span (start, end) the catalog replaced, by position. omission is true for an omission the catalog wrote (a data URI or a
    media value), false for a placeholder. The kind is recorded when the span is written, never read back from its text."""

    def __new__(cls, start, end, omission=False):
        span = tuple.__new__(cls, (start, end))
        span.omission = omission
        return span


class _Rewrite:
    """An output text built from kept and replaced pieces, recording by position every span the catalog replaced (each placeholder
    and each data URI omission it writes) and whether it is an omission."""

    def __init__(self, reference):
        self.reference, self.parts, self.spans, self.length = reference, [], [], 0

    def keep(self, text):
        self.parts.append(text)
        self.length += len(text)

    def put(self, text, omission=False):
        self.spans.append(_Span(self.length, self.length + len(text), omission))
        self.keep(text)

    def region(self, text, runs=()):
        """Write a part of a URL (the path, the fragment, a decoded query value): each run (start, end) of runs, sorted and disjoint
        (an empty run inserts the placeholder), becomes the placeholder, and each data URI in the original text its omission. Data
        URIs are read in the original text, so a placeholder never cuts one short: where a run and a data URI overlap, the run's
        placeholder is written and the data URI's omission covers the rest of it. One pass over runs and data URIs."""
        data = []
        for start, end, _ in _scan_text(_DATA, text):
            replacement = self.reference.data_uri(text[start:end])
            if replacement is not None:
                data.append((start, end, replacement))
        placeholder, position, length, r, d, last = self.reference.redacted, 0, len(text), 0, 0, None
        while True:
            while r < len(runs) and runs[r][1] <= position and not runs[r][0] == runs[r][1] == position:
                r += 1
            if r < len(runs) and runs[r][0] == runs[r][1] == position:
                self.put(placeholder)
                last, r = ("run", r), r + 1
                continue
            while d < len(data) and data[d][1] <= position:
                d += 1
            if position >= length:
                break
            if r < len(runs) and runs[r][0] <= position:
                if last != ("run", r):
                    self.put(placeholder)
                    last = ("run", r)
                position = runs[r][1]
            elif d < len(data) and data[d][0] <= position:
                end = min(data[d][1], runs[r][0]) if r < len(runs) else data[d][1]
                if last != ("data", d):
                    self.put(data[d][2], omission=True)
                    last = ("data", d)
                position = end
            else:
                end = min([length] + ([runs[r][0]] if r < len(runs) else []) + ([data[d][0]] if d < len(data) else []))
                self.keep(text[position:end])
                position, last = end, None

    def result(self):
        return "".join(self.parts), self.spans


def _replace_runs(value, runs, spans, replacement=_REDACTED):
    """Replace each (start, end) of runs (sorted, disjoint) with the placeholder. A span a run overlaps is dropped; the others move."""
    if not runs:
        return value, spans
    out, moved, copied, shift, k = [], [], 0, 0, 0
    for start, end in runs:
        while k < len(spans) and spans[k][1] <= start:
            moved.append(_Span(spans[k][0] + shift, spans[k][1] + shift, getattr(spans[k], "omission", False)))
            k += 1
        while k < len(spans) and spans[k][0] < end:
            k += 1
        out.append(value[copied:start])
        out.append(replacement)
        copied = end
        shift += len(replacement) - (end - start)
    moved.extend(_Span(span[0] + shift, span[1] + shift, getattr(span, "omission", False)) for span in spans[k:])
    out.append(value[copied:])
    return "".join(out), moved


def _past_path_limits(path: str) -> bool:
    """A path whose names are not checked: longer than PATH_LIMIT characters, or of PATH_PARTS or more
    separators."""
    return len(path) > PATH_LIMIT or path.count(".") + path.count("[") >= PATH_PARTS


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
        # Java's Pattern semantics, as the shared reference reads the exclusions: '.' matches no line terminator.
        self.java_exclusions = tuple(
            (re.compile(regex.pattern.replace(".", "[^\\n\\r\\u0085\\u2028\\u2029]")), guards)
            for regex, guards in self.exclusions)
        self.words = frozenset(credentials["vocabulary"]["words"])
        self.contextual = tuple(re.compile(item["path_regex"]) for item in credentials["contextual_exclusions"])
        self.shapes = tuple(self._shape(item) for item in credentials["value_shapes"])
        # The value shapes as read at a --name's value start: the value after an encoded gap is read as after a plain one,
        # where the shape's leading word boundary holds.
        self.word_shapes = tuple(re.compile(item["regex"][2:] if item["regex"].startswith("\\b") else item["regex"],
                                            re.I if "i" in item["flags"] else 0) for item in credentials["value_shapes"])
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
        self._parts: Dict[str, Tuple[bool, bool]] = {}

    def _shape(self, item: Mapping[str, Any]) -> Tuple[Any, Any]:
        """One value shape, and what replaces a match of it."""
        if item["regex"] == _JWT_SHAPE and not item["flags"]:
            return _JwtShape(), self.redacted
        return re.compile(item["regex"], re.I if "i" in item["flags"] else 0), self.redacted

    # Names -------------------------------------------------------------------------------------

    def normalize(self, name: str) -> str:
        return name.translate(self._fold)

    def components(self, path: str) -> List[str]:
        return [part for part in _PARTS.split(path) if part and not part.isdecimal()]

    def leaf(self, path: str) -> str:
        parts = self.components(path)
        return self.normalize(parts[-1]) if parts else ""

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

    def _component(self, part: str) -> Tuple[bool, bool]:
        """(excluded, credential-like) for one path component, kept, as components repeat."""
        known = self._parts.get(part)
        if known is None:
            folded = self.normalize(part)
            excluded = self.java_excluded(folded)
            known = (excluded, not excluded and (any(pattern in folded for pattern in self.patterns)
                                                 or self.vocabulary(part)))
            if len(part) <= 256:
                if len(self._parts) >= 8192:
                    self._parts.clear()
                self._parts[part] = known
        return known

    def _credential_name(self, name: str) -> bool:
        # A name of more than max_depth components is not read: the whole exported value becomes the placeholder.
        self.path_budget(name)
        parts = self.components(name)
        excluded = False
        # An exclusion covers its own component only, never another credential component. The vocabulary words count as
        # the whole name or a segment of a component.
        for index, part in enumerate(parts):
            part_excluded, credential = self._component(part)
            if part_excluded or (self.contextual and any(
                    regex.fullmatch(".".join(self.normalize(item) for item in parts[:index + 1]))
                    for regex in self.contextual)):
                excluded = True
            elif credential:
                return True
        if excluded:
            return False
        folded = self.normalize(name)
        return not self.java_excluded(folded) and (any(pattern in folded for pattern in self.patterns)
                                                   or self.vocabulary(name))

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
            full_name = path + "." + name if path else name
            if type(item) is str or (type(item) is list and any(type(value) is str for value in item)):
                # The hint reads the leaf of the full name through the budgeted path parser.
                self.path_budget(full_name)
            found = self.hint(name, item)
            if found[0] or found[1]:
                parent = self.hint_parent(full_name)
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
        return _substitute(_DATA, value, lambda original, _name, _start: self.data_uri(original) or original)

    # URLs and text -----------------------------------------------------------------------------

    def userinfo_span(self, base: str) -> Optional[Tuple[int, int]]:
        """Where ``user:password`` of ``http(s)://user:password@`` starts, and its ``@``, read in one pass; or None."""
        scheme = _SCHEME.match(base)
        if scheme is None:
            return None
        at = base.find("@", scheme.end())
        if at < 0:
            return None
        info = base[scheme.end():at]
        if "/" in info or ":" not in info[1:-1] or any(char.isspace() for char in info):
            return None
        return scheme.end(), at

    def url_value(self, original: str) -> str:
        """One already-framed URL; its query values are never delimited as prose."""
        return self.framed_url(original)[0]

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

    def text(self, value: str, secrets: Tuple[str, ...], framed: bool = False, structured: bool = False) -> str:
        """URLs and data URIs in text, then known secrets (longest first), then credential shapes, then the pair rule.

        ``structured`` marks the re-dumped JSON of a structured string: each of its leaf strings went through the pair
        rule on its own, so the rule does not read it again.
        """
        def token(original: str, group: str, start: int) -> str:
            if group == "data":
                return self.data_text(original)
            # Keep enclosing single quotes in prose; apostrophes inside a URL are URL characters.
            closing_quote = ""
            if start and value[start - 1] == "'" and original.endswith("'"):
                original, closing_quote = original[:-1], "'"
            return self.url_value(original) + closing_quote

        if not (framed and self.is_url_value(value)):
            value = _substitute(_TEXT, value, token)
            for secret in secrets:
                if secret in value:
                    value = self.redacted.join(part.replace(secret, self.redacted) for part in value.split(self.redacted))
            for shape, replacement in self.shapes:
                value = shape.sub(replacement, value)
            return value if structured else self.credential_pairs(value)
        # A whole URL value at a framed site: the catalog pass records by position each span it replaced; known secrets
        # and value shapes move those spans (a span one of them overlaps is gone), and the pair rule then runs over the
        # URL text, skipping only the spans still recorded. The skip never looks at placeholder text.
        value, spans = self.framed_url(value)
        for secret in secrets:
            if secret not in value:
                continue
            runs, offset = [], 0
            for part in value.split(self.redacted):
                found = part.find(secret)
                while found >= 0:
                    runs.append((offset + found, offset + found + len(secret)))
                    found = part.find(secret, found + len(secret))
                offset += len(part) + len(self.redacted)
            value, spans = _replace_runs(value, runs, spans, self.redacted)
        for shape, _ in self.shapes:
            value, spans = _replace_runs(value, _shape_spans(shape, value), spans, self.redacted)
        return self.credential_pairs(value, spans)

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

    # The credential pair rule ------------------------------------------------------------------

    def path_budget(self, name):
        """Java's PrivacyCatalog.path budget: a name of more than max_depth components (each non-empty run between '.', '[' and ']',
        numeric ones included) is not read."""
        if name.count(".") + name.count("[") + name.count("]") >= self.max_depth and\
                sum(1 for part in re.split(r"[.\[\]]+", name) if part) > self.max_depth:
            raise _PathBudget("privacy path budget")

    def vocabulary(self, name):
        """Whether a segment of the name is one of the catalog's vocabulary words: PrivacyCatalog.vocabulary, which reads UTF-16
        units. A segment is split at every unit that is not a letter or digit (a surrogate, half of a character outside the BMP,
        is neither) and at a lowercase unit followed by an uppercase one."""
        # A segment that is a word lowercases to it unit by unit, so the word is then a substring of the lowercased name (U+0130 is
        # the one non-ASCII unit java_lower maps to one of the words' letters). Without one, no unit loop is needed.
        lowered = name.replace("\u0130", "i").lower()
        if not any(word in lowered for word in self.words):
            return False
        units = name.encode("utf-16-le")
        units = [int.from_bytes(units[k:k + 2], "little") for k in range(0, len(units), 2)]
        word, previous = [], None
        for unit in units + [ord("-")]:
            c = chr(unit)
            letter = not 0xD800 <= unit <= 0xDFFF and _java_letter_or_digit(c)
            boundary = not letter or (previous is not None and c.isupper() and previous.islower())
            if boundary and word:
                if "".join(word) in self.words:
                    return True
                word = []
            if letter:
                word.append(_java_lower(c))
            previous = None if 0xD800 <= unit <= 0xDFFF else c
        return False

    def java_excluded(self, folded):
        """excluded() with Java's Pattern semantics: '.' matches no line terminator (LF, CR, U+0085, U+2028, U+2029)."""
        return any(regex.fullmatch(folded) and not any(marker in folded for marker in guards)
                   for regex, guards in self.java_exclusions)

    def pair_name(self, name):
        """A credential name for the pair rule, with the command-line and bracket spellings undone first: --password,
        -Ddb.password and user[password]."""
        if not name:
            return False
        stripped = name.lstrip("-")
        if stripped.startswith("D") and len(stripped) > 1 and name.startswith("-D"):
            stripped = stripped[1:]
        stripped = stripped.replace("[", ".").replace("]", ".")
        return bool(stripped) and (self.credential_name(stripped) or self.credential_name(name))

    def separator_pair(self, text, i, size, floor, credential):
        """The separator branch's decision for the separator (of size) at i, the name read back no further than floor: (pair, name,
        overflow). pair is true when the name is a credential name or undecided (a gap that is not readable, a name past its window,
        or a name that ends at a percent escape and stays undecided after one decode, I2)."""
        name_end = _gap_start(text, i, floor)
        readable = _readable_gap(text, name_end, i)
        name_start = name_end
        # The read-back stops at any %XX escape (the hex digits of %3D are name characters, but they are not part of a name).
        while (name_start > floor and name_end - name_start < _NAME_CAP and text[name_start - 1] in _PAIR_NAME_CHARS
               and not _escape_before(text, name_start, floor)):
            name_start -= 1
        # The window is full and the name runs on before it: the name is not read, the pair is replaced.
        overflow = (name_end - name_start >= _NAME_CAP and name_start > floor and text[name_start - 1] in _PAIR_NAME_CHARS
                    and not _escape_before(text, name_start, floor))
        if overflow:
            # The window alone may already hold more than max_depth components (the whole value is then the placeholder).
            self.path_budget(text[name_start:name_end])
        # A name that ends at a percent escape is decoded once (I2): undecided when a %XX remains after the one decode or the read
        # found nothing.
        escaped = None if overflow else _escaped_name(text, name_start, name_end, floor)
        name = text[name_start:name_end] if escaped is None else escaped[0]
        # A gap that is not readable leaves the name undecided: the pair is replaced (fail closed).
        pair = ((name_end - name_start >= 2 and (overflow or not readable or credential(text[name_start:name_end])))
                or (escaped is not None and (escaped[1] or not readable or credential(escaped[0]))))
        return pair, name, overflow

    def flag_ahead(self, text, start, credential):
        """Whether another flag starts at start, so that a --name before it has no value word (phase 1e r2): a "--", or a single-dash
        flag with a full credential name of two or more characters, then plain whitespace (a line break included) and more text.
        Anything else (an encoded gap, the end of the text) leaves the single-dash word the --name's value (fail closed)."""
        dashes = _flag_dashes(text, start)
        if dashes != 1:
            return dashes == 2
        end = start + 1
        while end < len(text) and text[end] in _PAIR_NAME_CHARS and end - start <= _NAME_CAP:
            end += 1
        if end - start < 3 or end - start > _NAME_CAP or end >= len(text) or not text[end].isspace():
            return False
        after = end
        while after < len(text) and text[after].isspace():
            after += 1
        return after < len(text) and credential(text[start:end])

    def line_starts_pair(self, text, start, credential):
        """STOP 1: whether the line read fresh at start begins with a pair, by any rule: a name (bare, dashed, bracketed or with %XX
        escapes other than encoded whitespace, or quoted with a quote, an apostrophe, an escaped quote or &quot;), a gap (horizontal or
        encoded whitespace) and a separator; or another flag (flag_ahead). Bounded by the name windows; read once for the one --name before the line."""
        length = len(text)
        k = -1
        for quote in (_ENTITY_QUOTE, _ESCAPED_QUOTE, '"', "'"):
            if text.startswith(quote, start):
                close = text.find(quote, start + len(quote), min(length, start + len(quote) + 130))
                if close < 0:
                    return False
                k = close + len(quote)
                break
        if k < 0:
            if self.flag_ahead(text, start, credential):
                return True
            name = start
            while name < length and text[name] == "-" and name - start < 2:
                name += 1
            k = name
            while k < length and k - name < _NAME_CAP:
                if text[k] in _PAIR_NAME_CHARS:
                    k += 1
                elif (k + 3 <= length and _escape_before(text, k + 3, k) and text[k + 1:k + 3] not in _ENCODED_EQUALS
                      and not _encoded_space_ahead(text, k)):
                    k += 3
                else:
                    break
            if k == name:
                return False
        while k < length and (_dashes_space(text[k]) or _encoded_space_ahead(text, k)):
            k += _encoded_space_ahead(text, k) or 1
        return k < length and bool(_separator_at(text, k))

    def credential_pairs(self, text, skip=()):
        """Replace the value of every credential-named pair with the placeholder, in one left-to-right pass linear in the text.

        name=value / name: value: the value runs to its true end (pair_value_end); for the list headers cookie, set-cookie,
        authorization and proxy-authorization it is the rest of the line. "name": value follows the JSON quoted-name ruling
        (json_value_end) and becomes a quoted placeholder, the name quoted with a quote, an apostrophe, an escaped quote (\\"name\\")
        or &quot;. --name value takes the next word (a quoted word to its closing quote). The user information of
        scheme://user:password@host is replaced. A placeholder already in the value is no exception.

        A name read within a bounded window (NAME_CAP characters back from '=' or ':', 130 for a quoted name, 128 after --) that runs
        on past the window is not decided: the pair is replaced (fail closed).

        skip holds the spans (start, end) the catalog itself replaced in a whole URL value, recorded by position during the catalog
        pass: a pair whose value starts inside one is left as the catalog wrote it, and the read goes on after the span. An omission
        span (Span.omission: a data URI or media omission the catalog wrote) shields nothing: a credential pair's value that starts
        in one is replaced to its true end.

        The separator of name=value is '=', ':' or %3D (%3d). The gap between a name and its separator is read back whole; one that
        is not readable (readable_gap) leaves the name undecided, and its pair is replaced."""
        length, out, copied, known, i = len(text), [], 0, {}, 0
        # The ':' that follows a --name and its gap: the pair's separator, never the start of "://" user information.
        dashes_colon = -1
        redacted = self.redacted
        skip = [span for span in skip if not getattr(span, "omission", False)]
        starts = [span[0] for span in skip]

        def credential(name):
            if name not in known:
                known[name] = self.pair_name(name)
            return known[name]

        def skipped(position):
            k = bisect.bisect_right(starts, position) - 1
            return skip[k][1] if k >= 0 and position < skip[k][1] else -1

        def quoted_pair(quoted):
            """Whether the quoted name of json_name's result is a credential or undecided pair, and the name: a name past its window is
            not read (undecided); a name that ends at a percent escape is decoded once (I2), undecided when a %XX remains or it
            holds nothing."""
            opening, quote, close, overflow = quoted
            name = "" if opening < 0 else text[opening + len(quote):close]
            plain = len(name)
            while plain > 0 and name[plain - 1] in _PAIR_NAME_CHARS and not _escape_before(name, plain, 0):
                plain -= 1
            escaped = _escaped_name(name, plain, len(name), 0) if opening >= 0 else None
            return overflow or credential(name) or (escaped is not None and (escaped[1] or credential(escaped[0]))), name

        def equals_value_end(k, size, name):
            """The value end of a quoted name with '=' or %3D at k (phase 1e r2): up to 8 spaces after the separator, then the true end
            of the value (pair_value_end), or the line end for a list header name."""
            value = k + size
            while value < length and value - k - size < 8 and text[value] == " ":
                value += 1
            if _normalize(name) in _PAIR_LIST_HEADERS:
                while value < length and text[value] not in "\n\r":
                    value += 1
                return value
            return _pair_value_end(text, value)

        def decide(k, size, floor, read_end=True):
            """Phase 1e, I1: a separator inside or at the end of consumed text (read back no further than floor) is handed to the
            branch that reads it at k: the quoted-name branch for a ':' after a quoted name, or for an '=' or %3D after a quoted name
            that is a credential or undecided pair (phase 1e r2), else the separator branch. The end of that pair's value when it
            is a credential or undecided pair (k itself when read_end is false), else -1."""
            quoted = _json_name(text, k)
            if quoted is not None and quoted[0] >= floor:
                pair, name = quoted_pair(quoted)
                if size == 1 and text[k] == ":":
                    if not pair:
                        return -1
                    if not read_end:
                        return k
                    value = k + 1
                    while value < length and _java_space(text[value]):
                        value += 1
                    return _json_value_end(text, value, _normalize(name) in _PAIR_LIST_HEADERS, quoted[1])
                if pair and _QUOTED_EQUALS:
                    return equals_value_end(k, size, name) if read_end else k
            if not self.separator_pair(text, k, size, floor, credential)[0]:
                return -1
            return _separator_value_end(text, k, size) if read_end else k

        def quoted_equals(k):
            """Whether the '=' or %3D at k follows a quoted name that is a credential or undecided pair (phase 1e r2)."""
            quoted = _json_name(text, k) if k > 0 and _QUOTED_EQUALS else None
            return quoted is not None and quoted_pair(quoted)[0]

        def word_end(begin, value):
            """The end of a --name's value word that starts at value; the replacement starts at begin (value, or an encoded control in
            the gap before it, I5). The word ends at any str.isspace character (I1); a quoted word runs to its closing quote and on to
            the word end; a value shape that starts at value is the value whole (I3). Each separator in the word or at its end is
            handed to the branch that reads it; for a credential or undecided pair the replacement runs on to that pair's value end
            (the first and the last such separator; each read is linear)."""
            value_end = word = value
            if text[value] in "\"'":
                value_end = value + 1
                while value_end < length and text[value_end] != text[value]:
                    value_end += 2 if text[value_end] == "\\" else 1
                value_end = word = min(length, value_end + 1)
            shape_end = value
            for shape in self.word_shapes:
                match = shape.match(text, value)
                if match:
                    shape_end = max(shape_end, match.end())
            first = last = -1
            while value_end < length and (value_end < shape_end or not _word_space(text[value_end])):
                size = _separator_at(text, value_end) if value_end >= word else 0
                if size and decide(value_end, size, begin, read_end=False) >= 0:
                    first = value_end if first < 0 else first
                    last = value_end
                value_end += 1
            # A separator after the word and a gap on its line (plain or encoded whitespace) is read too: the word is that pair's name
            # (an unreadable gap leaves it undecided, 1c and 1d), so its value is never left behind the replaced word.
            ahead = value_end
            while ahead < length and (_dashes_space(text[ahead]) or _encoded_space_ahead(text, ahead)):
                ahead += _encoded_space_ahead(text, ahead) or 1
            if ahead > value_end and ahead < length and _separator_at(text, ahead) and decide(ahead, _separator_at(text, ahead), begin,
                                                                                             read_end=False) >= 0:
                first = ahead if first < 0 else first
                last = ahead
            for k in sorted({first, last} - {-1}):
                value_end = max(value_end, decide(k, _separator_at(text, k), begin))
            return _consumed_end(text, begin, value_end, decide)

        while i < length:
            c = text[i]
            start, stop, replacement, userinfo = -1, -1, redacted, False
            quoted = _json_name(text, i) if c == ":" and i > 0 else None
            if quoted is not None:
                opening, quote, close, overflow = quoted
                # A name past its window is not read; a name before an unreadable gap is read (the list-header rule still applies)
                # but undecided: its value is replaced either way.
                # A quoted name that ends at a percent escape is decoded once (I2); undecided when a %XX remains or it holds nothing.
                pair, name = quoted_pair(quoted)
                if pair:
                    value = i + 1
                    while value < length and _java_space(text[value]):
                        value += 1
                    if skip and skipped(value) >= 0:
                        i = max(skipped(value), i + 1)
                        continue
                    header = _normalize(name) in _PAIR_LIST_HEADERS
                    end = _json_value_end(text, value, header, quote)
                    if not header:
                        end = _consumed_end(text, value, end, decide)
                    if end > value:
                        start, stop, replacement = value, end, quote + redacted + quote
                if start < 0:
                    i += 1
                    continue
            elif c == ":" and text.startswith("//", i + 1) and i != dashes_colon:
                limit, end = min(length, i + 3 + 512), i + 3
                while end < limit and text[end] not in "/?# \t\r\n":
                    end += 1
                at = text.rfind("@", i + 3, end)
                if at < i + 3:
                    # A password may hold '/', '?' or '#': a user:password form before a later '@' in the same word runs to it.
                    word = i + 3
                    while word < length and word - i < 1024 and not _java_space(text[word]):
                        word += 1
                    later = text.rfind("@", i + 3, word)
                    colon = text.find(":", i + 3, later) if later > i + 3 else -1
                    if later > i + 3 and colon > i + 3:
                        at = later
                if at < i + 3:
                    i += 3
                    continue
                start, stop, userinfo = i + 3, at, True
            elif (c == "=" or (c == "%" and text[i + 1:i + 3] in _ENCODED_EQUALS)) and quoted_equals(i):
                # Phase 1e r2: a quoted name with '=' or %3D ("password"=TAIL, 'password'%3DTAIL, \\"password\\"=TAIL,
                # &quot;password&quot;=TAIL) that is a credential or undecided pair: the value runs to its true end.
                size = 1 if c == "=" else 3
                value = i + size
                while value < length and value - i - size < 8 and text[value] == " ":
                    value += 1
                if skip and skipped(value) >= 0:
                    i = max(skipped(value), i + 1)
                    continue
                name = quoted_pair(_json_name(text, i))[1]
                start = value
                stop = equals_value_end(i, size, name)
                if _normalize(name) not in _PAIR_LIST_HEADERS:
                    stop = _consumed_end(text, value, stop, decide)
            elif c in "=:" or (c == "%" and text[i + 1:i + 3] in _ENCODED_EQUALS):
                # The separator: '=', ':', or '=' percent-encoded (%3D or %3d). The gap before it is read back whole.
                size = 1 if c in "=:" else 3
                pair, name, overflow = self.separator_pair(text, i, size, copied, credential)
                if pair:
                    value = i + size
                    while value < length and value - i - size < 8 and text[value] == " ":
                        value += 1
                    if skip and skipped(value) >= 0:
                        # A span the catalog replaced: never read its value end (that read would run on over the rest of the URL).
                        i = max(skipped(value), i + 1)
                        continue
                    start = value
                    if not overflow and _normalize(name) in _PAIR_LIST_HEADERS:
                        stop = value
                        while stop < length and text[stop] not in "\n\r":
                            stop += 1
                    else:
                        # The value runs to its true end; a separator at the end of it is read too (I1, general invariant).
                        stop = _consumed_end(text, value, _pair_value_end(text, value), decide)
            elif c == "-" and (i == 0 or text[i - 1].isspace() or _encoded_space_width(text, i, 0)) and _flag_dashes(text, i):
                # A --name, or (phase 1e r2) a single-dash flag with a full credential name: -password, never -p or a cluster -abc
                # that is no credential name.
                dashes = _flag_dashes(text, i)
                end = i + dashes
                while end < length and text[end] in _PAIR_NAME_CHARS and end - i < _NAME_CAP + dashes:
                    end += 1
                overflow = end - i >= _NAME_CAP + dashes and end < length and text[end] in _PAIR_NAME_CHARS
                while overflow and end < length and text[end] in _PAIR_NAME_CHARS:
                    end += 1
                if overflow:
                    self.path_budget(text[i:end])
                # The gap (phase 1e): horizontal whitespace (str.isspace less the line breaks, NBSP included, I1) and encoded whitespace
                # (I3), in any order. A line break after the gap is read by STOP 1. An encoded control in the gap starts the value (I5).
                if (end < length and end - i - dashes >= (1 if dashes == 2 else 2)
                        and (_dashes_space(text[end]) or text[end] in _LINE_BREAKS or _encoded_space_ahead(text, end))
                        and (overflow or credential(text[i:end]))):
                    value, control, gap_end, spaced = end, -1, end, False
                    while value < length:
                        if _dashes_space(text[value]):
                            value += 1
                            spaced = spaced or control >= 0
                            continue
                        width = _encoded_space_ahead(text, value)
                        if not width:
                            break
                        if control < 0 and _encoded_control(text, value, width):
                            control = value
                        value += width
                        gap_end = value
                    if _dashes_separator(text, value):
                        # A separator after the gap: the pair is name<gap>=value, read at the separator (its gap rule included).
                        dashes_colon = _dashes_colon_at(text, value)
                        i = value
                        continue
                    # An encoded control in the gap starts the value: it is replaced from the control to the end of the value word
                    # (I5); with no value word, the gap from the control to its last encoded character.
                    begin, value_end = (control if control >= 0 else value), -1
                    if value < length and text[value] in _LINE_BREAKS:
                        # STOP 1: after ONE line break the next line is read fresh. A pair that starts it is read by its own branch; else
                        # its first word is the value. A blank line in between (two or more line breaks) ends the --name.
                        line = value + (2 if text.startswith("\r\n", value) else 1)
                        # The next line's leading gap: horizontal and encoded whitespace, as after the --name (an encoded control in it
                        # starts the value, I5). A line that holds encoded whitespace and nothing else is no blank line: the read goes on
                        # over its line break to the next line (fail closed).
                        encoded = False
                        while line < length:
                            if _dashes_space(text[line]):
                                line += 1
                                continue
                            if encoded and text[line] in _LINE_BREAKS:
                                line += 2 if text.startswith("\r\n", line) else 1
                                encoded = False
                                continue
                            width = _encoded_space_ahead(text, line)
                            if not width:
                                break
                            if control < 0 and _encoded_control(text, line, width):
                                control = line
                            line, encoded = line + width, True
                        if line >= length or text[line] in _LINE_BREAKS or _dashes_separator(text, line):
                            # A blank line or the end: no value. A separator that starts the line is read at the separator, its name
                            # and the line break before it read back as an unreadable gap (fail closed), as before the ruling.
                            if line >= length or text[line] in _LINE_BREAKS:
                                i = end
                            else:
                                dashes_colon, i = _dashes_colon_at(text, line), line
                            continue
                        if self.line_starts_pair(text, line, credential):
                            if control >= 0:
                                start, stop = control, gap_end
                            else:
                                i = line
                                continue
                        else:
                            begin = control if control >= 0 else line
                            value_end = word_end(begin, line)
                    elif control >= 0 and not spaced and value < length:
                        # An encoded control right before the word starts it: the word runs from the control to its end, a "--" in
                        # it included (I5, as in the draft).
                        value_end = word_end(begin, value)
                    elif value >= length or self.flag_ahead(text, value, credential):
                        # No word, or the next word is another flag (a --name, or a single-dash credential flag and a gap).
                        if control >= 0:
                            start, stop = control, gap_end
                    else:
                        value_end = word_end(begin, value)
                    if value_end > begin:
                        start, stop = begin, value_end
                if start < 0:
                    i = end
                    continue
            if start >= 0 and skip:
                span_end = skipped(start)
                # User information that runs on past the catalog's own (to a later '@') is replaced whole, the span with it.
                if span_end >= 0 and not (userinfo and stop > span_end):
                    i = max(span_end, i + 1)
                    continue
            if start >= copied and stop >= start:
                out.append(text[copied:start])
                out.append(replacement)
                copied = stop
                i = max(stop, i + 1)
                continue
            i += 1
        if not out:
            return text
        out.append(text[copied:])
        return "".join(out)

    def after_question(self, text, marks, end):
        """Whether a name after a '?' is a credential name: each name text[q + 1:end] for a '?' q in marks (the '?' marks before end,
        after the previous '=' or the name start) is read, each within NAME_CAP characters. A name longer than NAME_CAP, or one
        that holds more than QUESTION_CAP '?' marks, is not decided: True (fail closed). At most QUESTION_CAP * NAME_CAP characters
        are read for one '=', all of them between the previous '=' and this one."""
        if not marks:
            return False
        if end - marks[0] - 1 > _NAME_CAP or len(marks) > _QUESTION_CAP:
            return True
        return any(self.query_name(unquote_plus(text[q + 1:end])) for q in marks)

    def question_name(self, raw_name):
        """Whether a raw query name, or the part of it after any '?' (a pair after a second '?'), is a credential name. The whole name
        is read whole; a part after a '?' is read as after_question reads it."""
        if self.query_name(unquote_plus(raw_name)):
            return True
        marks, at = [], raw_name.find("?")
        while at >= 0 and len(marks) <= _QUESTION_CAP:
            marks.append(at)
            at = raw_name.find("?", at + 1)
        return self.after_question(raw_name, marks, len(raw_name))

    def tail_pairs(self, segment, out=None):
        """Catalog rule for the part of a URL after its '#': split at '&' like a query; a credential-named pair's value (the name
        read whole or after a '?') is replaced to the next '&'. In any other pair, see second_question. With out (a Rewrite), the
        fragment is written there (data URIs read in the original fragment, see Rewrite.region); else its text is returned."""
        runs, offset = [], 0
        for piece in segment.split("&"):
            raw_name, equals, raw_value = piece.partition("=")
            value, end = offset + len(raw_name) + len(equals), offset + len(piece)
            if equals and self.question_name(raw_name):
                runs.append((value, end))
            elif equals:
                cut = self.second_question_cut(raw_value)
                if cut >= 0:
                    runs.append((value + cut, end))
            offset = end + 1
        rewrite = out if out is not None else _Rewrite(self)
        rewrite.region(segment, runs)
        return None if out is not None else rewrite.result()[0]

    def second_question_cut(self, raw_value):
        """Where second_question cuts the value (after the credential pair's '='), or -1. One forward pass: the '?' marks since the
        last '=' are kept (at most QUESTION_CAP + 1 of them), and each '=' decides its names once (after_question), reading only
        characters between the previous '=' and itself, so each character is read a bounded number of times."""
        i = raw_value.find("?")
        if i < 0:
            return -1
        marks, length = [], len(raw_value)
        while i < length:
            c = raw_value[i]
            if c == "?":
                if len(marks) <= _QUESTION_CAP:
                    marks.append(i)
            elif c == "=":
                if marks and self.after_question(raw_value, marks, i):
                    return i + 1
                marks = []
            i += 1
        return -1

    def path_pairs(self, base, out):
        """The URL path pair rule. Each path segment is read whole, every '=' and every %3D (%3d) in it: a pair's name runs back to
        the previous ';', ',', '&', '=' or %3D of the segment (the first separator also reads the name back to the segment start),
        percent-decoded once. A credential name's value is replaced to the next pair in the segment (a ';', ',' or '&' that a name
        and '=' or %3D follow) or the segment end ('/', or the end of the path, which '?' and '#' end); the read goes on there. The
        scheme and authority of an absolute URL are not path segments. The text goes to out with its data URIs (Rewrite.region)."""
        path = 0
        if base[:7].lower() == "http://" or base[:8].lower() == "https://":
            path = base.find("/", base.index("//") + 2)
            if path < 0:
                path = len(base)
        runs, position = [], path
        while position < len(base):
            end = base.find("/", position + 1)
            end = len(base) if end < 0 else end
            # base[position] is the '/' that opens the segment (or the first character of a relative reference that has none).
            first = position + 1 if base.startswith("/", position) else position
            k, name, decided = first, first, False
            while k < end:
                c = base[k]
                if c in _PATH_PAIR_SEPARATORS:
                    k += 1
                    name = k
                    continue
                size = 1 if c == "=" else 3 if c == "%" and base[k + 1:k + 3] in _ENCODED_EQUALS else 0
                if not size:
                    k += 1
                    continue
                credential = k > name and self.credential_name(unquote(base[name:k]))
                if not decided and not credential and k > first:
                    credential = self.credential_name(unquote(base[first:k]))
                decided = True
                if credential:
                    value_end = self.path_value_end(base, k + size, end)
                    runs.append((k + size, value_end))
                    k = name = value_end
                else:
                    k = name = k + size
            position = end
        out.region(base, runs)

    @staticmethod
    def path_value_end(base, start, end):
        """The end of a path pair's value in base[start:end] (one segment): the next ';', ',' or '&' that a non-empty name and then
        '=' or %3D follow, or the segment end. Each look stops at the next separator, so each character is read at most twice."""
        k = start
        while k < end:
            if base[k] not in _PATH_PAIR_SEPARATORS:
                k += 1
                continue
            j = k + 1
            while j < end and base[j] not in _PATH_PAIR_SEPARATORS and base[j] != "=" and not (
                    base[j] == "%" and base[j + 1:j + 3] in _ENCODED_EQUALS):
                j += 1
            if j < end and j > k + 1 and base[j] not in _PATH_PAIR_SEPARATORS:
                return k
            k = j
        return end

    def framed_url(self, original):
        """Scrub one already-framed URL. Returns the text and the spans (start, end) the catalog itself replaced in it, by position."""
        out = _Rewrite(self)
        target, hashmark, fragment = original.partition("#")
        base, question, raw_query = target.partition("?")
        info = self.userinfo_span(base)
        if info is not None:
            out.keep(base[:info[0]])
            out.put(self.redacted)
            out.keep("@")
            base = base[info[1] + 1:]
        self.path_pairs(base, out)
        out.keep(question)
        parsed = []
        for pair in raw_query.split("&"):
            raw_name, equals, raw_value = pair.partition("=")
            parsed.append((raw_name, equals, raw_value, unquote_plus(raw_name), unquote_plus(raw_value)))
        # Queries are flat objects, not unrelated scalars. Index every hint
        # before scrubbing any value, retaining duplicates and original bytes.
        hints = self.index_hints((name, decoded) for _, equals, _, name, decoded in parsed if equals)
        for index, (raw_name, equals, raw_value, name, decoded) in enumerate(parsed):
            if index:
                out.keep("&")
            out.keep(raw_name + equals)
            if not equals:
                continue
            # The checks of a value read its name as a path; past the path limits it is not read, so the value is not sent.
            if self.query_name(name) or self.question_name(raw_name) or _past_path_limits(name):
                out.put(self.redacted)
                continue
            replacement = self.media_string(decoded, name, hints=hints)
            if replacement is not None:
                # The whole value is media: its omission covers a pair after a second '?' in it too.
                out.put(replacement, omission=True)
                continue
            cut = self.second_question_cut(raw_value)
            if cut >= 0:
                # A credential-named pair after a second '?' goes to the value's end.
                out.keep(raw_value[:cut])
                out.put(self.redacted)
            elif self.data_text(decoded, framed=True) != decoded:
                # Do not recurse through text()/URL decoding: a query
                # adds one serialization layer, not an unbounded scan
                # of nested percent-encoded URLs. The decoded value is
                # written with each data URI replaced; only those are spans.
                out.region(decoded)
            else:
                out.keep(raw_value)
        # A credential-named pair after the '#' is replaced as a query pair is; so is one after a second '?' (above).
        out.keep(hashmark)
        if hashmark:
            self.tail_pairs(fragment, out)
        return out.result()

    def map_key(self, name, secrets):
        """A map key goes through the export rules of a string value at a framed site that need no context: a whole data URI or
        magic-byte media value, then the URL catalog and data URIs, known secrets, value shapes and the pair rule."""
        replacement = self.media_string(name, "", framed=True)
        return replacement if replacement is not None else self.text(name, secrets, framed=True)

    # The walk ----------------------------------------------------------------------------------

    def scrub(self, value: Any, path: str = "", siblings: Optional[Mapping[str, Any]] = None,
              secrets: Tuple[str, ...] = (), _media: bool = False, _audio: bool = False, _depth: int = 0,
              _budget: Optional[List[int]] = None, _hints: Optional[Dict[str, Tuple[bool, bool, bool]]] = None,
              framed: bool = True, _cut: bool = False, _root: bool = True) -> Any:
        """One plain-data value through the catalog, with ``path`` as its structural key.

        ``secrets`` is ordered longest first. ``_cut`` marks a value a value length limit may have
        cut: each text in it then also goes through cut_text. A name (the path, a map key, a pair
        name in text) of more than max_depth components makes the whole value ``<redacted>``. Raises
        _Budget past the catalog's other limits, and TypeError for a value that is not plain data;
        nothing is ever turned into text.
        """
        if _root:
            try:
                return self.scrub(value, path, siblings, secrets, _media, _audio, _depth, _budget, _hints, framed, _cut,
                                  _root=False)
            except _PathBudget:
                return self.redacted
        budget = _budget if _budget is not None else [self.max_nodes, self.max_text]
        budget[0] -= 1
        # The name path budget first: a path of more than max_depth components exports the whole value as the placeholder.
        self.path_budget(path)
        if _depth > self.max_depth or budget[0] < 0:
            raise _Budget("privacy structure budget")
        if _past_path_limits(path):
            raise _Budget("privacy path budget")
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
            out: Dict[str, Any] = {}
            for name, item in value.items():
                clean = self.scrub(item, path + "." + name if path else name, None, secrets, _media, _audio,
                                   _depth + 1, budget, hints, _cut=_cut, _root=False)
                key = self.map_key(name, secrets)
                # Two keys that are one after scrubbing: one key is kept, in the first one's place, and its value
                # is the placeholder.
                out[key] = self.redacted if key in out else clean
            return out
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
                               _cut=_cut, _root=False)
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
        structured = False
        if value.lstrip().startswith(("{", "[")):
            parsed, duplicated = _parse_json(value)
            if parsed is _UNPARSED:
                # Cut JSON or a Python repr: the media in its strings. Its credential members are read by
                # the pair rule, as quoted names, with the rest of the text.
                value = self.quoted_media(value)
            else:
                structured = True
                clean = self.scrub(parsed, "", None, secrets, media, audio, _depth + 1, budget, _root=False)
                # A duplicate member's discarded value was never checked: render the checked copy.
                if duplicated or clean != parsed:
                    value = json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
        value = self.text(value, secrets, framed=framed, structured=structured)
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
            try:
                credential = catalog.credential_name(name)
            except _PathBudget:
                # A name of more than max_depth components is not read: its value is registered (fail closed).
                credential = True
            if credential:
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


def _warn(cause: Optional[str] = None) -> None:
    """The one warning for content that could not be checked; ``cause`` names a catalog that did not load."""
    global _warned
    if not _warned:
        _warned = True
        if cause:
            logger.warning("traceAI: %s, so spans reach Future AGI with only their ids, timing and status code",
                           cause)
        else:
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
        try:
            hints: Optional[Dict[str, Tuple[bool, bool, bool]]] = catalog.index_hints(native.items())
        except _PathBudget:
            # A hint name of more than max_depth components: every value read with these siblings is
            # <redacted> whole.
            hints = None
        out: Dict[str, Any] = {}
        for key, item in native.items():
            try:
                budget = self.budget()
                name = catalog.scrub(key, "", None, self.secrets, _budget=budget)
                was_cut = key in cut
                if session_id is not None and key == SESSION_ID and item == session_id:
                    clean = catalog.scrub(item, "", None, self.secrets, _budget=budget, _cut=was_cut)
                elif hints is None:
                    clean = catalog.redacted
                else:
                    clean = catalog.scrub(item, key, None, self.secrets, _depth=1, _budget=budget, _hints=hints,
                                          _cut=was_cut)
            except Exception as error:
                logger.debug("traceAI: an attribute could not be checked for Future AGI and was not sent (%s)",
                             type(error).__name__)
                continue
            clean = _attribute(clean)
            if clean is not _DROP and type(name) is str:
                # Two keys that are one after scrubbing: one key is kept, in the first one's place, and its
                # value is the placeholder.
                out[name] = catalog.redacted if name in out else clean
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
        # A catalog file that is missing or unreadable, for example in a zipped or frozen app that
        # did not collect the package's data files, is named in the one warning.
        _warn(_load_error)
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
