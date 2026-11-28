"""Generate privacy.json and privacy-fixtures.json (stdlib only).

Edit the catalog and cases here, never the generated JSON. --check is strictly read-only.
The reference below is an executable recognition contract, not an SDK exporter:
S1 detached snapshots/hide flags and S5 registry lifecycle remain SDK duties.
"""

import argparse
import base64
import binascii
import bisect
import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, unquote_plus, urlsplit

SPEC = Path(__file__).resolve().parent
OUTPUT = SPEC / "privacy-fixtures.json"
TOKEN_TIMING_NAMES = (
    "time_to_first_token", "time_to_first_tokens", "time_per_output_token", "time_per_token",
    "inter_token_latency", "token_latency", "tokens_per_second", "output_tokens_per_second",
    "ms_per_token", "seconds_per_token",
)
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
JSON_SCALAR_CHARS = frozenset("0123456789+-.eEtrufalsn")
JSON_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")

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
PAIR_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-[]")
PAIR_NAME_START = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
PAIR_PADDING_FOLLOWERS = frozenset("= \t\n\x0b\x0c\r,;)]}\"'")
PAIR_LIST_HEADERS = frozenset(("cookie", "setcookie", "authorization", "proxyauthorization"))
# A name, wherever its length is bounded (a pair rule name, a quoted name, a --name, a name after a second '?'), is read through its
# last NAME_CAP characters; when the name runs on past that window the name is not decided and the pair is replaced (fail closed).
NAME_CAP = 128
# A name after a '?' that holds more '?' marks than this is not decided either (fail closed): each '=' then reads at most
# QUESTION_CAP names of at most NAME_CAP characters, so the second '?' read stays linear with a small constant.
QUESTION_CAP = 8
# The longest readable gap between a pair name and its separator: plain spaces only.
GAP_CAP = 8
# The quote spellings of a quoted name: a quote or apostrophe, an escaped quote inside a JSON string, an HTML entity quote.
ESCAPED_QUOTE, ENTITY_QUOTE = '\\"', "&quot;"
# '=' percent-encoded, after its '%': the pair rule and the URL path pair rule read it as a separator.
ENCODED_EQUALS = frozenset(("3D", "3d"))
# The separators between pairs inside one URL path segment.
PATH_PAIR_SEPARATORS = frozenset(";,&")
# Whitespace spelled as a percent escape (any hex case) and the JSON string escapes of whitespace: an encoded gap between a name and
# its separator is as unreadable as the whitespace it stands for.
ENCODED_SPACES = frozenset(("20", "09", "0A", "0B", "0C", "0D"))
ENCODED_NBSP = "%C2%A0"
JSON_SPACE_ESCAPES = frozenset("tnrfb")
HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
# A line terminator that Java's '$' (no MULTILINE) accepts before the end of the text.
JAVA_FINAL_TERMINATORS = ("\r\n", "\n", "\r", "\u0085", "\u2028", "\u2029")
# Phase 1e (I1): a line break is never part of a --name gap; it ends the --name on its line, and the next line is read fresh.
LINE_BREAKS = frozenset("\n\r\u0085\u2028\u2029\x0c\x0b")
# Phase 1e r2: a quoted name takes '=' and %3D as well as ':' (the Java comparison switches this off to attribute a difference).
QUOTED_EQUALS = True


def java_space(c):
    if c in "\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f":
        return True
    if c in "\u00a0\u2007\u202f":
        return False
    return unicodedata.category(c) in ("Zs", "Zl", "Zp")


def java_letter_or_digit(c):
    return unicodedata.category(c) in ("Lu", "Ll", "Lt", "Lm", "Lo", "Nd")


def java_lower(c):
    # Java's Character.toLowerCase is the simple one-to-one mapping; Python's lower() is the full mapping (U+0130 becomes two).
    lowered = c.lower()
    return lowered if len(lowered) == 1 else ("i" if c == "\u0130" else c)


def next_pair(text, start, spaced):
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
    if i - dashes > 2 or i >= length or text[i] not in PAIR_NAME_START:
        return False
    name = i
    i += 1
    while i < length and text[i] in PAIR_NAME_CHARS and i - name < 128:
        i += 1
    if i < length and text[i] in PAIR_NAME_CHARS:
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
    return not (rest >= length or text[rest] in PAIR_PADDING_FOLLOWERS
                or (length - rest <= 2 and text[rest:] in JAVA_FINAL_TERMINATORS))


def pair_value_end(text, start):
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
            if c in ",;\n\r" and next_pair(text, i + 1, True):
                return i
            if c == " " and next_pair(text, i + 1, False):
                return i
        i += 1
    return length


def encoded_space_width(text, end, floor):
    """The width of the encoded whitespace that ends at end, read back no further than floor, or 0: a percent escape (%20, %09,
    %0A, %0B, %0C, %0D or %C2%A0, any hex case) or a JSON string escape (\\t, \\n, \\r, \\f, \\b, or \\u and four hex digits of a
    whitespace character). A backslash that an earlier backslash escapes starts no escape. The only look back is over the backslashes
    right before the escape, which no other look reads."""
    if end - 3 >= floor and text[end - 3] == "%" and text[end - 2:end].upper() in ENCODED_SPACES:
        return 3
    if end - 6 >= floor and text[end - 6:end].upper() == ENCODED_NBSP:
        return 6
    width = 0
    if end - 2 >= floor and text[end - 2] == "\\" and text[end - 1] in JSON_SPACE_ESCAPES:
        width = 2
    elif end - 6 >= floor and text[end - 6:end - 4] == "\\u" and all(c in HEX_DIGITS for c in text[end - 4:end]) \
            and chr(int(text[end - 4:end], 16)).isspace():
        width = 6
    if not width:
        return 0
    slashes = 1
    while end - width - slashes >= floor and text[end - width - slashes] == "\\":
        slashes += 1
    return width if slashes % 2 else 0


def gap_start(text, end, floor):
    """The start of the gap that ends at end, read back no further than floor: whitespace (str.isspace, any length) and encoded
    whitespace (encoded_space_width), in any order. Each gap is read by the one separator after it, so the reads stay linear in
    the text."""
    while end > floor:
        if text[end - 1].isspace():
            end -= 1
            continue
        width = encoded_space_width(text, end, floor)
        if not width:
            break
        end -= width
    return end


def escape_before(text, end, floor):
    """Whether a percent escape (a '%' and two hex digits) ends at end: the read-back of a name stops there."""
    return end - 3 >= floor and text[end - 3] == "%" and text[end - 2] in HEX_DIGITS and text[end - 1] in HEX_DIGITS


PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")


def escaped_name(text, name_start, name_end, floor):
    """Phase 1e, I2: the name before a separator that ends at a percent escape, read back over name characters and %XX escapes (an
    encoded separator, %3D or %3d, ends it) and percent-decoded ONCE: (decoded, undecided), or None when the name does not end at an
    escape. name_start is where the plain read-back (which stops at any escape, 1d item 1) stopped. The name ends at an escape when a
    %XX other than %3D / %3d is right before name_end, or when the plain read-back stopped at a %25 escape (double encoding: "%255D"). The pair is undecided
    when a %XX remains after the one decode, when the read found no letter, digit or underscore, or when the name runs on past
    NAME_CAP characters."""
    if not ((escape_before(text, name_end, floor) and text[name_end - 2:name_end] not in ENCODED_EQUALS) or
            (name_start > floor and escape_before(text, name_start, floor) and text[name_start - 2:name_start] == "25")):
        # No name ends here (an encoded separator right before this separator, as in %3D%3D, leaves no name at all).
        return None
    start = name_end
    while start > floor and name_end - start < NAME_CAP:
        if escape_before(text, start, floor):
            if text[start - 2:start] in ENCODED_EQUALS:
                break
            start -= 3
        elif text[start - 1] in PAIR_NAME_CHARS:
            start -= 1
        else:
            break
    overflow = name_end - start >= NAME_CAP and start > floor and (text[start - 1] in PAIR_NAME_CHARS or escape_before(text, start, floor))
    decoded = unquote(text[start:name_end])
    undecided = overflow or bool(PERCENT_ESCAPE.search(decoded)) or not any(ch.isalnum() or ch == "_" for ch in decoded)
    return decoded, undecided


DATA_LINE = re.compile(r"[\r\n]+([A-Za-z0-9+/_=\-]+)")
BASE64_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/_-")


BASE64_INDEX = {c: k for k, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")}
BASE64_INDEX.update({"-": 62, "_": 63})


def padding(body, pad, base64, padded, count):
    """Whether pad '=' characters after body are trailing base64 padding (phase 1e, I4): the URI is ;base64, no padding came before,
    one or two '=', the base64 characters read so far, body and the padding are a multiple of four, the last character before the
    padding is a canonical one (its unused low bits are zero) and the line is no single-dash flag ("-api_key=")."""
    if not base64 or padded or pad > 2 or not body or body.startswith("-") or (count + len(body) + pad) % 4:
        return False
    return BASE64_INDEX[body[-1]] % (4 if pad == 1 else 16) == 0


def data_uri_end(text, start, end):
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
    count, padded = sum(1 for ch in decoded if ch in BASE64_ALPHABET), "=" in decoded
    position = head
    while position < end:
        line = DATA_LINE.match(text, position, end)
        if line is None:
            break
        after, gap = line.end(), line.end()
        while gap < len(text) and (dashes_space(text[gap]) or encoded_space_ahead(text, gap)):
            gap += encoded_space_ahead(text, gap) or 1
        if gap < len(text) and (text[gap] in "=:" or text[gap:gap + 3] in ("%3D", "%3d")):
            break   # a name, a gap and a separator: a pair, never base64
        body = line.group(1).rstrip("=")
        pad = len(line.group(1)) - len(body)
        if body.startswith("--") or "=" in body or (pad and not padding(body, pad, base64, padded, count)):
            break   # a --name line, an '=' inside the line, or an '=' that is not trailing base64 padding
        count, padded, position = count + len(body), padded or bool(pad), after
    return position


def scan_text(pattern, text):
    """The matches of pattern (DATA, or TEXT with its data group) in text, left to right and not overlapping, as re.sub and
    re.finditer read them, with each data URI cut by data_uri_end (I4): (start, end, group name)."""
    position = 0
    while position <= len(text):
        match = pattern.search(text, position)
        if match is None:
            return
        name = match.lastgroup if pattern.groupindex else "data"
        end = data_uri_end(text, match.start(), match.end()) if name == "data" else match.end()
        yield match.start(), end, name
        position = end if end > match.start() else match.start() + 1


def substitute(pattern, text, replace):
    """re.sub over scan_text: replace(original, group name) gives each match's text."""
    out, copied = [], 0
    for start, end, name in scan_text(pattern, text):
        out.append(text[copied:start])
        out.append(replace(text[start:end], name, start))
        copied = end
    out.append(text[copied:])
    return "".join(out)


def dashes_space(c):
    """Whether c belongs to the gap after a --name (phase 1e, I1): any str.isspace character that is not a line break, NBSP included.
    A line break ends the --name on its line."""
    return c.isspace() and c not in LINE_BREAKS


def word_space(c):
    """Whether c ends a --name's value word (phase 1e, I1): any str.isspace character. A small function so that the Java comparison
    can switch the item off (the agent ends the word at Character.isWhitespace, java_space)."""
    return c.isspace()


def encoded_control(text, start, width):
    """Whether the encoded whitespace of width at start (encoded_space_ahead) stands for a control character (phase 1e, I5): %09,
    %0A, %0B, %0C and %0D in any hex case, the JSON escapes \\t \\n \\r \\f \\b, and a \\u escape of a control. %20, %C2%A0 and a \\u
    escape of a space or separator are encoded spaces, not controls."""
    if text[start] == "%":
        return width == 3 and text[start + 1:start + 3] != "20"
    if width == 2:
        return True
    return unicodedata.category(chr(int(text[start + 2:start + 6], 16))) == "Cc"


def separator_at(text, k):
    """The width of the pair separator at k ('=', ':' that does not open "://", or %3D / %3d), or 0 (phase 1e, I1)."""
    c = text[k]
    if c == "=" or (c == ":" and not text.startswith("//", k + 1)):
        return 1
    return 3 if c == "%" and text[k + 1:k + 3] in ENCODED_EQUALS else 0


def separator_value_end(text, k, size):
    """The true end of the value of the pair whose separator (of size) is at k, as the separator branch reads it: up to 8 spaces after
    the separator, then pair_value_end, or the line end after a list header name (read back over name characters)."""
    value = k + size
    while value < len(text) and value - k - size < 8 and text[value] == " ":
        value += 1
    name = k
    while name > 0 and k - name < NAME_CAP and text[name - 1] in PAIR_NAME_CHARS:
        name -= 1
    if normalize(text[name:k]) in PAIR_LIST_HEADERS:
        while value < len(text) and text[value] not in "\n\r":
            value += 1
        return value
    return pair_value_end(text, value)


def consumed_end(text, start, stop, decide):
    """Phase 1e, I1 (general invariant): no rule leaves an '=', ':' or %3D unread at the end of the text it consumed. While
    text[start:stop] ends at a separator that is a credential or undecided pair (decide(k, size, floor) gives that pair's value end,
    or -1), the replacement runs on to that pair's value end. Each step moves forward."""
    while stop > start:
        if text[stop - 1] in "=:" and separator_at(text, stop - 1):
            size = 1
        elif stop - 3 >= start and separator_at(text, stop - 3) == 3:
            size = 3
        else:
            return stop
        end = decide(stop - size, size, start)
        if end <= stop:
            return stop
        stop = end
    return stop


def dashes_colon_at(text, value):
    """The index of the ':' that ends a --name's gap and starts its value, or -1: that ':' is the pair's separator, never the start of
    "://" user information."""
    return value if text[value] == ":" else -1


def encoded_space_ahead(text, start):
    """The width of the encoded whitespace (percent escape or JSON string escape, as encoded_space_width) that starts at start, or 0."""
    if text.startswith("%", start):
        if text[start + 1:start + 3].upper() in ENCODED_SPACES:
            return 3
        return 6 if text[start:start + 6].upper() == ENCODED_NBSP else 0
    if text.startswith("\\", start):
        if text[start + 1:start + 2] in JSON_SPACE_ESCAPES and text[start + 1:start + 2]:
            return 2
        digits = text[start + 2:start + 6]
        if text[start + 1:start + 2] == "u" and len(digits) == 4 and all(c in HEX_DIGITS for c in digits) and chr(int(digits, 16)).isspace():
            return 6
    return 0


def dashes_separator(text, start):
    """Whether the word after a --name and its gap starts with a separator ('=', ':' or %3D): the pair is then read at the separator,
    as name<gap>=value with the gap rule, not by the --name rule."""
    return start < len(text) and (text[start] in "=:" or (text[start] == "%" and text[start + 1:start + 3] in ENCODED_EQUALS))


def flag_dashes(text, i):
    """The dashes of a flag that starts at i (phase 1e r2): 2 for "--", 1 for a single dash followed by a name start (a letter or an
    underscore), else 0. Whether a single-dash flag is a pair is decided by its name: a full credential name of at least two
    characters, never a short flag."""
    if text.startswith("--", i):
        return 2
    return 1 if text.startswith("-", i) and text[i + 1:i + 2] in PAIR_NAME_START and text[i + 1:i + 2] else 0


def readable_gap(text, start, end):
    """Whether the gap text[start:end] between a name and its separator is readable: 0 to GAP_CAP plain spaces (U+0020). Any other
    whitespace, encoded whitespace included, or more spaces, leaves the name undecided (its value is replaced)."""
    return end - start <= GAP_CAP and all(text[k] == " " for k in range(start, end))


def quote_before(text, end):
    """The quote spelling that ends at end: the HTML entity &quot;, an escaped quote (a quote after an odd run of backslashes, read
    back at most NAME_CAP characters), a quote or an apostrophe; or None."""
    if end >= len(ENTITY_QUOTE) and text.startswith(ENTITY_QUOTE, end - len(ENTITY_QUOTE)):
        return ENTITY_QUOTE
    if end < 1 or text[end - 1] not in "\"'":
        return None
    if text[end - 1] == '"':
        slashes = 0
        while end - 2 - slashes >= 0 and text[end - 2 - slashes] == "\\" and slashes < NAME_CAP:
            slashes += 1
        if slashes % 2:
            return ESCAPED_QUOTE
    return text[end - 1]


def json_name(text, colon):
    """A quoted name that ends, after a whitespace gap, before the colon: (opening, quote, close, overflow), or None.

    quote is the spelling that closes the name at close (see quote_before); the name is text[opening + len(quote):close]. The opening
    quote is looked for only within the 130 characters before the closing one, never back to the start of the text. When that window
    ends inside the text without one, the name is longer than the cap and is not read: overflow is true and opening is -1. A gap
    that is not readable (readable_gap: a tab or any other whitespace but U+0020, or more than GAP_CAP spaces) leaves the name
    undecided: overflow is true too, and the value is replaced."""
    end = gap_start(text, colon, 0)
    quote = quote_before(text, end)
    if quote is None or (len(quote) == 1 and end < 3):
        return None
    close = end - len(quote)
    floor = max(0, close - 130)
    opening = text.rfind(quote, floor, close)
    if opening >= 0:
        return opening, quote, close, not readable_gap(text, end, colon)
    return (-1, quote, close, True) if floor > 0 else None


def json_string_end(text, start):
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


def json_member_boundary(text, end):
    """Whether only optional space and then ',', '}', ']' or the end of the text follow end."""
    while end < len(text) and java_space(text[end]):
        end += 1
    return end >= len(text) or text[end] in ",}]"


def escaped_string_end(text, start):
    """The index after an escaped JSON string inside a JSON string (\\"...\\") that starts at start, or the end of the text. The text is
    read one escape level down: \\\\ is a backslash of the inner string, which escapes the inner character after it, and the first \\"
    that no inner backslash escapes closes the string."""
    length, i, inner_escape = len(text), start + len(ESCAPED_QUOTE), False
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


def entity_string_end(text, start):
    """The index after a string quoted with &quot; that starts at start (a backslash escapes the next character or &quot;), or the end
    of the text."""
    length, i, size = len(text), start + len(ENTITY_QUOTE), len(ENTITY_QUOTE)
    while i < length:
        if text[i] == "\\":
            i += 1 + (size if text.startswith(ENTITY_QUOTE, i + 1) else 1)
        elif text.startswith(ENTITY_QUOTE, i):
            return i + size
        else:
            i += 1
    return length


def json_value_end(text, start, list_header=False, quote='"'):
    """The end of the value of a quoted credential name (the JSON quoted-name ruling).

    (a) A quoted string ends at its closing quote; under a name quoted with &quot; or with an escaped quote (\\"), a value that opens
    with the same spelling is a string that it closes. (b) A number, true, false or null ends there only when optional space and then
    ',', '}', ']' or the end of the text follow it. (c) Anything else (an object or array included) is prose: the value runs to its
    true end, as for name=value, which for a list header (cookie, set-cookie, authorization, proxy-authorization) is the line end."""
    length = len(text)
    if start >= length:
        return start
    if quote == ENTITY_QUOTE and text.startswith(ENTITY_QUOTE, start):
        return entity_string_end(text, start)
    if quote == ESCAPED_QUOTE and text.startswith(ESCAPED_QUOTE, start):
        return escaped_string_end(text, start)
    first = text[start]
    if first in "\"'":
        return json_string_end(text, start)
    # The scalar is read only over characters a number or literal can hold, so the probe never runs past this pair's value; Java's
    # scalar runs to the next of ',', '}', ']', space, tab, CR or LF, so anything else after the run makes it no JSON scalar.
    end = start
    while end < length and text[end] in JSON_SCALAR_CHARS:
        end += 1
    if (end >= length or text[end] in ",}] \t\r\n") and json_member_boundary(text, end):
        scalar = text[start:end]
        if scalar in ("true", "false", "null") or JSON_NUMBER.match(scalar):
            return end
    if list_header:
        while start < length and text[start] not in "\n\r":
            start += 1
        return start
    return pair_value_end(text, start)


def privacy_catalog():
    """Canonical catalog source; both privacy JSON files are generated below."""
    return {'version': 4,
     'scalar_metrics': {'version': 6,
                        'canonical_decimal': {'ruling': 'A declared canonical-decimal counter keeps a full decimal count string with '
                                                        'a safe-integer value, unless the string has base64 shape.',
                                              'regex': '^(0|[1-9][0-9]{0,15})$',
                                              'max_chars': 16,
                                              'base64_min_chars': 4,
                                              'keys': ['gen_ai.audio.bytes'],
                                              'scope': 'Only declared canonical-decimal counters consult this rule. '
                                                       'gen_ai.audio.bytes keeps full canonical decimal strings with a safe-integer '
                                                       'numeric value unless they have base64 shape at or above base64_min_chars. '
                                                       'elevenlabs.audio.bytes never grants a string role. The four-character shape '
                                                       'threshold is not a special case for one count. Preserve accepted string '
                                                       'types; no signs, leading zeros, fractions, exponents, whitespace or final '
                                                       'newline. Other G4 core/detail keys retain their original scalar roles, '
                                                       'including arbitrary strings and literal undefined. This rule does not change '
                                                       'Q8b NON_CONTENT types.'},
                        'ruling': 'Only exact declared usage and metric keys get a scalar metric role, and media, credential and '
                                  'secret signals always take precedence over it.',
                        'scope': 'Closed exact complete keys in the declared usage/metrics containers only. Case, punctuation, '
                                 'prefixes, suffixes and array indices are not aliases. A nested object path must equal the complete '
                                 'key. gen_ai.audio and elevenlabs.audio are metric containers only for their listed bytes counters, '
                                 'not for arbitrary audio members. Their nonnegative_safe_integer value_type grants a role to finite '
                                 'nonnegative safe-integer numbers; only gen_ai.audio.bytes additionally accepts strings under '
                                 'canonical_decimal. gen_ai.usage.cache_read_tokens.audio also accepts only nonnegative safe-integer '
                                 'numbers, never strings. All other inputs on these typed counters retain the role-free media '
                                 'policy. Other declared metrics keep their original number/boolean/string roles without decimal '
                                 'validation.',
                        'precedence': 'Scalar role only removes structural media classification caused by the metric spelling. '
                                      'Explicit media/type hints on any ancestor, inherited media/audio, established media payload '
                                      'ancestry, credential ancestry and known numeric/string secrets take precedence. On the two '
                                      'typed byte counters only, base64-shaped strings at or above '
                                      'canonical_decimal.base64_min_chars cannot obtain a role, even when numerically safe. Magic '
                                      'bytes, data URIs and other independent value checks still apply. A counter leaf under '
                                      'established media payload ancestry cannot use the usage metadata boundary to escape omission. '
                                      'Private payload-role ancestry survives parsed, stringified and diagnostic JSON handoffs; '
                                      'resetting a JSON root never grants a metric role. Metric-looking descendants reached through '
                                      'this retained payload origin remain payload, including numeric/bool scalars and numeric/bool '
                                      'arrays; ordinary metadata text boundaries are unchanged.',
                        'arrays': 'Arrays never receive the scalar metric role. Nonempty numeric/bool media payload arrays are '
                                  'omitted; existing string/object array walking and empty-array behavior remain unchanged.',
                        'containers': [{'path': 'gen_ai.usage',
                                        'keys': ['gen_ai.usage.input_tokens',
                                                 'gen_ai.usage.output_tokens',
                                                 'gen_ai.usage.total_tokens',
                                                 'gen_ai.usage.provider_total_tokens',
                                                 'gen_ai.usage.cache_read_tokens',
                                                 'gen_ai.usage.input_tokens.audio',
                                                 'gen_ai.usage.output_tokens.audio',
                                                 'gen_ai.usage.output_tokens.reasoning']},
                                       {'path': 'gen_ai.usage',
                                        'keys': ['gen_ai.usage.cache_read_tokens.audio'],
                                        'value_type': 'nonnegative_safe_integer'},
                                       {'path': 'gen_ai.audio',
                                        'keys': ['gen_ai.audio.bytes'],
                                        'value_type': 'nonnegative_safe_integer'},
                                       {'path': 'elevenlabs.audio',
                                        'keys': ['elevenlabs.audio.bytes'],
                                        'value_type': 'nonnegative_safe_integer'},
                                       {'path': 'llm.token_count',
                                        'keys': ['llm.token_count.prompt', 'llm.token_count.completion', 'llm.token_count.total']}],
                        'zero_detail_deferral': {'scope': 'Only these exact keys with numeric value zero may minimally yield to '
                                                          'canonical output and its earlier output groups. Nonzero, string-zero, '
                                                          'unknown usage, core totals and identity remain protected. Deferred '
                                                          'counters are metadata, not dropped content.',
                                                 'keys': ['gen_ai.usage.cache_read_tokens',
                                                          'gen_ai.usage.input_tokens.audio',
                                                          'gen_ai.usage.output_tokens.audio',
                                                          'gen_ai.usage.output_tokens.reasoning']}},
     'normalization': {'case': 'ascii_lowercase',
                       'remove': ['-', '_', '.'],
                       'paths': 'Split dotted and bracket-indexed paths into components; ignore numeric indices. Test every '
                                'component, retaining credential ancestry. Also test the folded full name unless a component is '
                                'explicitly excluded. An excluded metric component never exempts a different credential component.'},
     'credentials': {'replacement': '<redacted>',
                     'source_ownership': {'version': 1,
                                          'registered_matches': 'Match original text before independent owners or syntax rewrites. '
                                                                'Preserve minimum eight characters, longest-first matching and '
                                                                'replacement-sentinel segments. A crossing match consumes the '
                                                                'affected complete JSON/string owner and overlapping prose bytes; '
                                                                'never restore bare-media scanning over reconstructed URL/JSON '
                                                                'syntax.',
                                          'json_reuse': 'Never reuse raw duplicate members merely because surviving parsed values '
                                                        'compare equal. Nonnumeric discarded values force sanitized rendering. '
                                                        'Retained numeric/sentinel duplicates require every original numeric '
                                                        'occurrence to pass its full path, MIME, payload and source-lexeme policy; '
                                                        'fixed sentinels are checked for registered substrings. Preserve unrelated '
                                                        'original numeric lexemes in every structural edit. Raw escaped registered '
                                                        'strings are redacted at their source owner, not by injecting unescaped '
                                                        'markers into JSON.'},
                     'patterns': [{'id': 'token', 'contains': 'token', 'example': 'x-vault-token'},
                                  {'id': 'secret', 'contains': 'secret', 'example': 'client_secret'},
                                  {'id': 'key', 'contains': 'key', 'example': 'Ocp-Apim-Subscription-Key'},
                                  {'id': 'auth', 'contains': 'auth', 'example': 'Proxy-Authorization'},
                                  {'id': 'password', 'contains': 'password', 'example': 'database_password'},
                                  {'id': 'passwd', 'contains': 'passwd', 'example': 'db_passwd'},
                                  {'id': 'credential', 'contains': 'credential', 'example': 'provider_credentials'},
                                  {'id': 'signature', 'contains': 'signature', 'example': 'request_signature'},
                                  {'id': 'sig', 'contains': 'sig', 'example': 'sig'},
                                  {'id': 'session', 'contains': 'session', 'example': 'session_id'},
                                  {'id': 'cookie', 'contains': 'cookie', 'example': 'Set-Cookie'}],
                     'vocabulary': {'words': ['pwd', 'pass', 'passphrase', 'passwd', 'passcode', 'pin'],
                                    'match': 'Besides the patterns, a name is a credential name when one of these words is the whole '
                                             'name or one of its segments, at every site: query, fragment and URL path pairs, JSON '
                                             'and map keys, attribute keys and the credential pair rule. A segment is a maximal run '
                                             'of Unicode letters and decimal digits (categories Lu, Ll, Lt, Lm, Lo and Nd); every '
                                             "other character delimits it (for example '-', '_', '.', '/', '[', ']', ':', '?', '=', "
                                             "'&', ';', ',', '%' and space), and so does the change from a lowercase letter to an "
                                             'uppercase one (dbPass, db.pwd, user-pin, X-Pin). Words compare in lowercase. As for '
                                             'the patterns, the rule is tested per path component and then on the whole name, and an '
                                             'excluded component is not tested. Over-redaction (a name such as pin) fails closed.'},
                     'exclusions': [{'id': 'tokens',
                                     'regex': '^.*token(s(details)?|details)$',
                                     'unless_contains_patterns': ['secret',
                                                                  'key',
                                                                  'auth',
                                                                  'password',
                                                                  'passwd',
                                                                  'credential',
                                                                  'signature',
                                                                  'sig',
                                                                  'session',
                                                                  'cookie'],
                                     'examples': ['tokens',
                                                  'max_tokens',
                                                  'max_completion_tokens',
                                                  'max_output_tokens',
                                                  'input_tokens',
                                                  'output_tokens',
                                                  'total_tokens',
                                                  'prompt_tokens',
                                                  'completion_tokens',
                                                  'cache_read_tokens',
                                                  'cache_write_tokens',
                                                  'cached_tokens',
                                                  'cache_read_input_tokens',
                                                  'cache_creation_input_tokens',
                                                  'reasoning_tokens',
                                                  'num_tokens',
                                                  'max_new_tokens',
                                                  'prompt_tokens_details',
                                                  'completion_tokens_details',
                                                  'input_tokens_details',
                                                  'output_tokens_details',
                                                  'prompt_token_details',
                                                  'completion_token_details',
                                                  'input_token_details',
                                                  'output_token_details'],
                                     'reason': 'Token limits, usage and singular/plural token-detail objects and their counters are '
                                               'not authentication tokens. Independent credential markers still win; this is not a '
                                               'blanket subtree exemption.'},
                                    {'id': 'token-count',
                                     'regex': '^.*token ?counts?$',
                                     'unless_contains_patterns': ['secret',
                                                                  'key',
                                                                  'auth',
                                                                  'password',
                                                                  'passwd',
                                                                  'credential',
                                                                  'signature',
                                                                  'sig',
                                                                  'session',
                                                                  'cookie'],
                                     'examples': ['token_count',
                                                  'token_counts',
                                                  'token count',
                                                  'token counts',
                                                  'promptTokenCount',
                                                  'candidatesTokenCount',
                                                  'totalTokenCount',
                                                  'cachedContentTokenCount',
                                                  'thoughtsTokenCount',
                                                  'inputTokenCount',
                                                  'outputTokenCount'],
                                     'reason': 'Includes camelCase provider usage metrics.'},
                                    {'id': 'token-timing',
                                     'regex': '^.*(timetofirsttokens?|timeper(output)?token|intertokenlatency|tokenlatency|tokenspersecond|mspertoken|secondspertoken)$',
                                     'unless_contains_patterns': ['secret',
                                                                  'key',
                                                                  'auth',
                                                                  'password',
                                                                  'passwd',
                                                                  'credential',
                                                                  'signature',
                                                                  'sig',
                                                                  'session',
                                                                  'cookie'],
                                     'examples': ['time_to_first_token',
                                                  'time_to_first_tokens',
                                                  'time_per_output_token',
                                                  'time_per_token',
                                                  'inter_token_latency',
                                                  'token_latency',
                                                  'tokens_per_second',
                                                  'output_tokens_per_second',
                                                  'ms_per_token',
                                                  'seconds_per_token',
                                                  'gen_ai.response.time_to_first_token',
                                                  'gen_ai.server.time_to_first_token',
                                                  'gen_ai.server.time_per_output_token',
                                                  'gen_ai_response_time_to_first_token',
                                                  'gen_ai_server_time_per_output_token',
                                                  'TimeToFirstToken',
                                                  'Time-To-First-Token',
                                                  'GEN_AI.RESPONSE.TIME_TO_FIRST_TOKEN'],
                                     'reason': 'Token timing and throughput measurements are numeric metadata, not credentials. '
                                               'Independent credential markers still win, including in the same component; no '
                                               'blanket subtree exemption.'},
                                    {'id': 'token-metrics',
                                     'regex': '^(tokenusage|tokenlimit|tokenbudget|tokenlength|tokenspersecond|tokenrate|tokenizer|tokenization)$',
                                     'examples': ['token_usage',
                                                  'token_limit',
                                                  'token_budget',
                                                  'token_length',
                                                  'tokens_per_second',
                                                  'token_rate',
                                                  'tokenizer',
                                                  'tokenization'],
                                     'reason': 'Explicit non-secret usage and tokenizer metadata; not a blanket usage subtree '
                                               'exemption.'},
                                    {'id': 'key-identifiers',
                                     'regex': '^(keyid|apikeyid|publickeyid|privatekeyid|signingkeyid|xamzkeyid|xgoogkeyid)$',
                                     'examples': ['key_id',
                                                  'api_key_id',
                                                  'public_key_id',
                                                  'private_key_id',
                                                  'signing_key_id',
                                                  'x-amz-key-id',
                                                  'x-goog-key-id'],
                                     'reason': 'Identifiers, not the corresponding private key material. x-amz-credential remains '
                                               'secret. Access-key identifiers are masked for the manual Mastra privacy contract.'},
                                    {'id': 'key-metadata',
                                     'regex': '^(keytype|keyname|keyversion|keyalgorithm|keyboard|monkey|keywords)$',
                                     'examples': ['key_type',
                                                  'key_name',
                                                  'key_version',
                                                  'key_algorithm',
                                                  'keyboard',
                                                  'monkey',
                                                  'keywords'],
                                     'reason': 'Explicit metadata and ordinary words; never arbitrary *key* exemptions.'},
                                    {'id': 'signature-metadata',
                                     'regex': '^(signaturealgorithm|signatureversion|signaturemethod|signalgain|signalstrength|signal|signals|design|assignment|assigned)$',
                                     'examples': ['signature_algorithm',
                                                  'signature_version',
                                                  'signature_method',
                                                  'signal_gain',
                                                  'signal_strength',
                                                  'signal',
                                                  'signals',
                                                  'design',
                                                  'assignment',
                                                  'assigned'],
                                     'reason': 'Avoid the sig substring in ordinary words and signing-algorithm metadata.'},
                                    {'id': 'auth-metadata',
                                     'regex': '^(authtype|authscheme|authmethod|authenabled|authenticationmethod|authorizationtype|author|authors|authorname)$',
                                     'examples': ['auth_type',
                                                  'auth_scheme',
                                                  'auth_method',
                                                  'auth_enabled',
                                                  'authentication_method',
                                                  'authorization_type',
                                                  'author',
                                                  'authors',
                                                  'author_name'],
                                     'reason': 'Non-secret authentication descriptors and authorship.'},
                                    {'id': 'session-metrics',
                                     'regex': '^(sessioncount|sessionduration|sessiontimeout|sessionstart|sessionend)$',
                                     'examples': ['session_count',
                                                  'session_duration',
                                                  'session_timeout',
                                                  'session_start',
                                                  'session_end'],
                                     'reason': 'Session metrics, not session identifiers or session cookies.'},
                                    {'id': 'cookie-metadata',
                                     'regex': '^(cookiecount|cookieenabled|cookiesenabled)$',
                                     'examples': ['cookie_count', 'cookie_enabled', 'cookies_enabled'],
                                     'reason': 'Cookie configuration and counts, not Cookie/Set-Cookie values.'}],
                     'contextual_exclusions': [],
                     'seed_names': ['authorization',
                                    'authorization_token',
                                    'api_key',
                                    'x-api-key',
                                    'xi-api-key',
                                    'api_token',
                                    'token',
                                    'access_token',
                                    'refresh_token',
                                    'id_token',
                                    'secret',
                                    'secret_key',
                                    'client_secret',
                                    'password',
                                    'private_key',
                                    'x-goog-api-key',
                                    'anthropic-api-key',
                                    'Ocp-Apim-Subscription-Key',
                                    'x-secret-key',
                                    'Proxy-Authorization',
                                    'Cookie',
                                    'Set-Cookie',
                                    'passwd',
                                    'credential',
                                    'credentials',
                                    'auth_token',
                                    'x-amz-security-token',
                                    'x-auth-token',
                                    'x-access-token',
                                    'private-token',
                                    'x-vault-token',
                                    'key',
                                    'sig',
                                    'signature',
                                    'session'],
                     'sources': ['python/fi_instrumentation/auto/wire/_privacy.py',
                                 'typescript/packages/traceai_auto/src/wire/privacy.ts',
                                 'go/auto/internal/wire/privacy.go',
                                 'go/auto/privacy.go',
                                 'java/traceai-javaagent/src/main/java/ai/traceai/auto/WirePrivacy.java',
                                 'csharp/src/TraceAI.AutoInstrumentation/Wire/WirePrivacy.cs',
                                 'ruby/lib/traceai/auto/privacy.rb'],
                     'missed_name_sources': [{'names': ['x-goog-api-key', 'anthropic-api-key', 'ocp-apim-subscription-key']},
                                             {'names': ['authorization_token', 'client_secret', 'password']},
                                             {'names': ['apiKey', 'X-Amz-Signature']},
                                             {'names': ['api_key']},
                                             {'names': ['Authorization']},
                                             {'names': ['Authorization']},
                                             {'names': ['api_key', 'secret_key', 'X-Amz-Signature']},
                                             {'names': ['client_secret', 'Proxy-Authorization']},
                                             {'names': ['X-Auth-Token', 'passwd', 'token']},
                                             {'names': ['x-vault-token', 'database_password']},
                                             {'names': ['X-Session-Id', 'auth_token', 'x-vault-token']},
                                             {'names': ['key', 'passwd', 'Set-Cookie', 'private-token', 'x-vault-token']},
                                             {'names': ['x-access-token',
                                                        'private-token',
                                                        'x-vault-token',
                                                        'client_secret',
                                                        'password',
                                                        'refresh_token',
                                                        'api-key']}],
                     'value_shapes': [{'id': 'provider-api-key',
                                       'regex': '\\bsk-[A-Za-z0-9_-]{20,}',
                                       'flags': '',
                                       'reason': 'Bare long provider-key shape; applies to values, never an exemption.'},
                                      {'id': 'aws-access-key',
                                       'regex': '\\b(?:AKIA|ASIA)[A-Z0-9]{16}\\b',
                                       'flags': '',
                                       'reason': 'AWS access-key credential shape.'},
                                      {'id': 'jwt',
                                       'regex': '\\beyJ[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}',
                                       'flags': '',
                                       'reason': 'Three-segment JWT-shaped credential value.'},
                                      {'id': 'authorization-value',
                                       'regex': '\\b(?:Bearer|Basic)\\s+[A-Za-z0-9+/_=-]{8,}',
                                       'flags': 'i',
                                       'reason': 'Credential authorization scheme and value, including unnamed logprob values.'},
                                      {'id': 'private-key-pem',
                                       'regex': '-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\\s\\S]*?(?:-----END (?:RSA |EC '
                                                '|OPENSSH )?PRIVATE KEY-----|$)',
                                       'flags': '',
                                       'reason': 'Complete or truncated private-key PEM value.'}]},
     'signed_url': {'parameters': ['key',
                                   'api_key',
                                   'api-key',
                                   'apikey',
                                   'secret_key',
                                   'token',
                                   'access_token',
                                   'sig',
                                   'signature',
                                   'x-amz-signature',
                                   'x-amz-credential',
                                   'x-amz-security-token',
                                   'x-goog-signature',
                                   'x-goog-credential',
                                   'se',
                                   'sp',
                                   'sv',
                                   'sr',
                                   'skoid',
                                   'sktid'],
                    'also_match_credential_patterns': True,
                    'decode_parameter_names': 'Before treating a scalar/body/attribute/array-element value as one HTTP(S), /path or '
                                              '?query URL, validate its entire lexical boundary, not only its prefix. A message '
                                              'beginning with a URL can still be prose containing later URLs: scan every URL '
                                              'independently and preserve the prose. Recognized data-URI query values own literal '
                                              'quotes, whitespace, markup and backslashes; mask only those payloads for whole-URL '
                                              'lexical validation, then parse the original whole URL before delimiter-limited '
                                              'scanning. Never use credential or audio redaction to validate the boundary, since it '
                                              'can swallow prose or later URLs. Isolate each HTTP(S) URL or relative /path or ?query '
                                              'reference in text. Apostrophes are legal URI characters in names and values, not '
                                              'token delimiters; preserve enclosing single quotes in prose. Split off the fragment '
                                              'before splitting at the first question mark; later question marks are legal '
                                              'query-name/value characters. Split query pairs on ampersands, then the first equals '
                                              'sign. Percent-decode names once as UTF-8; plus means space. Use the same predicate as '
                                              'transport observation. Percent-decode values once and apply the shared structural '
                                              'media predicate, including data-URI recognition, not only base64 sniffing. Treat all '
                                              'decoded query pairs as one flat object for MIME/type siblings, including '
                                              'dotted/bracket-indexed names and duplicate hints (combine hints, never '
                                              'last-value-wins); keep exact indexed parent isolation. Frame query pairs before '
                                              'scanning literal data URIs so following parameters/fragments survive. Do not '
                                              'recursively decode nested URLs. Preserve original spelling of unchanged values, '
                                              'separators, ordering, duplicates and fragments; replace only sensitive values. '
                                              'Already-redacted values are stable.',
                    'userinfo': 'Replace HTTP(S) user:password@ with <redacted>@.'},
     'media': {'base64_alphabet': 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=_-\r\n',
               'base64_prefix_decoded_bytes': 32,
               'unrecognized_base64_min_chars_exclusive': 256,
               'unrecognized_base64_fields': ['data',
                                              'bytes',
                                              'b64json',
                                              'filedata',
                                              'audiobase64',
                                              'images',
                                              'image',
                                              'inputimage',
                                              'outputimage',
                                              'inlinedata'],
               'length_counts': 'ASCII characters in the original payload including CR/LF; data URI payload is percent-decoded once '
                                'before counting.',
               'replacement': '<omitted: {length} base64 chars>',
               'other_replacement': '<omitted: media>',
               'magic': [{'id': 'png', 'tests': [{'offset': 0, 'hex': '89504e470d0a1a0a'}]},
                         {'id': 'jpeg', 'tests': [{'offset': 0, 'hex': 'ffd8ff'}]},
                         {'id': 'gif87a', 'tests': [{'offset': 0, 'hex': '474946383761'}]},
                         {'id': 'gif89a', 'tests': [{'offset': 0, 'hex': '474946383961'}]},
                         {'id': 'riff-wave', 'tests': [{'offset': 0, 'hex': '52494646'}, {'offset': 8, 'hex': '57415645'}]},
                         {'id': 'riff-webp', 'tests': [{'offset': 0, 'hex': '52494646'}, {'offset': 8, 'hex': '57454250'}]},
                         {'id': 'id3', 'tests': [{'offset': 0, 'hex': '494433'}]},
                         {'id': 'mp3-frame-sync', 'tests': [{'offset': 0, 'hex': 'ffe0', 'mask': 'ffe0'}]},
                         {'id': 'ogg', 'tests': [{'offset': 0, 'hex': '4f676753'}]},
                         {'id': 'flac', 'tests': [{'offset': 0, 'hex': '664c6143'}]},
                         {'id': 'ftyp', 'tests': [{'offset': 4, 'hex': '66747970'}]},
                         {'id': 'pdf', 'tests': [{'offset': 0, 'hex': '255044462d'}]}],
               'data_uri': {'scheme_boundary': 'The character before data: must not be an ASCII letter, digit, plus, dot, hyphen, '
                                               'underscore or colon. Never match the data: suffix of metadata: or another '
                                               'scheme/name.',
                            'value_boundary': 'When an exact scalar/body/attribute/array-element or decoded query value begins with '
                                              'data: and contains a comma, the entire value is the URI. Quotes, apostrophes, '
                                              'whitespace, brackets and embedded URLs are payload, never delimiters. Recognize it '
                                              'before contextual media rules. Only free text uses delimiter-limited scanning; a '
                                              'prefix without a comma is not a data URI.',
                            'any_encoding': True,
                            'base64_replacement': '<omitted: {length} base64 chars>',
                            'other_replacement': '<omitted: data URI>',
                            'allow_wrapped_base64': True},
               'mime_siblings': {'names': ['mimetype', 'mediatype', 'contenttype'],
                                 'prefixes': ['audio/', 'image/', 'video/'],
                                 'exact': ['application/pdf', 'application/octet-stream'],
                                 'payload_fields': ['data',
                                                    'bytes',
                                                    'content',
                                                    'value',
                                                    'body',
                                                    'base64',
                                                    'b64json',
                                                    'filedata',
                                                    'audiobase64'],
                                 'scope': "The same object's payload fields and all their descendants, including source/inlineData "
                                          'wrappers. Payload ancestry persists through arbitrary chunk wrappers and numeric indices. '
                                          'Audio metadata_fields reset inherited media, audio and payload context at that component, '
                                          'so transcript.content is text. Fresh explicit hints/fields can start new context beneath '
                                          'a boundary. For flattened siblings use the exact indexed parent path; never bleed into '
                                          'another array element. Scalar, string-array and indexed MIME hints bind to the MIME '
                                          "field's parent, not its index. MIME parameters after a semicolon are ignored."},
               'image': {'type_names': ['image', 'imageurl', 'inputimage'],
                         'modality_names': ['image'],
                         'payload_fields': ['data', 'bytes', 'content', 'image', 'imageurl', 'url', 'base64', 'b64json'],
                         'scope': 'Type, modality and image MIME siblings establish image context in the same object and its '
                                  'descendants. Existing generic MIME payload fields retain their policy. These additional image '
                                  'payload fields omit encoded values at any length, including image_url and url, while ordinary '
                                  'HTTP URLs remain text. Audio metadata boundaries still reset inherited context except when the '
                                  'component is itself an image payload field. Exact indexed parents isolate sibling hints. No '
                                  'magic-signature or positive-cap exemption is added.'},
               'video': {'type_names': ['video', 'inputvideo'],
                         'modality_names': ['video'],
                         'scope': 'Type and modality video declarations establish the same generic media context as video MIME '
                                  'siblings. The mime_siblings payload_fields and their descendants are omitted at every image cap, '
                                  'even without a magic signature. Audio metadata boundaries and exact indexed-parent isolation '
                                  'remain unchanged. This adds no safe-MIME evidence and does not extend image-only URL payload '
                                  'fields to video.'},
               'audio': {'fields': ['audio', 'inputaudio', 'outputaudio', 'audiobase64', 'audiodata', 'audiobytes', 'audiocontent'],
                         'payload_fields': ['data',
                                            'bytes',
                                            'content',
                                            'value',
                                            'body',
                                            'base64',
                                            'b64',
                                            'b64json',
                                            'delta',
                                            'chunk',
                                            'samples'],
                         'type_names': ['audio', 'inputaudio', 'outputaudio'],
                         'any_size': True,
                         'scope': "An audio field's direct scalar or indexed/array samples are payload, but an audio object "
                                  'establishes only audio context for its named children. Only explicit payload fields start '
                                  'inherited payload context through arbitrary wrappers. Keep direct-sample context separate from '
                                  'established payload ancestry: a nested audio field never clears that ancestry; only '
                                  'metadata_fields do. When a field is both an audio field and a MIME payload field (audio_base64), '
                                  'both rules apply, so MIME-established payload ancestry persists through its named wrappers. '
                                  'Parameters such as voice, format, max_tokens, sample_rate, speed, IDs and usage are not '
                                  'implicitly payload. Type hints establish audio context, not payload. Credential, magic-byte and '
                                  'data-URI rules still apply independently.',
                         'numeric_arrays': 'Replace numeric/bool payload scalars or arrays with other_replacement; walk arrays of '
                                           'strings/objects preserving structure. Null and empty arrays carry no media bytes.',
                         'metadata_fields': ['transcript',
                                             'text',
                                             'format',
                                             'mimetype',
                                             'mediatype',
                                             'contenttype',
                                             'duration',
                                             'durationsecs',
                                             'language',
                                             'inputbytes',
                                             'outputbytes',
                                             'samplerate',
                                             'channels',
                                             'url',
                                             'audiourl',
                                             'type',
                                             'voice',
                                             'speed',
                                             'maxtokens',
                                             'id',
                                             'expiresat',
                                             'usage']}},
     'fixture_sites': ['request_headers',
                       'request_query',
                       'request_body',
                       'response_body',
                       'attribute_keys',
                       'flattened_paths',
                       'indexed_paths',
                       'mime_siblings',
                       'magic_bytes',
                       'free_text_urls'],
     'limits': {'max_depth': 32, 'max_nodes': 16384, 'max_text_chars': 4194304},
     'scope': 'S6 recognition only. SDK S1 snapshot/hide-family and S5 process-wide registry/transport observation are mandatory in '
              'addition; this catalog does not redefine registry timing or permit body buffering on suppressed requests.',
     'logprob_tokens': {'version': 1,
                        'disclosure': 'logprob tokens are checked against registered secrets and credential shapes, not key-name '
                                      'rules',
                        'anchor': 'Explicit parsed provider response root only; never generic attributes, tool results, user '
                                  'content, nested JSON strings or flattened key spellings. Wildcards require real arrays.',
                        'reconstruction': 'If value masking changes any choice message.content or delta.content, fully mask its '
                                          'entire logprobs block. Buffered streams are scanned by choice index before export: '
                                          'earlier, current and later blocks are masked, including top_logprobs and bytes. '
                                          'Concatenated deltas are checked too; affected streaming text is fully masked. No raw '
                                          'delta is exported before this scan.',
                        'formats': [{'format': 'openai.chat',
                                     'paths': [['choices', '*', 'logprobs', 'content', '*', 'token'],
                                               ['choices', '*', 'logprobs', 'content', '*', 'top_logprobs', '*', 'token']],
                                     'text_paths': [['message', 'content'], ['delta', 'content']],
                                     'sources': ['typescript/packages/traceai_openai/src/instrumentation.ts (chat result and '
                                                 'buffered chat chunks)',
                                                 'spec/auto-instrumentation/generate_privacy_fixtures.py '
                                                 '(ordinary-logprobs-content)'],
                                     'provider_equivalents': 'Only the same OpenAI chat response structure is evidenced; no '
                                                             'additional provider-specific token paths are registered.'}]}}


def catalog_bytes(catalog):
    """privacy.json as committed: two-space JSON, with a list of names under a "keys" member on one line when that line fits in 120
    characters."""
    def inline(match):
        line = match.group(1) + "[" + ", ".join(item.strip().rstrip(",") for item in match.group(2).splitlines()) + "]"
        return line if len(line) <= 120 else match.group(0)
    text = re.sub(r'( *"keys": )\[\n((?: +"[^"\n]*",?\n)+) +\]', inline, json.dumps(catalog, indent=2, ensure_ascii=False))
    return (text + "\n").encode("utf-8")


def normalize(name):
    return name.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")).translate(
        str.maketrans("", "", "-_."))


def components(path):
    return [part for part in re.split(r"[.\[\]]+", path) if part and not part.isdecimal()]


def leaf(path):
    parts = components(path)
    return normalize(parts[-1]) if parts else ""


class PathBudget(ValueError):
    """A name of more than limits.max_depth components: the whole exported value becomes the placeholder (fail closed)."""


class Span(tuple):
    """A span (start, end) the catalog replaced, by position. omission is true for an omission the catalog wrote (a data URI or a
    media value), false for a placeholder. The kind is recorded when the span is written, never read back from its text."""

    def __new__(cls, start, end, omission=False):
        span = tuple.__new__(cls, (start, end))
        span.omission = omission
        return span


class Rewrite:
    """An output text built from kept and replaced pieces, recording by position every span the catalog replaced (each placeholder
    and each data URI omission it writes) and whether it is an omission."""

    def __init__(self, reference):
        self.reference, self.parts, self.spans, self.length = reference, [], [], 0

    def keep(self, text):
        self.parts.append(text)
        self.length += len(text)

    def put(self, text, omission=False):
        self.spans.append(Span(self.length, self.length + len(text), omission))
        self.keep(text)

    def region(self, text, runs=()):
        """Write a part of a URL (the path, the fragment, a decoded query value): each run (start, end) of runs, sorted and disjoint
        (an empty run inserts the placeholder), becomes the placeholder, and each data URI in the original text its omission. Data
        URIs are read in the original text, so a placeholder never cuts one short: where a run and a data URI overlap, the run's
        placeholder is written and the data URI's omission covers the rest of it. One pass over runs and data URIs."""
        data = []
        for start, end, _ in scan_text(DATA, text):
            replacement = self.reference.data_uri(text[start:end])
            if replacement is not None:
                data.append((start, end, replacement))
        placeholder, position, length, r, d, last = self.reference.credentials["replacement"], 0, len(text), 0, 0, None
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


def replace_runs(value, runs, spans, replacement="<redacted>"):
    """Replace each (start, end) of runs (sorted, disjoint) with the placeholder. A span a run overlaps is dropped; the others move."""
    if not runs:
        return value, spans
    out, moved, copied, shift, k = [], [], 0, 0, 0
    for start, end in runs:
        while k < len(spans) and spans[k][1] <= start:
            moved.append(Span(spans[k][0] + shift, spans[k][1] + shift, getattr(spans[k], "omission", False)))
            k += 1
        while k < len(spans) and spans[k][0] < end:
            k += 1
        out.append(value[copied:start])
        out.append(replacement)
        copied = end
        shift += len(replacement) - (end - start)
    moved.extend(Span(span[0] + shift, span[1] + shift, getattr(span, "omission", False)) for span in spans[k:])
    out.append(value[copied:])
    return "".join(out), moved


class Reference:
    def __init__(self, catalog):
        self.catalog = catalog
        self.credentials = catalog["credentials"]
        self.patterns = [item["contains"] for item in self.credentials["patterns"]]
        self.words = frozenset(self.credentials["vocabulary"]["words"])
        patterns = {item["id"]: item["contains"] for item in self.credentials["patterns"]}
        self.exclusions = [(re.compile(item["regex"]), [patterns[name] for name in item.get("unless_contains_patterns", ())])
                           for item in self.credentials["exclusions"]]
        self.contextual_exclusions = [re.compile(item["path_regex"]) for item in self.credentials["contextual_exclusions"]]
        self.signed = {normalize(name) for name in catalog["signed_url"]["parameters"]}
        self.media = catalog["media"]
        self.mime = self.media["mime_siblings"]
        self.audio = self.media["audio"]
        self.max_depth = catalog["limits"]["max_depth"]
        # The catalog's value shapes as read at a --name's value start (phase 1e, I3): the value after an encoded gap is read as after
        # a plain one, where the shape's leading word boundary holds.
        self.word_shapes = [re.compile(shape["regex"][2:] if shape["regex"].startswith("\\b") else shape["regex"],
                                       re.I if "i" in shape["flags"] else 0) for shape in self.credentials["value_shapes"]]

    def path_budget(self, name):
        """Java's PrivacyCatalog.path budget: a name of more than max_depth components (each non-empty run between '.', '[' and ']',
        numeric ones included) is not read."""
        if name.count(".") + name.count("[") + name.count("]") >= self.max_depth and \
                sum(1 for part in re.split(r"[.\[\]]+", name) if part) > self.max_depth:
            raise PathBudget("privacy path budget")

    def excluded(self, name):
        folded = normalize(name)
        return any(regex.fullmatch(folded) and not any(marker in folded for marker in guards)
                   for regex, guards in self.exclusions)

    def credential_name(self, name):
        self.path_budget(name)
        parts = components(name)
        excluded, context = False, []
        # Exclusions apply to one component, never the rest of the path. In
        # particular a logprob token exemption cannot excuse secret ancestors,
        # and wildcard metric exclusions cannot hide independent auth markers.
        # The vocabulary words count as a whole name or a segment of a component (PrivacyCatalog.credentialName).
        for part in parts:
            folded = normalize(part)
            context.append(folded)
            current = ".".join(context)
            if self.java_excluded(folded) or any(regex.fullmatch(current) for regex in self.contextual_exclusions):
                excluded = True
            elif any(pattern in folded for pattern in self.patterns) or self.vocabulary(part):
                return True
        if excluded:
            return False
        folded = normalize(name)
        return not self.java_excluded(folded) and (any(pattern in folded for pattern in self.patterns) or self.vocabulary(name))

    def query_name(self, name):
        return normalize(name) in self.signed or self.credential_name(name)

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
            letter = not 0xD800 <= unit <= 0xDFFF and java_letter_or_digit(c)
            boundary = not letter or (previous is not None and c.isupper() and previous.islower())
            if boundary and word:
                if "".join(word) in self.words:
                    return True
                word = []
            if letter:
                word.append(java_lower(c))
            previous = None if 0xD800 <= unit <= 0xDFFF else c
        return False

    def java_excluded(self, folded):
        """Reference.excluded with Java's Pattern semantics: '.' matches no line terminator (LF, CR, U+0085, U+2028, U+2029)."""
        if not hasattr(self, "_java_exclusions"):
            self._java_exclusions = [(re.compile(regex.pattern.replace(".", "[^\\n\\r\\u0085\\u2028\\u2029]")), guards)
                                     for regex, guards in self.exclusions]
        return any(regex.fullmatch(folded) and not any(marker in folded for marker in guards)
                   for regex, guards in self._java_exclusions)

    def pair_catalog_name(self, name):
        """The catalog's name rule as the pair rule reads it: the patterns, exclusions and vocabulary words per component, then on the
        whole name. Over-redaction fails closed."""
        self.path_budget(name)
        parts = [part for part in re.split(r"[.\[\]]", name) if part and not part.isdecimal()]
        excluded, context = False, []
        for part in parts:
            folded = normalize(part)
            context.append(folded)
            current = ".".join(context)
            if self.java_excluded(folded) or any(regex.fullmatch(current) for regex in self.contextual_exclusions):
                excluded = True
            elif any(pattern in folded for pattern in self.patterns) or self.vocabulary(part):
                return True
        if excluded:
            return False
        folded = normalize(name)
        return not self.java_excluded(folded) and (any(pattern in folded for pattern in self.patterns) or self.vocabulary(name))

    def pair_name(self, name):
        """A credential name for the pair rule, with the command-line and bracket spellings undone first: --password,
        -Ddb.password and user[password]."""
        if not name:
            return False
        stripped = name.lstrip("-")
        if stripped.startswith("D") and len(stripped) > 1 and name.startswith("-D"):
            stripped = stripped[1:]
        stripped = stripped.replace("[", ".").replace("]", ".")
        return bool(stripped) and (self.pair_catalog_name(stripped) or self.pair_catalog_name(name))

    def separator_pair(self, text, i, size, floor, credential):
        """The separator branch's decision for the separator (of size) at i, the name read back no further than floor: (pair, name,
        overflow). pair is true when the name is a credential name or undecided (a gap that is not readable, a name past its window,
        or a name that ends at a percent escape and stays undecided after one decode, I2)."""
        name_end = gap_start(text, i, floor)
        readable = readable_gap(text, name_end, i)
        name_start = name_end
        # The read-back stops at any %XX escape (the hex digits of %3D are name characters, but they are not part of a name).
        while (name_start > floor and name_end - name_start < NAME_CAP and text[name_start - 1] in PAIR_NAME_CHARS
               and not escape_before(text, name_start, floor)):
            name_start -= 1
        # The window is full and the name runs on before it: the name is not read, the pair is replaced.
        overflow = (name_end - name_start >= NAME_CAP and name_start > floor and text[name_start - 1] in PAIR_NAME_CHARS
                    and not escape_before(text, name_start, floor))
        if overflow:
            # The window alone may already hold more than max_depth components (the whole value is then the placeholder).
            self.path_budget(text[name_start:name_end])
        # A name that ends at a percent escape is decoded once (I2): undecided when a %XX remains after the one decode or the read
        # found nothing.
        escaped = None if overflow else escaped_name(text, name_start, name_end, floor)
        name = text[name_start:name_end] if escaped is None else escaped[0]
        # A gap that is not readable leaves the name undecided: the pair is replaced (fail closed).
        pair = ((name_end - name_start >= 2 and (overflow or not readable or credential(text[name_start:name_end])))
                or (escaped is not None and (escaped[1] or not readable or credential(escaped[0]))))
        return pair, name, overflow

    def flag_ahead(self, text, start, credential):
        """Whether another flag starts at start, so that a --name before it has no value word (phase 1e r2): a "--", or a single-dash
        flag with a full credential name of two or more characters, then plain whitespace (a line break included) and more text.
        Anything else (an encoded gap, the end of the text) leaves the single-dash word the --name's value (fail closed)."""
        dashes = flag_dashes(text, start)
        if dashes != 1:
            return dashes == 2
        end = start + 1
        while end < len(text) and text[end] in PAIR_NAME_CHARS and end - start <= NAME_CAP:
            end += 1
        if end - start < 3 or end - start > NAME_CAP or end >= len(text) or not text[end].isspace():
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
        for quote in (ENTITY_QUOTE, ESCAPED_QUOTE, '"', "'"):
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
            while k < length and k - name < NAME_CAP:
                if text[k] in PAIR_NAME_CHARS:
                    k += 1
                elif (k + 3 <= length and escape_before(text, k + 3, k) and text[k + 1:k + 3] not in ENCODED_EQUALS
                      and not encoded_space_ahead(text, k)):
                    k += 3
                else:
                    break
            if k == name:
                return False
        while k < length and (dashes_space(text[k]) or encoded_space_ahead(text, k)):
            k += encoded_space_ahead(text, k) or 1
        return k < length and bool(separator_at(text, k))

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
        redacted = self.credentials["replacement"]
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
            while plain > 0 and name[plain - 1] in PAIR_NAME_CHARS and not escape_before(name, plain, 0):
                plain -= 1
            escaped = escaped_name(name, plain, len(name), 0) if opening >= 0 else None
            return overflow or credential(name) or (escaped is not None and (escaped[1] or credential(escaped[0]))), name

        def equals_value_end(k, size, name):
            """The value end of a quoted name with '=' or %3D at k (phase 1e r2): up to 8 spaces after the separator, then the true end
            of the value (pair_value_end), or the line end for a list header name."""
            value = k + size
            while value < length and value - k - size < 8 and text[value] == " ":
                value += 1
            if normalize(name) in PAIR_LIST_HEADERS:
                while value < length and text[value] not in "\n\r":
                    value += 1
                return value
            return pair_value_end(text, value)

        def decide(k, size, floor, read_end=True):
            """Phase 1e, I1: a separator inside or at the end of consumed text (read back no further than floor) is handed to the
            branch that reads it at k: the quoted-name branch for a ':' after a quoted name, or for an '=' or %3D after a quoted name
            that is a credential or undecided pair (phase 1e r2), else the separator branch. The end of that pair's value when it
            is a credential or undecided pair (k itself when read_end is false), else -1."""
            quoted = json_name(text, k)
            if quoted is not None and quoted[0] >= floor:
                pair, name = quoted_pair(quoted)
                if size == 1 and text[k] == ":":
                    if not pair:
                        return -1
                    if not read_end:
                        return k
                    value = k + 1
                    while value < length and java_space(text[value]):
                        value += 1
                    return json_value_end(text, value, normalize(name) in PAIR_LIST_HEADERS, quoted[1])
                if pair and QUOTED_EQUALS:
                    return equals_value_end(k, size, name) if read_end else k
            if not self.separator_pair(text, k, size, floor, credential)[0]:
                return -1
            return separator_value_end(text, k, size) if read_end else k

        def quoted_equals(k):
            """Whether the '=' or %3D at k follows a quoted name that is a credential or undecided pair (phase 1e r2)."""
            quoted = json_name(text, k) if k > 0 and QUOTED_EQUALS else None
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
            while value_end < length and (value_end < shape_end or not word_space(text[value_end])):
                size = separator_at(text, value_end) if value_end >= word else 0
                if size and decide(value_end, size, begin, read_end=False) >= 0:
                    first = value_end if first < 0 else first
                    last = value_end
                value_end += 1
            # A separator after the word and a gap on its line (plain or encoded whitespace) is read too: the word is that pair's name
            # (an unreadable gap leaves it undecided, 1c and 1d), so its value is never left behind the replaced word.
            ahead = value_end
            while ahead < length and (dashes_space(text[ahead]) or encoded_space_ahead(text, ahead)):
                ahead += encoded_space_ahead(text, ahead) or 1
            if ahead > value_end and ahead < length and separator_at(text, ahead) and decide(ahead, separator_at(text, ahead), begin,
                                                                                             read_end=False) >= 0:
                first = ahead if first < 0 else first
                last = ahead
            for k in sorted({first, last} - {-1}):
                value_end = max(value_end, decide(k, separator_at(text, k), begin))
            return consumed_end(text, begin, value_end, decide)

        while i < length:
            c = text[i]
            start, stop, replacement, userinfo = -1, -1, redacted, False
            quoted = json_name(text, i) if c == ":" and i > 0 else None
            if quoted is not None:
                opening, quote, close, overflow = quoted
                # A name past its window is not read; a name before an unreadable gap is read (the list-header rule still applies)
                # but undecided: its value is replaced either way.
                # A quoted name that ends at a percent escape is decoded once (I2); undecided when a %XX remains or it holds nothing.
                pair, name = quoted_pair(quoted)
                if pair:
                    value = i + 1
                    while value < length and java_space(text[value]):
                        value += 1
                    if skip and skipped(value) >= 0:
                        i = max(skipped(value), i + 1)
                        continue
                    header = normalize(name) in PAIR_LIST_HEADERS
                    end = json_value_end(text, value, header, quote)
                    if not header:
                        end = consumed_end(text, value, end, decide)
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
                    while word < length and word - i < 1024 and not java_space(text[word]):
                        word += 1
                    later = text.rfind("@", i + 3, word)
                    colon = text.find(":", i + 3, later) if later > i + 3 else -1
                    if later > i + 3 and colon > i + 3:
                        at = later
                if at < i + 3:
                    i += 3
                    continue
                start, stop, userinfo = i + 3, at, True
            elif (c == "=" or (c == "%" and text[i + 1:i + 3] in ENCODED_EQUALS)) and quoted_equals(i):
                # Phase 1e r2: a quoted name with '=' or %3D ("password"=TAIL, 'password'%3DTAIL, \\"password\\"=TAIL,
                # &quot;password&quot;=TAIL) that is a credential or undecided pair: the value runs to its true end.
                size = 1 if c == "=" else 3
                value = i + size
                while value < length and value - i - size < 8 and text[value] == " ":
                    value += 1
                if skip and skipped(value) >= 0:
                    i = max(skipped(value), i + 1)
                    continue
                name = quoted_pair(json_name(text, i))[1]
                start = value
                stop = equals_value_end(i, size, name)
                if normalize(name) not in PAIR_LIST_HEADERS:
                    stop = consumed_end(text, value, stop, decide)
            elif c in "=:" or (c == "%" and text[i + 1:i + 3] in ENCODED_EQUALS):
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
                    if not overflow and normalize(name) in PAIR_LIST_HEADERS:
                        stop = value
                        while stop < length and text[stop] not in "\n\r":
                            stop += 1
                    else:
                        # The value runs to its true end; a separator at the end of it is read too (I1, general invariant).
                        stop = consumed_end(text, value, pair_value_end(text, value), decide)
            elif c == "-" and (i == 0 or text[i - 1].isspace() or encoded_space_width(text, i, 0)) and flag_dashes(text, i):
                # A --name, or (phase 1e r2) a single-dash flag with a full credential name: -password, never -p or a cluster -abc
                # that is no credential name.
                dashes = flag_dashes(text, i)
                end = i + dashes
                while end < length and text[end] in PAIR_NAME_CHARS and end - i < NAME_CAP + dashes:
                    end += 1
                overflow = end - i >= NAME_CAP + dashes and end < length and text[end] in PAIR_NAME_CHARS
                while overflow and end < length and text[end] in PAIR_NAME_CHARS:
                    end += 1
                if overflow:
                    self.path_budget(text[i:end])
                # The gap (phase 1e): horizontal whitespace (str.isspace less the line breaks, NBSP included, I1) and encoded whitespace
                # (I3), in any order. A line break after the gap is read by STOP 1. An encoded control in the gap starts the value (I5).
                if (end < length and end - i - dashes >= (1 if dashes == 2 else 2)
                        and (dashes_space(text[end]) or text[end] in LINE_BREAKS or encoded_space_ahead(text, end))
                        and (overflow or credential(text[i:end]))):
                    value, control, gap_end, spaced = end, -1, end, False
                    while value < length:
                        if dashes_space(text[value]):
                            value += 1
                            spaced = spaced or control >= 0
                            continue
                        width = encoded_space_ahead(text, value)
                        if not width:
                            break
                        if control < 0 and encoded_control(text, value, width):
                            control = value
                        value += width
                        gap_end = value
                    if dashes_separator(text, value):
                        # A separator after the gap: the pair is name<gap>=value, read at the separator (its gap rule included).
                        dashes_colon = dashes_colon_at(text, value)
                        i = value
                        continue
                    # An encoded control in the gap starts the value: it is replaced from the control to the end of the value word
                    # (I5); with no value word, the gap from the control to its last encoded character.
                    begin, value_end = (control if control >= 0 else value), -1
                    if value < length and text[value] in LINE_BREAKS:
                        # STOP 1: after ONE line break the next line is read fresh. A pair that starts it is read by its own branch; else
                        # its first word is the value. A blank line in between (two or more line breaks) ends the --name.
                        line = value + (2 if text.startswith("\r\n", value) else 1)
                        # The next line's leading gap: horizontal and encoded whitespace, as after the --name (an encoded control in it
                        # starts the value, I5). A line that holds encoded whitespace and nothing else is no blank line: the read goes on
                        # over its line break to the next line (fail closed).
                        encoded = False
                        while line < length:
                            if dashes_space(text[line]):
                                line += 1
                                continue
                            if encoded and text[line] in LINE_BREAKS:
                                line += 2 if text.startswith("\r\n", line) else 1
                                encoded = False
                                continue
                            width = encoded_space_ahead(text, line)
                            if not width:
                                break
                            if control < 0 and encoded_control(text, line, width):
                                control = line
                            line, encoded = line + width, True
                        if line >= length or text[line] in LINE_BREAKS or dashes_separator(text, line):
                            # A blank line or the end: no value. A separator that starts the line is read at the separator, its name
                            # and the line break before it read back as an unreadable gap (fail closed), as before the ruling.
                            if line >= length or text[line] in LINE_BREAKS:
                                i = end
                            else:
                                dashes_colon, i = dashes_colon_at(text, line), line
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
        if end - marks[0] - 1 > NAME_CAP or len(marks) > QUESTION_CAP:
            return True
        return any(self.query_name(unquote_plus(text[q + 1:end])) for q in marks)

    def question_name(self, raw_name):
        """Whether a raw query name, or the part of it after any '?' (a pair after a second '?'), is a credential name. The whole name
        is read whole; a part after a '?' is read as after_question reads it."""
        if self.query_name(unquote_plus(raw_name)):
            return True
        marks, at = [], raw_name.find("?")
        while at >= 0 and len(marks) <= QUESTION_CAP:
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
        rewrite = out if out is not None else Rewrite(self)
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
                if len(marks) <= QUESTION_CAP:
                    marks.append(i)
            elif c == "=":
                if marks and self.after_question(raw_value, marks, i):
                    return i + 1
                marks = []
            i += 1
        return -1

    def second_question(self, raw_value):
        """A value that holds '?name=...' with a credential name (read from that '?' to the next '=', a '?' in it included):
        everything from that pair's '=' to the value's end goes."""
        cut = self.second_question_cut(raw_value)
        return raw_value if cut < 0 else raw_value[:cut] + self.credentials["replacement"]

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
            full_name = path + "." + name if path else name
            if isinstance(item, str) or (isinstance(item, list) and any(isinstance(value, str) for value in item)):
                # PrivacyCatalog.hint reads the leaf of the full name through the budgeted path parser.
                self.path_budget(full_name)
            hint = self.hint(name, item)
            if any(hint):
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
        return substitute(DATA, value, lambda original, _name, _start: self.data_uri(original) or original)

    def url_value(self, original):
        """Scrub one already-framed URL; never delimit its query values as prose."""
        return self.framed_url(original)[0]

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
                if c in PATH_PAIR_SEPARATORS:
                    k += 1
                    name = k
                    continue
                size = 1 if c == "=" else 3 if c == "%" and base[k + 1:k + 3] in ENCODED_EQUALS else 0
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
            if base[k] not in PATH_PAIR_SEPARATORS:
                k += 1
                continue
            j = k + 1
            while j < end and base[j] not in PATH_PAIR_SEPARATORS and base[j] != "=" and not (
                    base[j] == "%" and base[j + 1:j + 3] in ENCODED_EQUALS):
                j += 1
            if j < end and j > k + 1 and base[j] not in PATH_PAIR_SEPARATORS:
                return k
            k = j
        return end

    def framed_url(self, original):
        """Scrub one already-framed URL. Returns the text and the spans (start, end) the catalog itself replaced in it, by position."""
        out = Rewrite(self)
        target, hashmark, fragment = original.partition("#")
        base, question, raw_query = target.partition("?")
        userinfo = USERINFO.match(base)
        if userinfo:
            out.keep(userinfo.group(1))
            out.put(self.credentials["replacement"])
            out.keep("@")
            base = base[userinfo.end():]
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
            if self.query_name(name) or self.question_name(raw_name):
                out.put(self.credentials["replacement"])
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
                out.put(self.credentials["replacement"])
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

    def text(self, value, secrets, framed=False, structured=False):
        def token(original, group, start):
            if group == "data":
                return self.data_text(original)
            # Retain enclosing single quotes in prose without treating internal
            # apostrophes (even the first character of a value) as delimiters.
            closing_quote = ""
            if start and value[start - 1] == "'" and original.endswith("'"):
                original, closing_quote = original[:-1], "'"
            return self.url_value(original) + closing_quote

        # An outer scalar boundary does not prove the value is a single URL:
        # messages/errors may start with one URL and contain several more. Exact
        # URLs still frame literal data-URI queries before prose tokenization.
        whole_url = framed and self.is_url_value(value)
        if not whole_url:
            value = substitute(TEXT, value, token)
            for secret in sorted({s for s in secrets if len(s) >= 8 and s != "<redacted>"}, key=lambda s: (-len(s), s)):
                value = "<redacted>".join(part.replace(secret, "<redacted>") for part in value.split("<redacted>"))
            for shape in self.credentials["value_shapes"]:
                value = re.sub(shape["regex"], "<redacted>", value, flags=re.I if "i" in shape["flags"] else 0)
            # The pair rule runs last, after known secrets and value shapes. Re-dumped JSON of a structured string is not read
            # again: each of its leaf strings went through the rule on its own.
            return value if structured else self.credential_pairs(value)
        # A whole URL value at a framed site: the catalog pass records by position each span it replaced; known secrets and value
        # shapes move those spans (a span one of them overlaps is gone), and the pair rule then runs over the URL text, skipping
        # only the spans still recorded. The skip never looks at placeholder text.
        value, spans = self.framed_url(value)
        for secret in sorted({s for s in secrets if len(s) >= 8 and s != "<redacted>"}, key=lambda s: (-len(s), s)):
            runs, offset = [], 0
            for part in value.split("<redacted>"):
                found = part.find(secret)
                while found >= 0:
                    runs.append((offset + found, offset + found + len(secret)))
                    found = part.find(secret, found + len(secret))
                offset += len(part) + len("<redacted>")
            value, spans = replace_runs(value, runs, spans)
        for shape in self.credentials["value_shapes"]:
            runs = [match.span() for match in re.finditer(shape["regex"], value, flags=re.I if "i" in shape["flags"] else 0)]
            value, spans = replace_runs(value, runs, spans)
        return self.credential_pairs(value, spans)

    def scrub(self, value, path="", siblings=None, secrets=(), _media=False, _audio=False, _depth=0, _budget=None, _hints=None, framed=True):
        if _budget is None:
            # The exported value as a whole: a name (a path, a map key, a pair name in text) of more than max_depth components
            # makes the whole value the placeholder, as Java's agent exports it.
            try:
                return self._scrub(value, path, siblings, secrets, _media, _audio, _depth, _budget, _hints, framed)
            except PathBudget:
                return self.credentials["replacement"]
        return self._scrub(value, path, siblings, secrets, _media, _audio, _depth, _budget, _hints, framed)

    def map_key(self, name, secrets):
        """A map key goes through the export rules of a string value at a framed site that need no context: a whole data URI or
        magic-byte media value, then the URL catalog and data URIs, known secrets, value shapes and the pair rule."""
        replacement = self.media_string(name, "", framed=True)
        return replacement if replacement is not None else self.text(name, secrets, framed=True)

    def _scrub(self, value, path="", siblings=None, secrets=(), _media=False, _audio=False, _depth=0, _budget=None, _hints=None, framed=True):
        limits = self.catalog["limits"]
        budget = _budget if _budget is not None else [limits["max_nodes"], limits["max_text_chars"]]
        budget[0] -= 1
        # The name path budget first: a path of more than max_depth components exports the whole value as the placeholder.
        self.path_budget(path)
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
                cleaned = self.scrub(item, child_path, None, secrets, _media, _audio, _depth + 1, budget, hints)
                key = self.map_key(name, secrets)
                # Two keys that are one after scrubbing: one key is kept, in the first one's place, and its value is the placeholder.
                out[key] = self.credentials["replacement"] if key in out else cleaned
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
        structured = False
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except ValueError:
                pass  # Malformed/embedded JSON policy belongs to the S1 boundary.
            else:
                structured = isinstance(parsed, (dict, list))
                clean = self.scrub(parsed, "", None, secrets, media, audio, _depth + 1, budget)
                if clean != parsed:
                    value = json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
        return self.text(value, secrets, framed=framed, structured=structured)

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

    def sites(prefix, name, private, covers, public=17):
        original, expected = (secret, redacted) if private else (public, public)
        add(prefix + "-headers", "request_headers", {name: original}, {name: expected}, covers=covers)
        url = "https://example.invalid/object?" + quote(name, safe="") + "=" + str(original) + "&model=keep#anchor"
        clean_url = "https://example.invalid/object?" + quote(name, safe="") + "=" + str(expected) + "&model=keep#anchor"
        add(prefix + "-query", "request_query", url, clean_url, covers=covers)
        add(prefix + "-request", "request_body", {"nested": [{name: original}]}, {"nested": [{name: expected}]}, covers=covers)
        add(prefix + "-response", "response_body", {"result": {name: original}}, {"result": {name: expected}}, covers=covers)
        for site, path in (("attribute_keys", name), ("flattened_paths", "metadata." + name),
                           ("indexed_paths", "metadata." + name + ".0")):
            add(prefix + "-" + site, site, original, expected, path, covers)

    def url_sites(prefix, original, expected, covers, prose=None):
        # Returned URLs deliberately have no S5 seed: export must recognize them
        # on its own, not pass only because request observation registered them.
        # In prose ("see <url> next") a credential-named pair runs to its true end, so a
        # URL with such a pair gives its own prose expectation; others keep " next".
        add(prefix + "-query", "request_query", original, expected, covers=covers)
        add(prefix + "-response", "response_body", {"url": original}, {"url": expected}, covers=covers)
        add(prefix + "-text", "free_text_urls", "see " + original + " next",
            "see " + expected + " next" if prose is None else prose, covers=covers)
        add(prefix + "-attribute", "attribute_keys", original, expected, "http.url", covers)

    def bounded_url_sites(prefix, original, expected, covers):
        # Literal query payloads may contain prose delimiters. Only exact URL
        # values, not URLs discovered inside prose, supply this outer boundary.
        add(prefix + "-query", "request_query", original, expected, covers=covers)
        add(prefix + "-response", "response_body", {"url": original}, {"url": expected}, covers=covers)
        add(prefix + "-attribute", "attribute_keys", original, expected, "http.url", covers)
        add(prefix + "-indexed", "indexed_paths", original, expected, "output.urls.0", covers)
        add(prefix + "-array", "response_body", [original], [expected], covers=covers)

    def query_object_sites(prefix, pairs, prose=None):
        """Pairs are (name, original, expected); preserve unchanged query bytes."""
        original, expected = [], []
        for name, value, clean in pairs:
            raw_name, raw_value = quote(name, safe=""), quote(value, safe="")
            original.append(raw_name + "=" + raw_value)
            expected.append(raw_name + "=" + (raw_value if clean == value else clean))
        url_sites(prefix, "https://example.invalid/?" + "&".join(original) + "&model=keep#anchor",
                  "https://example.invalid/?" + "&".join(expected) + "&model=keep#anchor", ["query-media-object"], prose)

    for item in catalog["credentials"]["patterns"]:
        sites("pattern-" + item["id"], item["example"], True, ["pattern:" + item["id"]])
    for item in catalog["credentials"]["exclusions"]:
        for i, name in enumerate(item["examples"]):
            # Keep historical case ids after removing two weaker exemptions.
            if item["id"] == "key-identifiers" and name in ("x-amz-key-id", "x-goog-key-id"):
                i += 2
            sites("exclusion-" + item["id"] + "-" + str(i), name, False, ["exclusion:" + item["id"]])
            if item["id"] == "token-timing":
                sites(f"timing-fraction-{i}", name, False, ["token-timing-numeric"], public=0.125)
                sites(f"timing-ancestor-{i}", "secret." + name, True, ["metric-credential-precedence"])
                sites(f"timing-descendant-{i}", name + ".api_key", True, ["metric-credential-precedence"])
    for item in catalog["credentials"]["exclusions"]:
        suffixes = (tuple("_" + name for name in TOKEN_TIMING_NAMES) if item["id"] == "token-timing"
                    else ("_tokens", "_tokens_details", "_token_details", "_token_count", "_token_counts"))
        for marker in item.get("unless_contains_patterns", ()):
            for suffix in suffixes:
                sites(f"metric-guard-{item['id']}-{marker}-{suffix}", "provider_" + marker + suffix, True,
                      ["exclusion-guard:" + item["id"] + ":" + marker])
    for name in ("token", "access_token", "x-access-token", "session_token", "token_secret",
                 "api_token_latency_key", "auth_token"):
        sites("timing-real-credential-" + name, name, True, ["token-timing-credentials"])
    for item in catalog["credentials"]["contextual_exclusions"]:
        for i, path in enumerate(item["examples"]):
            for site in ("attribute_keys", "flattened_paths", "indexed_paths"):
                add(f"contextual-{item['id']}-{i}-{site}", site, "Hello", "Hello", path,
                    ["contextual-exclusion:" + item["id"]])
                add(f"contextual-ancestor-{item['id']}-{i}-{site}", site, secret, redacted, "secret." + path)
                add(f"contextual-descendant-{item['id']}-{i}-{site}", site, secret, redacted, path + ".api_key")
    logprobs = {"choices": [{"logprobs": {"content": [{"token": "Hello", "logprob": -0.2,
                 "top_logprobs": [{"token": "world", "logprob": -0.3}]}]}}]}
    # Keep the shared path no weaker than the manual Mastra oracle.
    # Retain logprob numbers, but token-named payloads no longer get an exemption.
    masked_logprobs = {"choices": [{"logprobs": {"content": [{"token": redacted, "logprob": -0.2,
                       "top_logprobs": [{"token": redacted, "logprob": -0.3}]}]}}]}
    add("ordinary-logprobs-content", "response_body", logprobs, masked_logprobs)
    for i, name in enumerate(("access_key_id", "aws_access_key_id"), 5):
        sites("exclusion-key-identifiers-" + str(i), name, True, ["pattern:key"])
    # Preserve every prior logprob case, tightening only its token expectation.
    for i, path in enumerate(("choices.0.logprobs.content.0.token",
                              "choices.0.logprobs.content.0.top_logprobs.0.token", "logprobs.token")):
        for site in ("attribute_keys", "flattened_paths", "indexed_paths"):
            add(f"contextual-logprob-token-{i}-{site}", site, "Hello", redacted, path, ["pattern:token"])
            add(f"contextual-ancestor-logprob-token-{i}-{site}", site, secret, redacted, "secret." + path)
            add(f"contextual-descendant-logprob-token-{i}-{site}", site, secret, redacted, path + ".api_key")
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
    # In prose the pair rule reads the name as the run of name characters before '=': the whole name, or for the encoded spelling
    # the hex digits and the rest of the name ("%6Bey" reads "6Bey"). Where that run is a credential name the value runs to the end
    # of the text; these prose names are not credential names, so the URL's own rules decide the text:
    prose_public = {"se", "sp", "sv", "sr", "skoid", "sktid", "SE", "SP", "SV", "SR", "SKOID", "SKTID",
                    "73e", "73p", "73v", "73r", "73koid", "73ktid", "6Bey", "74oken", "73ig", "73ignature"}
    for i, name in enumerate(catalog["signed_url"]["parameters"]):
        for variant, raw_name in (("plain", name), ("upper", name.upper()), ("encoded", "%" + format(ord(name[0]), "02X") + name[1:])):
            url = f"https://example.invalid/object?{raw_name}={secret}&model=keep&{raw_name}=second#anchor"
            expected = f"https://example.invalid/object?{raw_name}=<redacted>&model=keep&{raw_name}=<redacted>#anchor"
            prose_name = raw_name[1:] if variant == "encoded" else raw_name
            prose = ("see " + expected + " next" if prose_name in prose_public
                     else f"see https://example.invalid/object?{raw_name}=<redacted>")
            add(f"signed-{i}-{variant}", "free_text_urls", "see " + url + " next", prose, covers=["signed:" + name])
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
    # In prose a credential-named pair runs to its true end, a placeholder already in its value included.
    add("empty-query-secret", "free_text_urls", "https://example.invalid/?token=&model=keep", "https://example.invalid/?token=<redacted>")
    add("already-redacted-query", "free_text_urls", "https://example.invalid/?token=<redacted>&model=keep", "https://example.invalid/?token=<redacted>")
    add("encoded-credential-query", "free_text_urls", "https://example.invalid/?client%2Esecret=" + secret,
        "https://example.invalid/?client%2Esecret=<redacted>")
    add("ordinary-query", "free_text_urls", "https://example.invalid/?key_id=public&max_tokens=23&design=keep",
        "https://example.invalid/?key_id=public&max_tokens=23&design=keep")
    # A credential-named pair after the '#' is replaced by the URL rules at every site; in prose the name before '=' after the
    # '?' ("tag", "foo", "filter") is not a credential name, so only the fragment's pair runs on (to the end of the text).
    for name in ("client_secret?tag", "password?foo", "token?filter"):
        url = "https://example.invalid/?" + name + "=" + secret + "&model=keep#fragment?token=ordinary"
        expected = "https://example.invalid/?" + name + "=<redacted>&model=keep#fragment?token=<redacted>"
        for site in ("request_query", "response_body", "free_text_urls"):
            add("literal-question-query-" + name + "-" + site, site, url, expected, covers=["url-query-boundaries"])
    for i, name in enumerate(("client_secret'tag", "password'foo", "token'filter")):
        for encoding, raw_name in (("literal", name), ("encoded", quote(name, safe=""))):
            for root, base in (("absolute", "https://example.invalid/"), ("relative", "/path")):
                url = base + "?" + raw_name + "=" + secret + "&model=keep#fragment?token=ordinary"
                expected = base + "?" + raw_name + "=<redacted>&model=keep#fragment?token=<redacted>"
                url_sites(f"apostrophe-name-{i}-{encoding}-{root}", url, expected, ["url-apostrophes"], "see " + expected)
    for i, credential in enumerate(("'" + secret, secret + "'suffix", secret + "'", "'" + secret + "'")):
        url = "https://example.invalid/?token=" + credential + "&model=keep#anchor"
        expected = "https://example.invalid/?token=<redacted>&model=keep#anchor"
        url_sites(f"apostrophe-value-{i}", url, expected, ["url-apostrophes"], "see https://example.invalid/?token=<redacted>")
    # Not a whole URL value (it starts with a quote) at any site, so the pair rule reads it: the token value runs to the end,
    # closing quote included.
    url_sites("apostrophe-enclosing-quotes", "'https://example.invalid/?token='" + secret + "'",
              "'https://example.invalid/?token=<redacted>", ["url-apostrophes"], "see 'https://example.invalid/?token=<redacted>")
    url_sites("apostrophe-ordinary-value", "https://example.invalid/?model=author's-model&key_id=public",
              "https://example.invalid/?model=author's-model&key_id=public", ["url-apostrophes"])
    for i, url in enumerate(("https://example.invalid/#fragment?token=ordinary",
                             "https://example.invalid/?model=keep#fragment?token=ordinary",
                             "#fragment?token=ordinary", "/path#fragment?token=ordinary")):
        # The case names are historical: a credential-named pair in the fragment is replaced, in prose to the end of the text.
        add("fragment-preserved-" + str(i), "free_text_urls", "see " + url + " next",
            "see " + url.replace("?token=ordinary", "?token=<redacted>"), covers=["url-query-boundaries"])
    add("relative-query-reference", "free_text_urls", "see /path?token?filter=" + secret + "#fragment?token=ordinary next",
        "see /path?token?filter=<redacted>#fragment?token=<redacted>", covers=["url-query-boundaries"])
    add("query-value-question-preserved", "free_text_urls", "https://example.invalid/?model=why?token=ordinary",
        "https://example.invalid/?model=why?token=<redacted>", covers=["url-query-boundaries"])
    # The first token value ends where the next pair ("https:") starts after a space; " then" is part of that value.
    add("multiple-url-fragments", "free_text_urls",
        "https://example.invalid/#fragment?token=ordinary then https://example.invalid/?token=" + secret,
        "https://example.invalid/#fragment?token=<redacted> https://example.invalid/?token=<redacted>", covers=["url-query-boundaries"])
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
                if query.startswith("token="):
                    # A credential-named pair in prose: its value runs to the end of the text.
                    expected = first + separator + "https://example.invalid/?token=<redacted>"
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
            if query.startswith("token="):
                # The token value runs to its true end: the end of the text, or the space before the next pair ("https:").
                expected = "https://example.invalid/?" + clean_query + suffix.replace(" trailing prose", "").replace(" then ", " ")
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
                                                           ("api_key", secret, redacted)],
                       "see https://example.invalid/?mime_type=audio%2Fpcm&content=<omitted: 8 base64 chars>&api_key=<redacted>")
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

    # The credential pair rule and the fragment-pair catalog rule. Whole URL values at the url_sites, then the same shapes and the
    # JSON quoted-name ruling in prose at every url-prefixed-prose site. Synthetic TAIL markers only.
    for shape, url, clean in (
            ("fragment-after-query", "https://example.invalid/?model=keep#token=TAILA", "https://example.invalid/?model=keep#token=" + redacted),
            ("second-question", "https://example.invalid/?a=1?token=TAILB", "https://example.invalid/?a=1?token=" + redacted),
            ("placeholder-continues", "https://example.invalid/?password=" + redacted + ",TAILC", "https://example.invalid/?password=" + redacted),
            ("shape-continues", "https://example.invalid/#password=Bearer+EXAMPLEVALUEONLY,TAILD", "https://example.invalid/#password=" + redacted)):
        prefix = "pair-rule-url-" + shape
        add(prefix + "-query", "request_query", url, clean, covers=["url-fragment-pairs"])
        add(prefix + "-response", "response_body", {"url": url}, {"url": clean}, covers=["url-fragment-pairs"])
        # In prose the pair rule carries the value on to its true end, which here is the end of the text.
        add(prefix + "-text", "free_text_urls", "see " + url + " next", "see " + clean, covers=["url-fragment-pairs", "credential-pair-rule"])
        add(prefix + "-attribute", "attribute_keys", url, clean, "http.url", ["url-fragment-pairs"])
    prose = [
        ("fragment-after-query", "log https://example.invalid/?model=keep#token=TAILA then more", "log https://example.invalid/?model=keep#token=" + redacted),
        ("second-question", "log https://example.invalid/?a=1?token=TAILB then more", "log https://example.invalid/?a=1?token=" + redacted),
        ("placeholder-continues", "log password=" + redacted + ",TAILC then more", "log password=" + redacted),
        ("shape-continues", "log password=Bearer EXAMPLEVALUEONLY,TAILD then more", "log password=" + redacted),
        ("name-after-question-in-word", "log #a?token=TAILE,password=TAILF then more", "log #a?token=" + redacted + ",password=" + redacted),
        ("json-string-then-text", 'log "password": "TAILG" then more', 'log "password": "' + redacted + '" then more'),
        ("json-escaped-quote", 'log "password": "TAIL\\"H" then more', 'log "password": "' + redacted + '" then more'),
        ("json-entity-quote-inside", 'log "password": "TAIL&quot;I" then more', 'log "password": "' + redacted + '" then more'),
        ("json-entity-quote-delimited", 'log "password": &quot;TAILJ&quot; then more', 'log "password": "' + redacted + '"'),
        ("json-word-then-words", 'log "password": TAILK then more', 'log "password": "' + redacted + '"'),
    ]
    for scalar, token in (("number", "12345"), ("true", "true"), ("false", "false"), ("null", "null")):
        # (b) a JSON scalar ends there only before ',', '}', ']' or the end of the text; (c) before a word it is prose.
        for end, suffix, kept in (("comma", ", then more", ", then more"), ("brace", "} then more", "} then more"),
                                  ("bracket", "] then more", "] then more"), ("end", "", ""), ("word", " TAILL then more", "")):
            prose.append((f"json-{scalar}-{end}", 'log "password": ' + token + suffix, 'log "password": "' + redacted + '"' + kept))
    for shape, text, clean in prose:
        prefix = "pair-rule-prose-" + shape
        covers = ["credential-pair-rule"] + (["json-quoted-name-ruling"] if shape.startswith("json-") else [])
        for site, path in (("response_body", ""), ("attribute_keys", "exception.message"), ("flattened_paths", "output.message"),
                           ("indexed_paths", "output.messages[0]"), ("free_text_urls", "")):
            add(prefix + "-" + site, site, text, clean, path, covers)
        for site in ("request_body", "response_body"):
            add(prefix + "-object-" + site, site, {"message": text}, {"message": clean}, covers=covers)
        add(prefix + "-array", "response_body", [text], [clean], covers=covers)

    # Phase 1b. The catalog's vocabulary words at every site, URL path pairs, map keys, the name path budget, the pair rule over a
    # whole URL value (skipping by position only the spans the catalog replaced), names longer than a bounded read (fail closed),
    # quoted names spelled &quot; or \" and object or array values under a quoted name in prose. Synthetic TAIL markers only.
    prose_sites = (("response_body", ""), ("attribute_keys", "exception.message"), ("flattened_paths", "output.message"),
                   ("indexed_paths", "output.messages[0]"), ("free_text_urls", ""))

    def whole_url_sites(prefix, url, clean, prose, covers):
        add(prefix + "-query", "request_query", url, clean, covers=covers)
        add(prefix + "-response", "response_body", {"url": url}, {"url": clean}, covers=covers)
        add(prefix + "-attribute", "attribute_keys", url, clean, "http.url", covers)
        if prose is not None:
            add(prefix + "-text", "free_text_urls", "see " + url + " next", prose, covers=covers)

    def prose_cases(prefix, text, clean, covers):
        for site, path in prose_sites:
            add(prefix + "-" + site, site, text, clean, path, covers)
        for site in ("request_body", "response_body"):
            add(prefix + "-object-" + site, site, {"message": text}, {"message": clean}, covers=covers)
        add(prefix + "-array", "response_body", [text], [clean], covers=covers)

    for word in catalog["credentials"]["vocabulary"]["words"]:
        for form, name in (("whole", word), ("segment", "db_" + word), ("camel", "db" + word.capitalize())):
            prefix, covers = "pair-rule-1b-vocabulary-" + word + "-" + form, ["vocabulary:" + word]
            add(prefix + "-query", "request_query", "https://example.invalid/?" + name + "=TAILV&model=keep",
                "https://example.invalid/?" + name + "=" + redacted + "&model=keep", covers=covers)
            add(prefix + "-fragment", "request_query", "https://example.invalid/?model=keep#" + name + "=TAILV&view=keep",
                "https://example.invalid/?model=keep#" + name + "=" + redacted + "&view=keep", covers=covers)
            add(prefix + "-path", "request_query", "https://example.invalid/v1/" + name + "=TAILV/next",
                "https://example.invalid/v1/" + name + "=" + redacted + "/next", covers=covers + ["url-path-pairs"])
            add(prefix + "-headers", "request_headers", {name: "TAILV", "accept": "keep"}, {name: redacted, "accept": "keep"}, covers=covers)
            add(prefix + "-request", "request_body", {"nested": [{name: "TAILV", "model": "keep"}]},
                {"nested": [{name: redacted, "model": "keep"}]}, covers=covers)
            add(prefix + "-response", "response_body", {"result": {name: "TAILV", "model": "keep"}},
                {"result": {name: redacted, "model": "keep"}}, covers=covers)
            add(prefix + "-map-key", "response_body", {"note " + name + "=TAILV": "keep", "model": "keep"},
                {"note " + name + "=" + redacted: redacted, "model": "keep"}, covers=covers + ["map-keys"])
            add(prefix + "-attribute", "attribute_keys", "TAILV", redacted, name, covers)
            add(prefix + "-flattened", "flattened_paths", "TAILV", redacted, "metadata." + name, covers)
            add(prefix + "-indexed", "indexed_paths", "TAILV", redacted, "metadata." + name + ".0", covers)
            add(prefix + "-prose", "free_text_urls", "log " + name + "=TAILV then more", "log " + name + "=" + redacted,
                covers=covers + ["credential-pair-rule"])
            add(prefix + "-prose-json", "response_body", 'log "' + name + '": "TAILV" then more',
                'log "' + name + '": "' + redacted + '" then more', covers=covers + ["credential-pair-rule"])

    # URL path pairs: the value of a <credential name>=<value> or %3D path segment runs to the segment end.
    for shape, url, clean, prose in (
            ("single", "https://example.invalid/password=TAILP", "https://example.invalid/password=" + redacted,
             "see https://example.invalid/password=" + redacted),
            ("mid", "https://example.invalid/a/password=TAILP/b", "https://example.invalid/a/password=" + redacted + "/b",
             "see https://example.invalid/a/password=" + redacted),
            ("query-fragment", "https://example.invalid/a/password=TAILP?model=keep#frag",
             "https://example.invalid/a/password=" + redacted + "?model=keep#frag", "see https://example.invalid/a/password=" + redacted),
            # Phase 1c ruling (a): the pair rule reads %3D/%3d as a separator, so in prose the value runs to its true end.
            ("encoded", "https://example.invalid/a/password%3DTAILP/b", "https://example.invalid/a/password%3D" + redacted + "/b",
             "see https://example.invalid/a/password%3D" + redacted),
            ("encoded-lower", "https://example.invalid/a/api_key%3dTAILP?model=keep",
             "https://example.invalid/a/api_key%3d" + redacted + "?model=keep", "see https://example.invalid/a/api_key%3d" + redacted),
            ("matrix", "https://example.invalid/a;access_token=TAILP/b", "https://example.invalid/a;access_token=" + redacted + "/b",
             "see https://example.invalid/a;access_token=" + redacted),
            ("userinfo", "https://user:TAILQ@example.invalid/a/token=TAILP/b?model=keep",
             "https://" + redacted + "@example.invalid/a/token=" + redacted + "/b?model=keep", "see https://" + redacted + "@example.invalid/a/token=" + redacted),
            ("relative", "/a/password=TAILP/b?model=keep", "/a/password=" + redacted + "/b?model=keep", "see /a/password=" + redacted),
            ("not-credential", "https://example.invalid/a/model=keep/b", "https://example.invalid/a/model=keep/b",
             "see https://example.invalid/a/model=keep/b next")):
        whole_url_sites("pair-rule-1b-path-" + shape, url, clean, prose, ["url-path-pairs"])

    # The pair rule over a whole URL value: only the spans the catalog replaced are skipped, by position.
    for shape, url, clean, prose in (
            ("query-comma", "https://example.invalid/?model=keep,password=TAILA", "https://example.invalid/?model=keep,password=" + redacted,
             "see https://example.invalid/?model=keep,password=" + redacted),
            ("fragment-comma", "https://example.invalid/#foo=bar,password=TAILB", "https://example.invalid/#foo=bar,password=" + redacted,
             "see https://example.invalid/#foo=bar,password=" + redacted),
            ("fragment-path", "https://example.invalid/#/cb;access_token=TAILC", "https://example.invalid/#/cb;access_token=" + redacted,
             "see https://example.invalid/#/cb;access_token=" + redacted),
            ("vocabulary-query", "https://example.invalid/?pwd=TAILD", "https://example.invalid/?pwd=" + redacted,
             "see https://example.invalid/?pwd=" + redacted),
            ("catalog-span-then-pair", "https://example.invalid/?token=TAILE&model=keep,password=TAILF",
             "https://example.invalid/?token=" + redacted + "&model=keep,password=" + redacted,
             # In prose the query rule and the pair rule read this text differently on a second pass; whole URL sites only.
             None),
            ("catalog-span-kept", "https://example.invalid/?token=TAILE&model=keep&view=keep",
             "https://example.invalid/?token=" + redacted + "&model=keep&view=keep", "see https://example.invalid/?token=" + redacted),
            ("value-colon", "https://example.invalid/?q=secret:TAILG&model=keep", "https://example.invalid/?q=secret:" + redacted,
             "see https://example.invalid/?q=secret:" + redacted),
            # The pair rule's user information read runs from the first '://' to the later '@' in the same word.
            ("nested-userinfo", "https://example.invalid/?next=https://user:TAILH@example.invalid/&model=keep",
             "https://" + redacted + "@example.invalid/&model=keep", "see https://" + redacted + "@example.invalid/&model=keep next")):
        whole_url_sites("pair-rule-1b-whole-url-" + shape, url, clean, prose, ["whole-url-pair-rule"])

    # Map keys: the export text rules, the pair rule included; keys that collide after scrubbing keep one key, valued the placeholder.
    for shape, value, clean in (
            ("pair", {"note pwd=TAILK": "keep", "model": "keep"}, {"note pwd=" + redacted: redacted, "model": "keep"}),
            ("collide", {"id AKIAEXAMPLETAILR0000": "a", "id AKIAEXAMPLETAILS0000": "b", "model": "keep"},
             {"id " + redacted: redacted, "model": "keep"}),
            ("collide-pair", {"model": "keep", "pwd=TAILM": "a", "pwd=TAILN": "b"}, {"model": "keep", "pwd=" + redacted: redacted}),
            ("collide-placeholder", {"id <redacted>": "a", "id AKIAEXAMPLETAILS0000": "b"}, {"id " + redacted: redacted}),
            ("shape", {"AKIAEXAMPLETAILQ0000": "keep", "model": "keep"}, {redacted: "keep", "model": "keep"}),
            ("url", {"see https://example.invalid/?token=TAILU": "keep"}, {"see https://example.invalid/?token=" + redacted: redacted})):
        add("pair-rule-1b-map-key-" + shape + "-response", "response_body", value, clean, covers=["map-keys"])
        add("pair-rule-1b-map-key-" + shape + "-request", "request_body", {"nested": [value]}, {"nested": [clean]}, covers=["map-keys"])

    # The name path budget: a name of limits.max_depth components is read; one of max_depth + 1 makes the whole value the placeholder.
    depth = catalog["limits"]["max_depth"]
    for count, label in ((depth, "at-max"), (depth + 1, "over-max")):
        over = count > depth
        name = "0." * (count - 1) + "field"
        prefix, covers = "pair-rule-1b-depth-" + label, ["name-path-budget"]
        add(prefix + "-prose", "free_text_urls", "log " + name + "=keep then more", redacted if over else "log " + name + "=keep then more",
            covers=covers)
        add(prefix + "-prose-credential", "response_body", "log " + "0." * (count - 1) + "password=TAILT then more",
            redacted if over else "log " + "0." * (count - 1) + "password=" + redacted, covers=covers)
        add(prefix + "-map-key", "response_body", {name: "keep", "model": "keep"}, redacted if over else {name: "keep", "model": "keep"},
            covers=covers)
        add(prefix + "-flattened", "flattened_paths", "keep", redacted if over else "keep", "metadata." + "0." * (count - 2) + "field", covers)
        add(prefix + "-attribute", "attribute_keys", "keep", redacted if over else "keep", name, covers)

    # Names longer than a bounded read: the trailing window decides when it holds a credential name, and a name that runs on past it
    # is not decided, so the pair is replaced (fail closed).
    pad = "x" * 130
    for shape, url, clean, prose in (
            ("query", "https://example.invalid/?" + pad + "token=TAILE&model=keep", "https://example.invalid/?" + pad + "token=" + redacted + "&model=keep",
             "see https://example.invalid/?" + pad + "token=" + redacted),
            ("query-head", "https://example.invalid/?token" + pad + "=TAILE&model=keep", "https://example.invalid/?token" + pad + "=" + redacted + "&model=keep",
             "see https://example.invalid/?token" + pad + "=" + redacted),
            ("second-question", "https://example.invalid/?a=1?" + pad + "token=TAILE", "https://example.invalid/?a=1?" + pad + "token=" + redacted,
             "see https://example.invalid/?a=1?" + pad + "token=" + redacted),
            ("second-question-head", "https://example.invalid/?a=1?token" + pad + "=TAILE&model=keep",
             "https://example.invalid/?a=1?token" + pad + "=" + redacted + "&model=keep", "see https://example.invalid/?a=1?token" + pad + "=" + redacted),
            ("second-question-undecided", "https://example.invalid/?a=1?" + pad + "=TAILE&model=keep",
             "https://example.invalid/?a=1?" + pad + "=" + redacted + "&model=keep", "see https://example.invalid/?a=1?" + pad + "=" + redacted),
            ("fragment", "https://example.invalid/#" + pad + "token=TAILE&view=keep", "https://example.invalid/#" + pad + "token=" + redacted + "&view=keep",
             "see https://example.invalid/#" + pad + "token=" + redacted),
            ("fragment-second-question", "https://example.invalid/#a=1?" + pad + "token=TAILE",
             "https://example.invalid/#a=1?" + pad + "token=" + redacted, "see https://example.invalid/#a=1?" + pad + "token=" + redacted),
            ("fragment-name-question", "https://example.invalid/#a?token" + pad + "=TAILE&view=keep",
             "https://example.invalid/#a?token" + pad + "=" + redacted + "&view=keep", "see https://example.invalid/#a?token" + pad + "=" + redacted)):
        whole_url_sites("pair-rule-1b-long-name-" + shape, url, clean, prose, ["bounded-names"])
    for shape, text, clean in (
            ("trailing", "log " + pad + "token=TAILE then more", "log " + pad + "token=" + redacted),
            ("head", "log token" + pad + "=TAILE then more", "log token" + pad + "=" + redacted),
            ("undecided", "log " + pad + pad + "=TAILE then more", "log " + pad + pad + "=" + redacted),
            ("quoted", 'log "' + pad + 'token": "TAILE" then more', 'log "' + pad + 'token": "' + redacted + '" then more'),
            ("dashes", "log --token" + pad + " TAILE then more", "log --token" + pad + " " + redacted + " then more")):
        prose_cases("pair-rule-1b-long-name-prose-" + shape, text, clean, ["bounded-names", "credential-pair-rule"])

    # Quoted names spelled with &quot; or an escaped quote (\"): rule (a) with that spelling as the closing quote.
    entity, escaped = "&quot;", '\\"'
    for shape, text, clean in (
            ("entity", "log " + entity + "password" + entity + ": " + entity + "TAILF" + entity + " then more",
             "log " + entity + "password" + entity + ": " + entity + redacted + entity + " then more"),
            ("entity-escape-inside", "log " + entity + "password" + entity + ": " + entity + "TAIL\\" + entity + "F" + entity + " then more",
             "log " + entity + "password" + entity + ": " + entity + redacted + entity + " then more"),
            ("entity-number", "log " + entity + "password" + entity + ": 12345, then more",
             "log " + entity + "password" + entity + ": " + entity + redacted + entity + ", then more"),
            ("escaped", "log " + escaped + "password" + escaped + ": " + escaped + "TAILG" + escaped + " then more",
             "log " + escaped + "password" + escaped + ": " + escaped + redacted + escaped + " then more"),
            ("escaped-escape-inside", "log " + escaped + "password" + escaped + ": " + escaped + "TAIL\\\\" + escaped + "G" + escaped + " then more",
             "log " + escaped + "password" + escaped + ": " + escaped + redacted + escaped + " then more"),
            ("escaped-members", "log {" + escaped + "password" + escaped + ":" + escaped + "TAILG" + escaped + "," + escaped + "model" + escaped
             + ":" + escaped + "keep" + escaped + "}",
             "log {" + escaped + "password" + escaped + ":" + escaped + redacted + escaped + "," + escaped + "model" + escaped + ":" + escaped
             + "keep" + escaped + "}")):
        prose_cases("pair-rule-1b-quoted-" + shape, text, clean, ["quoted-name-spellings", "json-quoted-name-ruling", "credential-pair-rule"])
    add("pair-rule-1b-quoted-json-string-attribute", "attribute_keys", '{"message": "log \\"password\\": \\"TAILG\\" then more"}',
        '{"message":"log \\"password\\": \\"' + redacted + '\\" then more"}', "input.value", ["quoted-name-spellings"])
    add("pair-rule-1b-quoted-json-string-escaped-attribute", "attribute_keys",
        '{"message": "log \\\\\\"password\\\\\\": \\\\\\"TAILG\\\\\\" then more"}',
        '{"message":"log \\\\\\"password\\\\\\": \\\\\\"' + redacted + '\\\\\\" then more"}', "input.value", ["quoted-name-spellings"])

    # Object and array values under a quoted credential name in prose: rule (c), to the true value end (not the balanced close).
    for shape, text in (("object", '"password": {"a": "TAILH"} and more'), ("array", '"password": ["TAILI"] and more')):
        prose_cases("pair-rule-1b-quoted-" + shape + "-value", text, '"password": "' + redacted + '"',
                    ["json-quoted-name-ruling", "credential-pair-rule"])

    # Phase 1c. Item 1: a gap between a name and its separator that is not readable (a tab or any other whitespace but U+0020, or
    # more than GAP_CAP spaces) leaves the name undecided and its value is replaced; up to GAP_CAP spaces stay a readable gap. Each
    # gap under a bare name, a quoted name, a --name and two non-credential controls, in prose and at a JSON site.
    for label, gap in (("tab", "\t"), ("spaces-8", " " * GAP_CAP), ("spaces-9", " " * (GAP_CAP + 1)), ("mixed", "  \t  "),
                       ("em-space", "\u2003")):
        readable = label == "spaces-8"
        for form, text, clean in (
                ("bare", "log password" + gap + "=TAILV then more", "log password" + gap + "=" + redacted),
                ("quoted", 'log "password"' + gap + ': "TAILV" then more', 'log "password"' + gap + ': "' + redacted + '" then more'),
                # A --name, a gap, then a separator: the pair is read at the separator, as for a bare name.
                ("dashes", "log --password" + gap + "=TAILV then more", "log --password" + gap + "=" + redacted),
                ("dashes-spaced", "log --password" + gap + "= TAILV then more", "log --password" + gap + "= " + redacted),
                # A list header (cookie) keeps its extent, the rest of the line, after any gap.
                ("cookie", "log cookie" + gap + ": a=1; b=TAILV, c=TAILW\nthen more", "log cookie" + gap + ": " + redacted + "\nthen more"),
                ("quoted-cookie", 'log "cookie"' + gap + ": a=1; b=TAILV, c=TAILW\nthen more",
                 'log "cookie"' + gap + ': "' + redacted + '"\nthen more'),
                ("model", "log model" + gap + "=keep then more",
                 "log model" + gap + "=keep then more" if readable else "log model" + gap + "=" + redacted),
                ("quoted-model", 'log "model"' + gap + ': "keep" then more',
                 'log "model"' + gap + ': "keep" then more' if readable else 'log "model"' + gap + ': "' + redacted + '" then more')):
            prefix, covers = "pair-rule-1c-gap-" + form + "-" + label, ["pair-name-gaps", "credential-pair-rule"]
            add(prefix + "-prose", "free_text_urls", text, clean, covers=covers)
            add(prefix + "-json", "response_body", {"message": text}, {"message": clean}, covers=covers)

    # Item 2: an omission span never shields a credential pair's value that starts in it; the value runs to its true end. A data URI
    # in a whole URL's query value, cut short by each decoded character that ends one, a base64 data URI, a colon pair and a
    # vocabulary name.
    for shape, url, clean in (
            ("space", "https://example.invalid/?q=password=data:,x%20TAILX", "https://example.invalid/?q=password=" + redacted),
            ("quote", "https://example.invalid/?q=password=data:,x%22TAILX", "https://example.invalid/?q=password=" + redacted),
            ("apostrophe", "https://example.invalid/?q=password=data:,x%27TAILX", "https://example.invalid/?q=password=" + redacted),
            ("lt", "https://example.invalid/?q=password=data:,x%3CTAILX", "https://example.invalid/?q=password=" + redacted),
            ("gt", "https://example.invalid/?q=password=data:,x%3ETAILX", "https://example.invalid/?q=password=" + redacted),
            ("backslash", "https://example.invalid/?q=password=data:,x%5CTAILX", "https://example.invalid/?q=password=" + redacted),
            ("base64", "https://example.invalid/?q=password=data:text/plain;base64,QUJD%20TAILX",
             "https://example.invalid/?q=password=" + redacted),
            ("colon", "https://example.invalid/?q=password:%20data:,x%20TAILX", "https://example.invalid/?q=password: " + redacted),
            ("vocabulary", "https://example.invalid/?q=pwd=data:,x%20TAILX", "https://example.invalid/?q=pwd=" + redacted)):
        whole_url_sites("pair-rule-1c-data-uri-" + shape, url, clean, "see " + clean, ["omission-spans", "whole-url-pair-rule"])

    # Item 3: path segments read every '=' and every %3D (%3d); a credential name's value runs to the next pair in the segment or
    # the segment end. In prose the pair rule runs the value to its true end.
    for shape, url, clean, prose in (
            ("semicolon-equals", "https://example.invalid/a=1;password=TAILX/next", "https://example.invalid/a=1;password=" + redacted + "/next",
             "see https://example.invalid/a=1;password=" + redacted),
            ("semicolon-encoded", "https://example.invalid/a=1;password%3DTAILX/next",
             "https://example.invalid/a=1;password%3D" + redacted + "/next", "see https://example.invalid/a=1;password%3D" + redacted),
            ("comma-equals", "https://example.invalid/a=1,password=TAILX/next", "https://example.invalid/a=1,password=" + redacted + "/next",
             "see https://example.invalid/a=1,password=" + redacted),
            ("comma-encoded", "https://example.invalid/a=1,password%3DTAILX/next",
             "https://example.invalid/a=1,password%3D" + redacted + "/next", "see https://example.invalid/a=1,password%3D" + redacted),
            ("ampersand-equals", "https://example.invalid/a=1&password=TAILX/next",
             "https://example.invalid/a=1&password=" + redacted + "/next", "see https://example.invalid/a=1&password=" + redacted),
            ("ampersand-encoded", "https://example.invalid/a=1&password%3DTAILX/next",
             "https://example.invalid/a=1&password%3D" + redacted + "/next", "see https://example.invalid/a=1&password%3D" + redacted),
            ("lower", "https://example.invalid/a%3d1;api_key%3dTAILX/next", "https://example.invalid/a%3d1;api_key%3d" + redacted + "/next",
             "see https://example.invalid/a%3d1;api_key%3d" + redacted),
            ("several", "https://example.invalid/a=1;password=TAILX;model=keep,token%3DTAILY&view=keep/next",
             "https://example.invalid/a=1;password=" + redacted + ";model=keep,token%3D" + redacted + "&view=keep/next",
             "see https://example.invalid/a=1;password=" + redacted + ";model=keep,token%3D" + redacted),
            ("not-credential", "https://example.invalid/a=1;model%3Dkeep,view=keep/next", "https://example.invalid/a=1;model%3Dkeep,view=keep/next",
             "see https://example.invalid/a=1;model%3Dkeep,view=keep/next next")):
        whole_url_sites("pair-rule-1c-path-every-" + shape, url, clean, prose, ["url-path-every-pair", "url-path-pairs"])

    # Ruling (b): a path pair's value ends at the segment end; a later credential pair in the same URL (a later segment, the query,
    # the fragment) is still replaced by the pair rule after the path pair's span is skipped.
    for shape, url, clean, prose in (
            ("segment", "https://example.invalid/a=1;password=TAILX/next/secret:TAILY",
             "https://example.invalid/a=1;password=" + redacted + "/next/secret:" + redacted, "see https://example.invalid/a=1;password=" + redacted),
            ("query", "https://example.invalid/a=1;password=TAILX/next?model=keep,secret=TAILY",
             "https://example.invalid/a=1;password=" + redacted + "/next?model=keep,secret=" + redacted,
             "see https://example.invalid/a=1;password=" + redacted + ",secret=" + redacted),
            ("fragment", "https://example.invalid/a=1;password=TAILX/next#view=keep,secret=TAILY",
             "https://example.invalid/a=1;password=" + redacted + "/next#view=keep,secret=" + redacted,
             "see https://example.invalid/a=1;password=" + redacted + ",secret=" + redacted)):
        whole_url_sites("pair-rule-1c-path-later-" + shape, url, clean, prose, ["url-path-every-pair", "whole-url-pair-rule"])

    # Item 4: a credential-like host, pinned as it is: the pair rule reads the host before its ':' as a name, so everything after
    # it goes (over-redaction). A host:port without a credential word is kept.
    for shape, url, clean in (
            ("port", "http://keycloak.example.invalid:8080/path?x=1", "http://keycloak.example.invalid:" + redacted),
            ("userinfo", "http://user:TAILU@keycloak.example.invalid:8080/path?x=1", "http://" + redacted + "@keycloak.example.invalid:" + redacted),
            ("control", "http://service.example.invalid:8080/path?x=1", "http://service.example.invalid:8080/path?x=1")):
        whole_url_sites("pair-rule-1c-host-" + shape, url, clean, "see " + clean + (" next" if shape == "control" else ""),
                        ["credential-like-host"])

    # Phase 1d. The cases are authored here from the rulings, not read from the reference's output.
    def prose_pair(prefix, text, clean, covers):
        add(prefix + "-prose", "free_text_urls", text, clean, covers=covers)
        add(prefix + "-json", "response_body", {"message": text}, {"message": clean}, covers=covers)

    # Item 1: the name read-back stops at any %XX escape: the hex digits of %3D are not part of the name that follows it.
    for word in sorted(catalog["credentials"]["vocabulary"]["words"]) + ["password"]:
        prefix, covers = "pair-rule-1d-readback-" + word, ["encoded-name-read-back", "credential-pair-rule"]
        prose_pair(prefix + "-encoded-equals", "log a%3D" + word + "%3DTAILX then more", "log a%3D" + word + "%3D" + redacted, covers)
        for form, url, clean in (("query", "https://example.invalid/?q=a%3D" + word + "%3DTAILX", "https://example.invalid/?q=a%3D" + word + "%3D" + redacted),
                                 ("fragment", "https://example.invalid/#a%3D" + word + "%3DTAILX", "https://example.invalid/#a%3D" + word + "%3D" + redacted)):
            whole_url_sites(prefix + "-" + form, url, clean, "see " + clean, ["encoded-name-read-back", "whole-url-pair-rule"])
    covers = ["encoded-name-read-back", "credential-pair-rule"]
    for shape, text, clean in (
            ("lower", "log a%3dpwd%3dTAILX then more", "log a%3dpwd%3d" + redacted),
            ("before-equals", "log a%3Dpwd=TAILX then more", "log a%3Dpwd=" + redacted),
            ("before-colon", "log a%3Dpwd:TAILX then more", "log a%3Dpwd:" + redacted),
            ("other-escape", "log a%41pwd%3DTAILX then more", "log a%41pwd%3D" + redacted),
            ("space-escape", "log a%20pwd=TAILX then more", "log a%20pwd=" + redacted),
            ("control", "log a%3Dmodel%3Dkeep then more", "log a%3Dmodel%3Dkeep then more")):
        prose_pair("pair-rule-1d-readback-" + shape, text, clean, covers)

    # Item 2: a --name gap is read with str.isspace: U+00A0, U+2007, U+202F and U+0085 are gaps after nothing, a space or a tab.
    for lead_label, lead in (("none", ""), ("space", " "), ("tab", "\t")):
        for code_label, code in (("nbsp", "\u00a0"), ("figure", "\u2007"), ("narrow", "\u202f"), ("nel", "\u0085")):
            gap, prefix = lead + code, "pair-rule-1d-dashes-gap-" + lead_label + "-" + code_label
            covers = ["dashes-gap-whitespace", "credential-pair-rule"]
            prose_pair(prefix + "-equals", "log --password" + gap + "=TAILV then more", "log --password" + gap + "=" + redacted, covers)
            prose_pair(prefix + "-colon", "log --password" + gap + ":TAILV then more", "log --password" + gap + ":" + redacted, covers)
            prose_pair(prefix + "-bare", "log --password" + gap + "TAILV then more", "log --password" + gap + redacted + " then more", covers)

    # Item 3: a --name value that starts with ':' or '://' is the --name's value, not user information.
    covers = ["dashes-colon-value", "credential-pair-rule"]
    for label, gap in (("space", " "), ("tab", "\t")):
        prefix = "pair-rule-1d-dashes-colon-" + label
        prose_pair(prefix + "-slashes", "log --password" + gap + "://TAILV then more", "log --password" + gap + ":" + redacted, covers)
        prose_pair(prefix + "-plain", "log --password" + gap + ":TAILV then more", "log --password" + gap + ":" + redacted, covers)
    prose_pair("pair-rule-1d-dashes-colon-equals-slashes", "log --token=://TAILV then more", "log --token=" + redacted, covers)
    prose_pair("pair-rule-1d-dashes-colon-slashes-end", "log --password ://TAILV", "log --password :" + redacted, covers)

    # Item 4: encoded and escaped whitespace between a name and its separator is an unreadable gap (fail closed).
    covers = ["encoded-gaps", "pair-name-gaps", "credential-pair-rule"]
    escaped = "\\\""
    for label, gap in (("pct-20", "%20"), ("pct-09", "%09"), ("pct-0A", "%0A"), ("pct-0B", "%0B"), ("pct-0C", "%0C"), ("pct-0D", "%0D"),
                       ("pct-C2A0", "%C2%A0"), ("pct-0a", "%0a"), ("pct-0b", "%0b"), ("pct-0c", "%0c"), ("pct-0d", "%0d"),
                       ("pct-c2a0", "%c2%a0"), ("pct-c2A0", "%c2%A0"), ("pct-C2a0", "%C2%a0"),
                       ("json-t", "\\t"), ("json-n", "\\n"), ("json-r", "\\r"), ("json-f", "\\f"), ("json-b", "\\b"),
                       ("json-u0020", "\\u0020"), ("json-u0009", "\\u0009"), ("json-u00a0", "\\u00a0"), ("json-u00A0", "\\u00A0"),
                       ("json-u2003", "\\u2003"), ("json-u2028", "\\u2028"), ("json-u0085", "\\u0085"),
                       ("space-pct-20", " %20"), ("pct-20-space", "%20 ")):
        prefix = "pair-rule-1d-encoded-gap-" + label
        for sep_label, sep in (("equals", "="), ("encoded", "%3D"), ("colon", ":")):
            prose_pair(prefix + "-" + sep_label, "log password" + gap + sep + "TAILX then more", "log password" + gap + sep + redacted, covers)
        prose_pair(prefix + "-quoted", 'log "password"' + gap + ': "TAILX" then more', 'log "password"' + gap + ': "' + redacted + '" then more', covers)
        prose_pair(prefix + "-escaped-quote", "log " + escaped + "password" + escaped + gap + ": " + escaped + "TAILX" + escaped + " then more",
                   "log " + escaped + "password" + escaped + gap + ": " + escaped + redacted + escaped + " then more", covers)
    for label, gap in (("pct-20", "%20"), ("json-t", "\\t")):
        prefix = "pair-rule-1d-encoded-gap-" + label
        prose_pair(prefix + "-model", "log model" + gap + "=keep then more", "log model" + gap + "=" + redacted, covers)
        prose_pair(prefix + "-cookie", "log cookie" + gap + ": a=1; b=TAILX, c=TAILY\nthen more", "log cookie" + gap + ": " + redacted + "\nthen more", covers)
    prose_pair("pair-rule-1d-encoded-gap-serialized-json", "log {" + escaped + "password" + escaped + "\\t: " + escaped + "TAILX" + escaped + ", " + escaped
               + "model" + escaped + ": " + escaped + "keep" + escaped + "}",
               "log {" + escaped + "password" + escaped + "\\t: " + escaped + redacted + escaped + ", " + escaped + "model" + escaped + ": " + escaped
               + "keep" + escaped + "}", covers)

    # Phase 1e r2 (rulings of 527cc071 on I1-I5, STOP 1, STOP 2 and the new pair shapes). Authored from the rulings, never read from
    # the reference's output. The tag after "pair-rule-1e-" is the class of the changed-expectation list.
    line_breaks = (("nl", "\n"), ("cr", "\r"), ("crlf", "\r\n"), ("nel", "\u0085"), ("u2028", "\u2028"), ("u2029", "\u2029"),
                   ("ff", "\x0c"), ("vt", "\x0b"))
    covers = ["dashes-line-break", "credential-pair-rule"]
    # STOP 1: after a credential --name and ONE line break the next line is read fresh: a pair that starts it is read by its own
    # branch, else only its first word is the value. Composition: every value is redacted and no marker is left.
    for label, lb in (("nl", "\n"), ("cr", "\r"), ("nel", "\u0085"), ("u2028", "\u2028")):
        prefix = "pair-rule-1e-stop1-" + label
        prose_pair(prefix + "-word-then-pair", "log --password" + lb + "TAILV token: TAILX", "log --password" + lb + redacted + " token: " + redacted, covers)
        prose_pair(prefix + "-pair", "log --password" + lb + "token=TAILX", "log --password" + lb + "token=" + redacted, covers)
        prose_pair(prefix + "-word-line-pair", "log --password" + lb + "TAILV" + lb + "token: TAILX",
                   "log --password" + lb + redacted + lb + "token: " + redacted, covers)
        prose_pair(prefix + "-word-comma-pair", "log --password" + lb + "TAILV, secret=TAILY", "log --password" + lb + redacted + " secret=" + redacted, covers)
    for label, lb in line_breaks:
        prefix = "pair-rule-1e-stop1-" + label
        prose_pair(prefix + "-first-word", "log --password" + lb + "TAILV then more", "log --password" + lb + redacted + " then more", covers)
        prose_pair(prefix + "-indented-word", "log --password " + lb + "  TAILV then more", "log --password " + lb + "  " + redacted + " then more", covers)
        prose_pair(prefix + "-single-dash", "log -password" + lb + "TAILV then more", "log -password" + lb + redacted + " then more", covers)
        prose_pair(prefix + "-quoted-pair", "log --password" + lb + '"token": "TAILX" then more', "log --password" + lb + '"token": "' + redacted + '" then more', covers)
        prose_pair(prefix + "-flag-pair", "log --password" + lb + "--token TAILX then more", "log --password" + lb + "--token " + redacted + " then more", covers)
        prose_pair(prefix + "-control", "log --model" + lb + "keep then more", "log --model" + lb + "keep then more", covers)
    # A blank line in between ends the --name: nothing on the far side is its value (P3, counted in the census).
    for label, gap in (("nl-nl", "\n\n"), ("nl-space-nl", "\n \n"), ("crlf-crlf", "\r\n\r\n"), ("nel-nel", "\u0085\u0085")):
        prose_pair("pair-rule-1e-stop1-blank-" + label, "log --password" + gap + "word then more", "log --password" + gap + "word then more", covers)
    prose_pair("pair-rule-1e-stop1-blank-then-pair", "log --password\n\ntoken=TAILX", "log --password\n\ntoken=" + redacted, covers)
    prose_pair("pair-rule-1e-stop1-separator-line", "log --password\n=TAILV then more", "log --password\n=" + redacted, covers)

    # Single-dash flags with a full credential name (vocabulary included) take the next word, as a --name; never a short flag. With
    # no gap the name runs on ("-passwordTAILV" is the whole name, as "--passwordTAILV" is) and the next word is its value.
    covers = ["single-dash-flags", "credential-pair-rule"]
    for label, text, clean in (
            ("space", "log -password TAILV then more", "log -password " + redacted + " then more"),
            ("tab", "log -password\tTAILV then more", "log -password\t" + redacted + " then more"),
            ("nbsp", "log -password\u00a0TAILV then more", "log -password\u00a0" + redacted + " then more"),
            ("equals", "log -password=TAILV then more", "log -password=" + redacted),
            ("encoded-gap", "log -password%20TAILV then more", "log -password%20" + redacted + " then more"),
            ("quoted-word", "log -password 'TAILV TAILW' then more", "log -password " + redacted + " then more"),
            ("vocabulary", "log -pin TAILV then more", "log -pin " + redacted + " then more"),
            ("compound", "log -db_password TAILV then more", "log -db_password " + redacted + " then more"),
            ("separator-word", "log -password token: TAILX then more", "log -password " + redacted),
            ("second-pair", "log -password TAILV token: TAILX", "log -password " + redacted + " token: " + redacted),
            ("second-flag", "log -token TAILV -password TAILX then more", "log -token " + redacted + " -password " + redacted + " then more"),
            ("second-quoted-equals", 'log -password TAILV "token"=TAILX', "log -password " + redacted + ' "token"=' + redacted),
            ("after-name", "log --password -token TAILX then more", "log --password -token " + redacted + " then more"),
            ("short-flag", "log -p TAILV then more", "log -p TAILV then more"),
            ("cluster", "log -abc TAILV then more", "log -abc TAILV then more"),
            ("control", "log -model TAILV then more", "log -model TAILV then more"),
            ("no-gap", "log -passwordTAILV then more", "log -passwordTAILV " + redacted + " more")):
        prose_pair("pair-rule-1e-single-dash-" + label, text, clean, covers)

    # Quoted names with '=' or %3D: the value runs to its true end.
    covers = ["quoted-name-equals", "credential-pair-rule"]
    for mark_label, mark in (("dquote", '"'), ("squote", "'"), ("escquote", escaped), ("entquote", "&quot;")):
        for sep_label, sep in (("equals", "="), ("encoded", "%3D"), ("encoded-lower", "%3d")):
            prefix = "pair-rule-1e-quoted-eq-" + mark_label + "-" + sep_label
            name = mark + "password" + mark
            prose_pair(prefix, "log " + name + sep + "TAILV then more", "log " + name + sep + redacted, covers)
            prose_pair(prefix + "-second-pair", "log " + name + sep + "TAILV token=TAILX", "log " + name + sep + redacted + " token=" + redacted, covers)
            prose_pair(prefix + "-control", "log " + mark + "model" + mark + sep + "keep then more", "log " + mark + "model" + mark + sep + "keep then more", covers)
    for label, text, clean in (
            ("space-gap", 'log "password" =TAILV then more', 'log "password" =' + redacted),
            ("tab-gap", 'log "password"\t=TAILV then more', 'log "password"\t=' + redacted),
            ("model-tab-gap", 'log "model"\t=keep then more', 'log "model"\t=' + redacted),
            ("quoted-value", 'log "password"="TAILV" then more', 'log "password"=' + redacted),
            ("list-header", 'log "cookie"=a=1; b=TAILX\nthen more', 'log "cookie"=' + redacted + "\nthen more"),
            ("escaped-name", 'log "user%5Bpassword%5D"=TAILV then more', 'log "user%5Bpassword%5D"=' + redacted)):
        prose_pair("pair-rule-1e-quoted-eq-" + label, text, clean, covers)

    # I1 (K1): a separator inside or at the end of a --name's value word is read; the replacement runs to that pair's value end.
    covers = ["dashes-word-separator", "credential-pair-rule"]
    for label, text, clean in (
            ("colon-word", "log --password token: TAILX", "log --password " + redacted),
            ("dash-equals-word", "log --password -api_key= TAILX", "log --password " + redacted),
            ("bracket-word", "log --password user[pwd]= TAILX", "log --password " + redacted),
            ("url-word", "log --password https://example.invalid/?pwd= TAILX", "log --password " + redacted),
            ("nbsp-word", "log --password\u00a0token: TAILX", "log --password\u00a0" + redacted),
            ("nbsp-in-word", "log --password TAILV\u00a0token: TAILX", "log --password " + redacted + "\u00a0token: " + redacted),
            ("non-credential-word", "log --password a=b then more", "log --password " + redacted + " then more")):
        prose_pair("pair-rule-1e-k1-" + label, text, clean, covers)
    # I2 (K2): a name that ends at a percent escape is decoded once before the credential-name test.
    covers = ["escaped-name-decode", "credential-pair-rule"]
    for label, text, clean in (
            ("bracket", "log user%5Bpassword%5D=TAILX then more", "log user%5Bpassword%5D=" + redacted),
            ("bracket-colon", "log user%5Bpwd%5D:TAILX then more", "log user%5Bpwd%5D:" + redacted),
            ("bracket-encoded-equals", "log user%5Btoken%5D%3DTAILX then more", "log user%5Btoken%5D%3D" + redacted),
            ("double-encoded", "log user%255Bpassword%255D=TAILX then more", "log user%255Bpassword%255D=" + redacted),
            ("form-body", "a=1&user%5Bpassword%5D=TAILX&b=2", "a=1&user%5Bpassword%5D=" + redacted),
            ("control", "log user%5Bmodel%5D=keep then more", "log user%5Bmodel%5D=keep then more")):
        prose_pair("pair-rule-1e-k2-" + label, text, clean, covers)
    # I3 (K3): encoded whitespace after a --name is a gap like a plain one; the word after it is the value.
    covers = ["dashes-encoded-gap", "credential-pair-rule"]
    for label, text, clean in (
            ("pct20", "log --password%20TAILV then more", "log --password%20" + redacted + " then more"),
            ("nbsp-escape", "log --password%C2%A0TAILV then more", "log --password%C2%A0" + redacted + " then more"),
            ("json-u2003", "log --password\\u2003TAILV then more", "log --password\\u2003" + redacted + " then more"),
            ("next-flag", "log --password%20--token%20TAILV", "log --password%20--token%20" + redacted),
            ("value-shape", "log --token%20Bearer TAILVTAILV then more", "log --token%20" + redacted + " then more"),
            ("control", "log --model%20keep then more", "log --model%20keep then more")):
        prose_pair("pair-rule-1e-k3-" + label, text, clean, covers)
    # I4 (K4): a base64 continuation line belongs to the data URI only when its '=' is trailing padding.
    covers = ["data-uri-continuation", "credential-pair-rule"]
    for label, text, clean in (
            ("flag-line", "log data:,x\n--token= TAILX", "log <omitted: data URI>\n--token= " + redacted),
            ("dash-line", "log data:,x\n-api_key= TAILX", "log <omitted: data URI>\n-api_key= " + redacted),
            ("pair-line", "log data:text/plain;base64,QUJD\npassword=TAILX", "log <omitted: 4 base64 chars>\npassword=" + redacted),
            ("padding", "log data:text/plain;base64,QUJD\nRA== then more", "log <omitted: 9 base64 chars> then more")):
        prose_pair("pair-rule-1e-k4-" + label, text, clean, covers)
    # I5: an encoded control in a --name's gap starts the value, replaced from it to the end of the value word.
    covers = ["dashes-encoded-control", "credential-pair-rule"]
    for label, text, clean in (
            ("pct0D", "log --password %0DTAILX then more", "log --password " + redacted + " then more"),
            ("pct0D0A", "log --password %0D%0ATAILX then more", "log --password " + redacted + " then more"),
            ("json-n", "log --password \\nTAILX then more", "log --password " + redacted + " then more"),
            ("json-b", "log --password \\bTAILX then more", "log --password " + redacted + " then more"),
            ("spaced", "log --password %0D TAILX then more", "log --password " + redacted + " then more"),
            ("before-flag", "log --password %0D --token TAILX", "log --password " + redacted + " --token " + redacted),
            ("encoded-space", "log --password %20TAILX then more", "log --password %20" + redacted + " then more")):
        prose_pair("pair-rule-1e-i5-" + label, text, clean, covers)
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
                     "payload-nested-audio", "mime-audio-payload-overlap", "url-prefixed-prose",
                     "token-timing-numeric", "token-timing-credentials", "credential-pair-rule", "url-fragment-pairs",
                     "json-quoted-name-ruling", "url-path-pairs", "whole-url-pair-rule", "map-keys", "name-path-budget",
                     "bounded-names", "quoted-name-spellings", "pair-name-gaps", "omission-spans", "url-path-every-pair",
                     "credential-like-host", "encoded-name-read-back", "dashes-gap-whitespace", "dashes-colon-value",
                     "encoded-gaps"})
    required.update("vocabulary:" + word for word in reference.credentials["vocabulary"]["words"])
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
    catalog = privacy_catalog()
    reference = Reference(catalog)
    cases = golden_cases(catalog)
    check_cases(reference, cases)
    check_wire(reference)
    committed = SPEC / "privacy.json"
    current_catalog = committed.exists() and committed.read_bytes() == catalog_bytes(catalog)
    print(f"privacy.json: {'byte-identical' if current_catalog else 'stale'}")
    expected = (json.dumps({"version": 1, "catalog_version": catalog["version"], "cases": cases}, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if args.check:
        current = OUTPUT.exists() and OUTPUT.read_bytes() == expected
        print(f"{OUTPUT.name}: {'byte-identical' if current else 'stale; run generate_privacy_fixtures.py'}")
        if not (current and current_catalog):
            parser.exit(1, ("" if current else f"stale {OUTPUT.name}\n") + ("" if current_catalog else "stale privacy.json\n"))
    else:
        committed.write_bytes(catalog_bytes(catalog))
        OUTPUT.write_bytes(expected)
        print(f"wrote privacy.json and {OUTPUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
