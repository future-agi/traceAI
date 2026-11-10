"""Bounded, by-value image redaction for the FI export view.

Strict JSON owns its string tokens before any URI decision. Raw occurrences use
exact edits; decoded-only occurrences and nonconvergent values replace their
whole owning scope. No object round trip, public counter or caller mutation.
"""

import logging
import re
import unicodedata
from array import array
from dataclasses import dataclass
from html import unescape
from html.entities import html5
from typing import Any, List, Optional, Tuple

_IMAGE_TYPES = frozenset(("image", "image_url", "input_image"))
_MIME_KEYS = frozenset(("media_type", "mime_type", "mimeType", "mediaType"))
_PAYLOAD_KEYS = frozenset(("data", "bytes", "content", "image", "image_url", "url", "base64", "b64_json"))
_SOURCE_KEYS = frozenset(("source", "inlineData", "inline_data", "image_url", "image"))
_JSON_ATOM = re.compile(r"(?:true|false|null|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)")
_DATA_START = re.compile(r"data:", re.IGNORECASE | re.ASCII)
_FLAG = re.compile(r";\s*(?ai:base64)\s*,")
_SLASH_ATOMS = re.compile(r"(?:\\/)+")
_TRANSPORT_SPECIAL = re.compile(r"(?:\\/)+|[\\%&]|[^\x00-\x7f]")
_RUN_PART = re.compile(r"[A-Za-z0-9+/=_-]+|(?:\\/)+|[\\%&]")
_PAYLOAD_RUN = re.compile(r"[A-Za-z0-9+/=_-]+")
_MIME_TOKEN_PART = re.compile(r"[!#$'*+.^_`|~0-9A-Za-z-]+")
_FENCE_DEPTH = 16
_STRING_SPECIAL = re.compile(r'["\\\x00-\x1f]')
_HEX = frozenset("0123456789abcdefABCDEF")
_SIMPLE = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
_WORK_FACTOR = 32
_SCAN_CAP = 1_048_576
_BACKSTOP_HEADER_WINDOW = 1024
_DECODE_PASSES = 3
_WHOLE = "[image-redacted]"
_MARK = "__REDACTED__"
_ZERO_DATA = re.compile(r"data:", re.IGNORECASE | re.ASCII)
_ZERO_IMAGE = re.compile(r"image/", re.IGNORECASE | re.ASCII)
_CAP_INTEGER = re.compile(r"[+-]?[0-9]+")
_logger = logging.getLogger(__name__)


def _normalize_image_cap(value: Any) -> int:
    """An invalid cap never authorizes export, crashes init, or logs its value."""
    if isinstance(value, str):
        value = value.strip()
        if _CAP_INTEGER.fullmatch(value) is not None:
            try:
                value = int(value, 10)
            except ValueError:
                # Python may reject extremely long decimal spellings.
                pass
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    _logger.warning("Invalid base64 image cap; using 0.")
    return 0


class _WorkExhausted(Exception):
    pass


class _Work:
    def __init__(self, length: int) -> None:
        self.remaining = _WORK_FACTOR * length

    def spend(self, units: int) -> None:
        if units > self.remaining:
            raise _WorkExhausted()
        self.remaining -= units


@dataclass
class _View:
    text: str
    starts: Optional[array] = None
    ends: Optional[array] = None
    raw_start: int = 0
    raw_end: int = 0

    def start(self, i: int) -> int:
        return self.starts[i] if self.starts is not None else self.raw_start + i

    def end(self, i: int) -> int:
        return self.ends[i] if self.ends is not None else self.raw_start + i + 1


class _Edits:
    """Direct-address union. Whole-scope B dominates all contained A edits."""
    def __init__(self, length: int) -> None:
        self.length = length
        self.ends: Optional[array] = None
        self.whole: Optional[bytearray] = None

    def add(self, start: int, end: int, whole: bool = False) -> None:
        if start >= end:
            return
        if self.ends is None:
            self.ends = array("I", [0]) * self.length
            self.whole = bytearray(self.length)
        self.ends[start] = max(self.ends[start], end)
        if whole:
            self.whole[start] = 1

    def apply_counted(self, text: str) -> Tuple[str, int]:
        if self.ends is None:
            return text, 0
        parts: List[str] = []
        cursor = i = count = 0
        while i < len(text):
            end = self.ends[i]
            if end <= i:
                i += 1
                continue
            whole = bool(self.whole[i])
            begin = i
            i += 1
            while i < end:
                end = max(end, self.ends[i])
                whole = whole or bool(self.whole[i])
                i += 1
            parts.extend((text[cursor:begin], _WHOLE if whole else _MARK))
            cursor = end
            count += 1
        parts.append(text[cursor:])
        return "".join(parts), count


def _digit(c: str) -> int:
    code = ord(c)
    if 65 <= code <= 90:
        return code - 65
    if 97 <= code <= 122:
        return code - 71
    if 48 <= code <= 57:
        return code + 4
    if c in "+-":
        return 62
    if c in "/_":
        return 63
    return -1


def _is_base64_text(text: str) -> bool:
    padding = False
    digits = 0
    for c in text:
        if c in "\r\n":
            continue
        if c == "=":
            padding = True
            continue
        if padding or _digit(c) < 0:
            return False
        digits += 1
    return digits > 0


def _has_image_signature(text: str) -> bool:
    head = bytearray()
    buffer = bits = 0
    for c in text:
        if c in "\r\n":
            continue
        digit = _digit(c)
        if digit < 0:
            break
        buffer = ((buffer << 6) | digit) & 0xFFFFFF
        bits += 6
        if bits >= 8:
            bits -= 8
            head.append((buffer >> bits) & 255)
            if len(head) == 12:
                break
    return (head.startswith(b"\x89PNG\r\n\x1a\n") or head.startswith(b"\xff\xd8\xff")
            or head.startswith(b"GIF8") or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"))


# Unicode Default_Ignorable_Code_Point (DerivedCoreProperties), in addition to Cf.
_DEFAULT_IGNORABLE = re.compile("[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f\u202a-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufff8\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff]")


def _invisible(c: str) -> bool:
    code = ord(c)
    return (unicodedata.category(c) == "Cf" or _DEFAULT_IGNORABLE.match(c) is not None or code == 0x034F
            or 0x180B <= code <= 0x180F or 0xFE00 <= code <= 0xFE0F
            or 0xE0100 <= code <= 0xE01EF)


def _json_atom(text: str, i: int) -> Optional[Tuple[str, int]]:
    if text[i] != "\\" or i + 1 >= len(text):
        return None
    c = text[i + 1]
    if c in _SIMPLE:
        return _SIMPLE[c], i + 2
    digits = text[i + 2:i + 6]
    if c != "u" or len(digits) != 4 or any(d not in _HEX for d in digits):
        return None
    code = int(digits, 16)
    end = i + 6
    if 0xD800 <= code <= 0xDBFF and text[end:end + 2] == "\\u":
        low = text[end + 2:end + 6]
        if len(low) == 4 and all(d in _HEX for d in low):
            number = int(low, 16)
            if 0xDC00 <= number <= 0xDFFF:
                code = 0x10000 + ((code - 0xD800) << 10) + number - 0xDC00
                end += 6
    return chr(code), end


def _transport_atom(text: str, i: int) -> Optional[Tuple[str, int]]:
    c = text[i]
    if c == "\\":
        # Coalesce a dense slash run without changing its one-pass semantics.
        # _decode_pass retains the individual two-character raw coordinates.
        slashes = _SLASH_ATOMS.match(text, i)
        if slashes is not None:
            return "/" * ((slashes.end() - i) // 2), slashes.end()
        return _json_atom(text, i)
    if c == "%":
        def byte(at: int) -> int:
            pair = text[at + 1:at + 3]
            if text[at:at + 1] != "%" or len(pair) != 2 or any(d not in _HEX for d in pair):
                return -1
            return int(pair, 16)
        first = byte(i)
        if first < 0:
            return None
        width = (1 if first < 128 else 2 if 0xC2 <= first <= 0xDF
                 else 3 if 0xE0 <= first <= 0xEF else 4 if 0xF0 <= first <= 0xF4 else 0)
        if not width:
            return None
        data = [first]
        for j in range(1, width):
            following = byte(i + j * 3)
            if not 0x80 <= following <= 0xBF:
                return None
            data.append(following)
        try:
            return bytes(data).decode("utf-8"), i + width * 3
        except UnicodeDecodeError:
            return None
    if c != "&":
        return None
    if text[i + 1:i + 2] == "#":
        j = i + 2
        hexadecimal = text[j:j + 1] in ("x", "X")
        if hexadecimal:
            j += 1
        begin = j
        point = 0
        while j < len(text) and (text[j] in _HEX if hexadecimal else "0" <= text[j] <= "9"):
            point = min(0x110000, point * (16 if hexadecimal else 10) + int(text[j], 16))
            j += 1
        if begin == j:
            return None
        if text[j:j + 1] == ";":
            j += 1
        # Bounded integer spelling also preserves HTML's numeric-reference rules.
        return unescape("&#" + str(point) + ";"), j
    best = None
    for j in range(i + 1, min(len(text), i + 33)):
        c = text[j]
        if not (c.isascii() and c.isalnum()) and c != ";":
            break
        name = text[i + 1:j + 1]
        if name in html5:
            best = html5[name], j + 1
        if c == ";":
            break
    return best


def _backstop_unit(text: str, index: int, stop: int) -> Tuple[str, int]:
    """One real raw-run atom; retained for the structural resource controls."""
    atom = _transport_atom(text, index)
    return atom if atom is not None and atom[1] <= stop else (text[index], index + 1)


def _decode_pass(view: _View, work: _Work) -> _View:
    """One simultaneous transport pass; ordinary spans are copied in bulk."""
    text = view.text
    work.spend(len(text))
    if _TRANSPORT_SPECIAL.search(text) is None:
        return view
    parts: List[str] = []
    starts, ends = array("I"), array("I")
    cursor = 0
    changed = False

    def literal(begin: int, end: int) -> None:
        if begin == end:
            return
        parts.append(text[begin:end])
        if view.starts is None:
            starts.extend(range(view.raw_start + begin, view.raw_start + end))
            ends.extend(range(view.raw_start + begin + 1, view.raw_start + end + 1))
        else:
            starts.extend(view.starts[begin:end])
            ends.extend(view.ends[begin:end])

    for found in _TRANSPORT_SPECIAL.finditer(text):
        i = found.start()
        if i < cursor:
            continue
        literal(cursor, i)
        atom = _transport_atom(text, i)
        content, end = atom if atom is not None else (text[i], i + 1)
        kept = content if content.isascii() else "".join(c for c in content if not _invisible(c))
        changed = changed or kept != text[i:end]
        parts.append(kept)
        if len(kept) > 1 and text[i:i + 2] == "\\/":
            if view.starts is None:
                starts.extend(range(view.raw_start + i, view.raw_start + end, 2))
                ends.extend(range(view.raw_start + i + 2, view.raw_start + end + 1, 2))
            else:
                starts.extend(view.starts[i:end:2])
                ends.extend(view.ends[i + 1:end:2])
        else:
            starts.extend([view.start(i)] * len(kept))
            ends.extend([view.end(end - 1)] * len(kept))
        cursor = end
    if not changed:
        return view
    literal(cursor, len(text))
    return _View("".join(parts), starts, ends, view.raw_start, view.raw_end)


def _would_change(view: _View, work: _Work) -> bool:
    """Equality probe only: do not construct/search a fourth decoded view."""
    text = view.text
    work.spend(len(text))
    cursor = 0
    for found in _TRANSPORT_SPECIAL.finditer(text):
        i = found.start()
        if i < cursor:
            continue
        atom = _transport_atom(text, i)
        content, end = atom if atom is not None else (text[i], i + 1)
        if any(_invisible(c) for c in content) or content != text[i:end]:
            return True
        cursor = end
    return False


def _has_backstop_fence(text: str) -> bool:
    """Complete strict JSON only; no EOF-prefix or recovery certificates."""
    if not text.lstrip(" \t\r\n").startswith(("{", "[", '"')):
        return False
    stack: List[str] = []
    root = False

    def take_value() -> bool:
        nonlocal root
        if not stack:
            if root:
                return False
            root = True
        elif stack[-1] == "object-value":
            stack[-1] = "object-after"
        elif stack[-1] in ("array-first", "array-value"):
            stack[-1] = "array-after"
        else:
            return False
        return True

    i = 0
    while i < len(text):
        c = text[i]
        entry = stack[-1] if stack else None
        if c in " \t\r\n":
            i += 1
            continue
        if c == '"':
            if entry in ("object-first", "object-key"):
                stack[-1] = "object-colon"
            elif not take_value():
                return False
            i += 1
            closed = False
            while i < len(text):
                c = text[i]
                if c == '"':
                    i += 1
                    closed = True
                    break
                if ord(c) < 32:
                    return False
                if c == "\\":
                    atom = _json_atom(text, i)
                    if atom is None:
                        return False
                    i = atom[1]
                else:
                    i += 1
            if not closed:
                return False
            continue
        if c in "[{":
            if not take_value():
                return False
            stack.append("object-first" if c == "{" else "array-first")
        elif c == "}":
            if entry not in ("object-first", "object-after"):
                return False
            stack.pop()
        elif c == "]":
            if entry not in ("array-first", "array-after"):
                return False
            stack.pop()
        elif c == ":":
            if entry != "object-colon":
                return False
            stack[-1] = "object-value"
        elif c == ",":
            if entry == "object-after":
                stack[-1] = "object-key"
            elif entry == "array-after":
                stack[-1] = "array-value"
            else:
                return False
        else:
            atom = _JSON_ATOM.match(text, i)
            if atom is None or not take_value():
                return False
            i = atom.end()
            continue
        i += 1
    return root and not stack


@dataclass
class _Frame:
    parent: Optional["_Frame"] = None
    key: Optional[str] = None
    declared: bool = False
    image: bool = False


@dataclass
class _Token:
    view: _View
    damaged: bool
    frame: Optional[_Frame] = None
    key: Optional[str] = None

    def image(self) -> bool:
        return bool(self.frame is not None and self.frame.image and self.key in _PAYLOAD_KEYS)


def _declares(key: Optional[str], value: str) -> bool:
    return ((key == "type" and value in _IMAGE_TYPES)
            or (key == "modality" and value == "image")
            or (key in _MIME_KEYS and value.lower().startswith("image/")))


def _read_token(view: _View, quote: int) -> Tuple[_Token, int]:
    text = view.text
    i = quote + 1
    special = _STRING_SPECIAL.search(text, i)
    if special is not None and text[special.start()] == '"':
        end = special.start()
        start_raw, end_raw = view.end(quote), view.start(end)
        child = _View(text[i:end],
                      view.starts[i:end] if view.starts is not None else None,
                      view.ends[i:end] if view.ends is not None else None,
                      start_raw, end_raw)
        return _Token(child, False), end + 1
    parts: List[str] = []
    starts, ends = array("I"), array("I")
    damaged = False
    while i < len(text) and text[i] != '"':
        if ord(text[i]) < 32:
            damaged = True
        atom = _json_atom(text, i)
        if atom is None:
            damaged = damaged or text[i] == "\\"
            content, end = text[i], i + 1
        else:
            content, end = atom
        parts.append(content)
        starts.extend([view.start(i)] * len(content))
        ends.extend([view.end(end - 1)] * len(content))
        i = end
    if i == len(text):
        damaged = True
    start = view.end(quote)
    end = view.start(i) if i < len(text) else view.raw_end
    token = _Token(_View("".join(parts), starts, ends, start, end), damaged)
    return token, min(i + 1, len(text))


def _document_tokens(view: _View, work: _Work, maximum: int = -1) -> List[_Token]:
    """Positive declaration recovery, not a syntax certificate."""
    work.spend(len(view.text))
    text = view.text
    stack: List[Any] = []
    frames: List[_Frame] = []
    tokens: List[_Token] = []
    i = 0
    while i < len(text):
        entry = stack[-1] if stack else None
        top = entry if isinstance(entry, _Frame) else None
        c = text[i]
        if c == '"':
            token, end = _read_token(view, i)
            following = end
            while following < len(text) and text[following].isspace():
                following += 1
            if top is not None and text[following:following + 1] == ":":
                top.key = token.view.text
            else:
                if top is not None:
                    token.key, token.frame = top.key, top
                    top.declared = top.declared or _declares(top.key, token.view.text)
                    top.key = None
            value = token.view.text
            # These tokens cannot change, grow, contain nested syntax or exceed
            # the cap. Declarations were recorded above even if not queued.
            if not (len(value) <= maximum and value.isascii()
                    and not any(c in value for c in "\\%&\"[{")):
                tokens.append(token)
            i = end
            continue
        if c == "{":
            frame = _Frame(parent=top if top is not None and top.key in _SOURCE_KEYS else None)
            if top is not None:
                top.key = None
            frames.append(frame)
            stack.append(frame)
        elif c == "[":
            if top is not None:
                top.key = None
            if isinstance(entry, int):
                stack[-1] = entry + 1
            else:
                stack.append(1)
        elif c in "}]":
            if isinstance(entry, int) and entry > 1:
                stack[-1] = entry - 1
            elif stack:
                stack.pop()
        elif c == "," or (c != ":" and not c.isspace()):
            if top is not None:
                top.key = None
        i += 1
    for frame in frames:
        frame.image = frame.declared or bool(frame.parent is not None and frame.parent.image)
    return tokens


def _compose(child: _View, base: _View) -> _View:
    """Compose dense coordinates once, with no ancestor search or recursion."""
    start = base.start(child.raw_start) if child.raw_start < len(base.text) else base.raw_end
    end = base.end(child.raw_end - 1) if child.raw_end > child.raw_start else start
    if child.starts is None and base.starts is None:
        return _View(child.text, raw_start=start, raw_end=end)
    return _View(child.text,
                 array("I", (base.start(child.start(i)) for i in range(len(child.text)))),
                 array("I", (base.end(child.end(i) - 1) for i in range(len(child.text)))), start, end)


def _prefixes(text: str, work: _Work) -> List[Tuple[int, int]]:
    work.spend(len(text))
    result = []
    for found in _DATA_START.finditer(text):
        i = found.end()
        while i < len(text) and (text[i].isspace() or _invisible(text[i])):
            i += 1
        if text[i:i + 6].lower() == "image/":
            result.append((found.start(), i + 6))
    return result


def _run_ends(text: str, raw: bool, work: _Work) -> array:
    """Disjoint runs and atoms, with bulk endpoint fills (no suffix retries)."""
    work.spend(len(text))
    ends = array("I", [0]) * (len(text) + 1)
    begin = following = last = 0

    def finish() -> None:
        if last > begin:
            ends[begin:last] = array("I", [last]) * (last - begin)

    pattern = _RUN_PART if raw else _PAYLOAD_RUN
    cursor = 0
    while cursor < len(text):
        found = pattern.search(text, cursor)
        if found is None:
            break
        i = found.start()
        end = found.end()
        content = found.group()
        if raw and content[0] in "\\%&":
            content, end = _backstop_unit(text, i, len(text))
        # Resume at the consumed atom's endpoint. A finditer match can start
        # inside that atom and extend beyond it (e.g. u0051UJD or 2FQUJD);
        # skipping that match would also discard the ordinary payload tail.
        cursor = end
        if not content or _PAYLOAD_RUN.fullmatch(content) is None:
            finish()
            begin = following = last = end
            continue
        if i != following:
            finish()
            begin = last = i
        following = end
        # R-WS treats all five ASCII whitespace characters as separators.
        # The chain table, rather than the first A range, owns continuations.
        last = end
    finish()
    return ends


def _chain_counts(view: _View, work: _Work) -> Optional[Tuple[array, array]]:
    """R-WS joined lengths, one suffix query per header or first-run end.

    Separators are exactly forgiving-base64 ASCII whitespace, not str.isspace().
    Transport atoms and ignorables are handled by the existing decoded views.
    """
    text = view.text
    if not any(c in text for c in " \t\r\n\f"):
        return None
    work.spend(len(text))
    counts = array("I", [0]) * (len(text) + 1)
    raw_counts = array("I", [0]) * (len(text) + 1) if view.starts is not None else counts
    parts = list(re.finditer(r"[A-Za-z0-9+/=_-]+|[ \t\r\n\f]+", text))
    following = len(text)
    total = raw_total = 0
    for part in reversed(parts):
        begin, end = part.span()
        if end != following:
            total = raw_total = 0
        size = end - begin
        if text[begin] in " \t\r\n\f":
            counts[begin:end] = array("I", [total]) * size
            if raw_counts is not counts:
                raw_counts[begin:end] = array("I", [raw_total]) * size
        else:
            counts[begin:end] = array("I", range(total + size, total, -1))
            total += size
            if raw_counts is not counts:
                # D2 applies to alphabet atoms, not whitespace spellings. Union
                # overlapping maps so a multi-character reference counts once.
                right = view.end(end - 1)
                for i in range(end - 1, begin - 1, -1):
                    raw_total += max(0, min(right, view.end(i)) - view.start(i))
                    raw_counts[i] = raw_total
                    right = view.start(i)
        following = begin
    return counts, raw_counts


def _mime_ends(text: str, work: _Work, passes: int = 0,
               transport: Optional[_View] = None, source: str = "") -> array:
    """C2 suffix certificates from RAW delimiters, never decoded quote state.

    Parameters decode independently with the same atom decoder as the views.
    An entity's terminating semicolon belongs to that atom, not a parameter.
    Quoted-pairs are consumed only by the raw double-quote closing table.
    R-TD additionally admits decoded separators, but only where provenance
    proves a transport atom, and raw quoted-string ownership is still retained.
    """
    work.spend(len(text))
    n = len(text)
    stop = text.rfind(",") + 1
    positive = array("I", [0]) * (n + 1)
    if not stop:
        return positive
    moved = None
    raw_close = None
    inverse = None
    if transport is not None:
        moved = bytearray(stop + 1)
        for found in re.finditer(r"[;,]", text[:stop]):
            i = found.start()
            begin, end = transport.start(i), transport.end(i)
            moved[i] = end - begin != 1 or source[begin:end] != text[i]
        if not any(moved):
            return positive
        if '"' in text:
            work.spend(len(source))
            raw_stop = len(source)
            raw_close = array("I", [raw_stop]) * (raw_stop + 2)
            for i in range(raw_stop - 1, -1, -1):
                raw_close[i] = (raw_close[i + 2] if source[i] == "\\" and i + 1 < raw_stop
                                else i if source[i] == '"' else raw_close[i + 1])
            inverse = {transport.end(i): i for i in range(stop)}
    flags = set()
    param = None
    consumed = 0
    for found in re.finditer(r'[;,"&]', text[:stop]):
        i = found.start()
        if i < consumed:
            continue
        if text[i] == "&":
            atom = _transport_atom(text, i)
            if atom is not None:
                consumed = atom[1]
            continue
        if param is not None and text[i] in ";,":
            value = text[param + 1:i]
            v = _View(value, raw_end=len(value))
            for _ in range(passes):
                after = _decode_pass(v, work)
                if after is v:
                    break
                v = after
            if v.text.strip().lower() == "base64":
                flags.add(param)
        param = i if text[i] == ";" else None
    close = array("I", [stop]) * (stop + 2)
    for i in range(stop - 1, -1, -1):
        close[i] = (close[i + 2] if text[i] == "\\" and i + 1 < stop
                    else i if text[i] == '"' else close[i + 1])
    comma = array("I", [0]) * (stop + 1)
    for i in range(stop - 1, -1, -1):
        c = text[i]
        if c == ",":
            comma[i] = i + 1
        elif c == '"':
            end = close[i + 1]
            if raw_close is not None:
                raw_quote = transport.end(i) - 1
                if source[raw_quote:raw_quote + 1] == '"':
                    # A raw backslash-quote can neither close an enclosing raw
                    # string nor expose its internal separators after decoding.
                    end = inverse.get(raw_close[raw_quote + 1] + 1, stop)
            if end < stop:
                comma[i], positive[i] = comma[end + 1], positive[end + 1]
                if moved is not None:
                    moved[i] = moved[end + 1]
        else:
            comma[i], positive[i] = comma[i + 1], positive[i + 1]
            if moved is not None:
                moved[i] |= moved[i + 1]
            if i in flags:
                positive[i] = comma[i]
    if moved is not None:
        for i in range(stop):
            if not moved[i]:
                positive[i] = 0
    return positive


def _mime_subtype(text: str, begin: int, work: _Work) -> Tuple[int, bool]:
    """Locate an atom-owned subtype boundary, without decoding another view.

    A changing spelling cannot certify raw A. Only an admitted decoded view
    can recognize it; a reference's semicolon is never a token boundary.
    """
    i = begin
    changing = False
    while i < len(text):
        part = _MIME_TOKEN_PART.match(text, i)
        if part is not None:
            i = part.end()
            continue
        c = text[i]
        if c in ";,":
            work.spend(i - begin)
            return (i if i > begin else 0), changing
        if c.isspace():
            # Preserve Python's literal C2 trailing header whitespace. This is
            # not permission to split a subtype at an interior decoded space.
            end = i
            while end < len(text) and text[end].isspace():
                end += 1
            work.spend(end - begin)
            return (end if i > begin and text[end:end + 1] in (";", ",") else 0), changing
        atom = _transport_atom(text, i) if c in "\\%&" else None
        if atom is not None:
            changing = changing or atom[0] != text[i:atom[1]]
            i = atom[1]
        elif c in "%&":
            i += 1
        elif _invisible(c):
            changing = True
            i += 1
        else:
            break
    work.spend(i - begin)
    return 0, changing


def _occurrences(view: _View, maximum: int, raw: bool, work: _Work,
                 mime: array, source: str = "") -> List[Tuple[int, int, int, bool]]:
    """C1 UNION raw C2 UNION transported-delimiter MIME, with R-WS chains."""
    text = view.text
    prefixes = _prefixes(text, work)
    if not prefixes:
        return []
    runs = _run_ends(text, raw, work)
    chains = _chain_counts(view, work)
    transported = _mime_ends(text, work, transport=view, source=source) if not raw else None
    flags = [(m.start(), m.end()) for m in _FLAG.finditer(text)]
    flag_index = {end: i for i, (_, end) in enumerate(flags)}
    # Raw comma endpoints -> this view's endpoints. One linear inverse map;
    # no binary searches or rescans per overlapping prefix.
    inverse = None
    if view.ends is not None:
        inverse = {end: i + 1 for i, end in enumerate(view.ends)}
    facts = {}

    def header_facts(header: int) -> Tuple[int, int, int, int, int]:
        cached = facts.get(header)
        if cached is None:
            end = runs[header]
            cached = (end, view.end(end - 1) if end > header else 0,
                      view.end(header - 1),
                      max(chains[0][end], chains[1][end]) if chains is not None else 0,
                      max(chains[0][header], chains[1][header]) if chains is not None else 0)
            facts[header] = cached
        return cached

    result: List[Tuple[int, int, int, bool]] = []
    low = high = eligible = 0
    for prefix_index, (start, prefix_end) in enumerate(prefixes):
        while low < len(flags) and flags[low][0] < prefix_end:
            low += 1
        high = max(high, low)
        while high < len(flags) and flags[high][0] <= start + _BACKSTOP_HEADER_WINDOW:
            high += 1
        raw_start = view.start(start)
        subtype_end, changing = _mime_subtype(text, prefix_end, work)
        admit = not changing
        ambiguous = not subtype_end
        if not raw:
            if subtype_end:
                # Raw C2 structure must end at a real raw delimiter, not inside
                # a decoded reference or a transported separator (R-TD below).
                boundary = view.start(subtype_end)
                admit = admit and (view.end(subtype_end) == boundary + 1
                                   and source[boundary:boundary + 1] == text[subtype_end])
            elif _mime_subtype(source, view.end(prefix_end - 1), work)[1]:
                admit = False
        raw_header = mime[view.end(prefix_end - 1)] if admit else 0
        if ambiguous and raw_header:
            # A malformed standalone subtype (p ng, p/ng) is not genuine
            # MIME in ANY raw input, including a rebased decoded view. Keep
            # the existing unseparable multiple-prefix B (PTC-019/020), but
            # never let a lone invalid subtype manufacture a C2 certificate.
            # Prefixes are ordered; this is one O(1) query, not a suffix scan.
            following = prefix_index + 1
            if (following == len(prefixes)
                    or view.start(prefixes[following][0]) >= raw_header):
                raw_header = 0
        header = inverse.get(raw_header, 0) if inverse is not None else raw_header
        moved_header = (transported[prefix_end] if transported is not None
                        and not changing and subtype_end else 0)
        # Retain the legacy broad literal C2 positive (PTC-019/020), but an
        # unseparable noncanonical subtype can only certify whole B, never A.
        # Transformed invalid subtypes above cannot manufacture a C2 flag.
        # Charge the ORIGINAL set, including every candidate pruned below.
        # Exhaustion and an early whole match both select the same owning B.
        units = high - low
        if header and (ambiguous or not low <= flag_index.get(header, -1) < high):
            units += 1
        work.spend(units + bool(moved_header))
        eligible = max(eligible, low)
        if chains is None:
            # Without R-WS, a noncounting C1 candidate has no effect. Its
            # extents cannot increase as the prefix advances in either view.
            while eligible < high:
                flag_end = flags[eligible][1]
                end, raw_end, _, _, _ = header_facts(flag_end)
                if end > flag_end and max(end - start, raw_end - raw_start) > maximum:
                    break
                eligible += 1
        else:
            eligible = low
        headers = {(flags[i][1], False) for i in range(eligible, high)}
        if header:
            headers.add((header, ambiguous))
        if moved_header:
            headers.add((moved_header, True))
        for header_end, transported_header in headers:
            end, raw_end, raw_header_end, remainder, joined = header_facts(header_end)
            counting = (end > header_end and
                        max(end - start, raw_end - raw_start) > maximum)
            whole = counting and transported_header
            if chains is not None:
                if counting:
                    # R-WS(i): preserve an exact A cut for a large first run
                    # only if its entire joined remainder is at most the cap.
                    whole = whole or remainder > maximum
                elif chains[0][header_end]:
                    # R-WS(ii): whitespace itself is not payload length. The
                    # header retains D2's own-scope raw/decoded maximum.
                    header_length = max(header_end - start, raw_header_end - raw_start)
                    whole = header_length + joined > maximum
            if whole:
                # B dominates every exact range in this owning Q1 scope.
                return [(start, header_end, max(end, header_end), True)]
            if counting:
                result.append((start, header_end, end, False))
    return result


def _inspect_declared(token: _Token, maximum: int, work: _Work) -> bool:
    view = token.view
    for turn in range(_DECODE_PASSES + 1):
        work.spend(len(view.text))
        if len(view.text) > maximum and (token.damaged or _is_base64_text(view.text)):
            return True
        if turn == _DECODE_PASSES:
            return False
        following = _decode_pass(view, work)
        if following is view:
            return False
        view = following
    return False


def _zero_scope_mentions_image(text: str, work: _Work) -> bool:
    """F2 zero-cap trigger (ruling r2): data:, then image/, then a comma.

    Q1 ownership is resolved before this helper; only this scope view is
    searched. Whitespace and ignorable characters cannot shelter a mention.
    RFC 2397 puts a comma before any payload, so a view with no comma after
    image/ carries none. Leftmost matches leave the longest suffix: linear.
    """
    work.spend(2 * len(text))
    compact = "".join(c for c in text if not c.isspace() and not _invisible(c))
    data = _ZERO_DATA.search(compact)
    if data is None:
        return False
    image = _ZERO_IMAGE.search(compact, data.end())
    return image is not None and compact.find(",", image.end()) >= 0


def _zero_scope_has_image(views: List[_View], image: bool, damaged: bool,
                          work: _Work) -> bool:
    for view in views:
        text = view.text
        if _zero_scope_mentions_image(text, work):
            return True
        # Retain the positive declared/signature-only paths for bare image
        # payloads, without treating arbitrary base64-looking prose as an image.
        work.spend(len(text))
        if text and ((image and damaged)
                or ((image or _has_image_signature(text)) and _is_base64_text(text))):
            return True
        if text.lstrip().startswith(("{", "[")):
            for token in _document_tokens(view, work):
                if token.image() and token.view.text and _inspect_declared(token, 0, work):
                    # This is recovery in an unfenced scope: do not invent a
                    # JSON partition for malformed input. Q1 was handled first.
                    return True
    return False


def _inspect_scope(base: _View, maximum: int, image: bool, damaged: bool,
                   work: _Work, edits: _Edits, remaining: int,
                   fallback: Tuple[int, int], depth: int = 0,
                   verify_output: bool = True) -> Optional[Tuple[List[_Token], int, bool]]:
    raw = base.text
    if len(raw) > _SCAN_CAP:
        edits.add(base.raw_start, base.raw_end, True)
        return None
    if not raw:
        return None
    views = [_View(raw, raw_end=len(raw))]
    stable = False
    for turn in range(remaining + 1):
        view = views[-1]
        work.spend(len(view.text))
        if _has_backstop_fence(view.text):
            tokens = _document_tokens(view, work, maximum)
            transparent = view.text.lstrip(" \t\r\n").startswith('"')
            for token in tokens:
                token.view = _compose(token.view, base)
                if transparent and image:
                    token.frame, token.key = _Frame(image=True), "data"
                    token.damaged = token.damaged or damaged
            return tokens, remaining - turn, bool(turn and transparent)
        if turn == remaining:
            break
        following = _decode_pass(view, work)
        if following is view:
            stable = True
            break
        views.append(following)
    if not stable and _would_change(views[-1], work):
        edits.add(fallback[0], fallback[1], True)
        return None

    if maximum == 0:
        if _zero_scope_has_image(views, image, damaged, work):
            edits.add(fallback[0], fallback[1], True)
        return None

    # Avoid allocating header/run tables for ordinary text and declared blobs.
    has_prefix = any(_prefixes(v.text, work) for v in views)
    raw_matches = []
    if has_prefix:
        raw_mime = _mime_ends(raw, work)
        raw_matches = _occurrences(views[0], maximum, True, work, raw_mime)
        by_header = {}
        for start, header_end, end, whole in raw_matches:
            if whole:
                edits.add(fallback[0], fallback[1], True)
                return None
            by_header[(start, header_end)] = max(by_header.get((start, header_end), 0), end)
        for turn, view in enumerate(views[1:], 1):
            # The raw quote table is authoritative even if normalization has
            # removed a raw backslash-quote or exposed a comma inside it.
            mime = _mime_ends(raw, work, turn)
            for start, header_end, end, whole in _occurrences(view, maximum, False, work, mime, raw):
                if whole:
                    edits.add(fallback[0], fallback[1], True)
                    return None
                raw_start = view.start(start)
                raw_header, raw_end = view.end(header_end - 1), view.end(end - 1)
                if by_header.get((raw_start, raw_header), 0) < raw_end:
                    edits.add(fallback[0], fallback[1], True)
                    return None

    # First-pass matching is complete before any A edits are emitted.
    # A decoded reference can emit multiple characters with one original span.
    # A boundary inside that atom is not an exact raw cut, even under Q1.
    for start, header_end, end, _ in raw_matches:
        if ((start > 0 and base.end(start - 1) > base.start(start))
                or (end < len(raw) and base.start(end) < base.end(end - 1))):
            edits.add(base.raw_start, base.raw_end, True)
            return None
    ranges = [(start, end) for start, _, end, _ in raw_matches]

    seen = set()
    for view in views:
        work.spend(len(view.text))
        if len(view.text) > maximum and ((image and damaged)
                or ((image or _has_image_signature(view.text)) and _is_base64_text(view.text))):
            ranges.append((0, len(raw)))
        if not view.text.lstrip().startswith(("{", "[")):
            continue
        for token in _document_tokens(view, work):
            if not token.image() or not token.view.text:
                continue
            key = (token.view.raw_start, token.view.raw_end)
            if key in seen:
                continue
            seen.add(key)
            if _inspect_declared(token, maximum, work):
                ranges.append((token.view.raw_start, token.view.raw_end))

    if verify_output:
        # R-VIEW includes R-FIX (the emitted text itself), including scopes
        # whose precise pass found nothing. Reuse texts, never recognizer facts.
        emitted_views = views
        if ranges:
            local = _Edits(len(raw))
            for start, end in ranges:
                local.add(start, end)
            emitted, _ = local.apply_counted(raw)
            emitted_views = [_View(emitted, raw_end=len(emitted))]
            output_work = _Work(len(emitted))
            for _ in range(remaining):
                view = emitted_views[-1]
                if _has_backstop_fence(view.text):
                    break
                following = _decode_pass(view, output_work)
                if following is view:
                    break
                emitted_views.append(following)
        if (any(_DATA_START.search(view.text) for view in views)
                or any(_DATA_START.search(view.text) for view in emitted_views)):
            # Here depth > 0 is exactly a strict JSON string token VALUE:
            # fenced documents returned above, tokens are queued at depth + 1
            # and these runs pass verify_output=False. A token's runs keep its
            # own declaration context but close only on a TEXT change of the
            # emitted value or a decoded view (a second call changes nothing).
            # Re-marking the fixed marker in place is not a payload, so the
            # verdict equals the plain value's. Root scopes keep the count test.
            token = depth > 0
            for turn, view in enumerate(emitted_views):
                # Every view becomes an independent RAW input, with fresh
                # delimiter/flag/run indexes and only its unspent allowance.
                # Closing checks are disabled throughout these bounded runs.
                checked, changes = _cap_string_counted(
                    view.text, maximum, image, remaining=remaining - turn,
                    depth=depth, damaged=damaged, verify_output=False,
                )
                if changes and (not token or checked != view.text):
                    edits.add(fallback[0], fallback[1], True)
                    return None
    for start, end in ranges:
        edits.add(base.start(start), base.end(end - 1))
    return None


def _cap_string_counted(text: str, maximum: int, image_key: bool, *,
                        remaining: int = _DECODE_PASSES, depth: int = 0,
                        damaged: bool = False, verify_output: bool = True) -> Tuple[str, int]:
    if len(text) > _SCAN_CAP:
        return _WHOLE, 1
    if not text:
        return text, 0
    edits = _Edits(len(text))
    # Lexical JSON recursion is free of transport passes, but not unbounded.
    # Work is local to each token; structural depth bounds repeated free work.
    # Revealing a scalar by transport keeps its pre-fence fallback ownership.
    root = _View(text, raw_end=len(text))
    pending = [(root, image_key, damaged, remaining, depth, (0, len(text)))]
    while pending:
        view, image, damaged, remaining, depth, fallback = pending.pop()
        if depth >= _FENCE_DEPTH:
            edits.add(fallback[0], fallback[1], True)
            continue
        try:
            result = _inspect_scope(view, maximum, image, damaged,
                                    _Work(len(view.text)), edits, remaining, fallback,
                                    depth, verify_output)
            if result is None:
                continue
            children, budget, transported_scalar = result
            for token in reversed(children):
                child = token.view
                if not child.text:
                    continue
                child_fallback = fallback if transported_scalar else (child.raw_start, child.raw_end)
                pending.append((child, token.image(), token.damaged, budget, depth + 1, child_fallback))
        except _WorkExhausted:
            edits.add(fallback[0], fallback[1], True)
    return edits.apply_counted(text)


def _cap_base64_images_counted(value: Any, max_length: Any, marker: str,
                             image_key: bool = False) -> Tuple[Any, int]:
    """Private physical replacement count; never exported as a new attribute."""
    max_length = _normalize_image_cap(max_length)
    if marker != "__REDACTED__":
        raise ValueError("base64 image cap requires the fixed __REDACTED__ marker")
    if isinstance(value, str):
        return _cap_string_counted(value, max_length, image_key)
    if isinstance(value, (list, tuple)):
        items = []
        count = 0
        for item in value:
            result, drops = _cap_string_counted(item, max_length, image_key) if isinstance(item, str) else (item, 0)
            items.append(result)
            count += drops
        if all(a is b for a, b in zip(items, value)):
            return value, count
        return (tuple(items) if isinstance(value, tuple) else items), count
    return value, 0


def cap_base64_images(value: Any, max_length: Any, marker: str, image_key: bool = False) -> Any:
    """Invalid caps fail closed to zero; the fixed marker contract is unchanged."""
    return _cap_base64_images_counted(value, max_length, marker, image_key)[0]
