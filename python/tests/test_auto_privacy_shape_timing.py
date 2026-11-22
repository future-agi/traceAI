"""Bounded-time and whole-token checks for the catalog's credential value shapes, through
``fi_instrumentation.auto._privacy.scrub``. Reusable constants: N, BUDGET_MS, INPUTS, JWTS."""

import base64
import json
import random
import re
import time

import pytest

N = 10000
BUDGET_MS = 100
MARK = "<redacted>"

INPUTS = {
    "jwt": "eyJ-" * N,
    "jwt_dot": "eyJ-" * N + ".",
    "jwt_two_dots": "eyJ.eyJ-" * (N // 2),
    "jwt_two_segments": "eyJabcdefgh." + "eyJ-" * N,
    "sk": "sk-" * N,
    "sk_long_run": "sk-" * 7 + "a" * N * 10 + "\N{LATIN SMALL LETTER E WITH ACUTE}",
    "aws_akia": "AKIA" * N,
    "aws_asia": "ASIA" * N,
    "bearer": "Bearer " * N,
    "basic": "Basic " * N,
    "bearer_spaces": ("Bearer" + " " * 1000 + "!") * 100,
    "private_key": "-----BEGIN PRIVATE KEY-----" * N,
}


def _b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


_HEAD = _b64({"alg": "HS256", "typ": "JWT"})
_BODY = _b64({"sub": "1234567890", "name": "Test User", "iat": 1516239022})
JWTS = {
    "plain": f"{_HEAD}.{_BODY}.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    "sig_ends_hyphen": f"{_HEAD}.{_BODY}.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQss-",
    "sig_with_hyphens": f"{_HEAD}.{_BODY}.Sfl-Kxw-RJSMeKKF2QT4fwpMeJf36POk6yJV",
    "preceded_by_hyphen": "x-" + f"{_HEAD}.{_BODY}.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    "in_text": f"token was {_HEAD}.{_BODY}.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c, please rotate it",
    "in_json_text": '{"note":"got ' + f"{_HEAD}.{_BODY}.abcdefghijklmnop" + ' back"}',
}


@pytest.fixture(scope="module")
def scrub():
    from fi_instrumentation.auto import _privacy

    _privacy.scrub("warm up", "input.value")
    return lambda value: _privacy.scrub(value, "input.value")


def _timed(scrub, value, runs=3):
    """The fastest of ``runs`` scrubs in milliseconds, so one slow run (a collection, a busy machine) does not decide."""
    best = None
    for _ in range(runs):
        start = time.monotonic()
        scrub(value)
        elapsed = (time.monotonic() - start) * 1000
        best = elapsed if best is None else min(best, elapsed)
    return best


@pytest.mark.parametrize("case", sorted(INPUTS))
def test_shape_input_scrubs_within_budget(scrub, case):
    elapsed = _timed(scrub, INPUTS[case])
    print(f"TIMING {case}: {elapsed:.1f} ms")
    assert elapsed < BUDGET_MS, f"{case} took {elapsed:.1f} ms (budget {BUDGET_MS} ms)"


def _token(text):
    start = text.index("eyJ")
    end = start
    while end < len(text) and (text[end].isalnum() or text[end] in "._-"):
        end += 1
    return text[start:end]


@pytest.mark.parametrize("case", sorted(JWTS))
def test_real_jwt_is_replaced_whole(scrub, case):
    text = JWTS[case]
    token = _token(text)
    assert token.count(".") == 2
    clean = scrub(text)
    # the whole token, including a trailing hyphen of its signature, is replaced by one marker
    assert clean == text.replace(token, MARK), (case, clean)
    for part in token.split("."):
        assert part[-6:] not in clean, (case, clean)


# ---- large tokens: the jwt shape is unbounded, so a real token of any size is replaced whole ----

_SIG = "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"


def _x5c_like_header(size):
    """A base64url header that starts with 'eyJ' and is about `size` characters long."""
    cert = base64.urlsafe_b64encode(bytes(range(256)) * (size // 256 + 1)).decode().rstrip("=")
    raw = '{"alg":"RS256","x5c":["' + cert + '"]}'
    head = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")[:size]
    assert head.startswith("eyJ") and len(head) == size
    return head


LARGE_JWTS = {
    "header_2kb": f"{_x5c_like_header(2048)}.{_BODY}.{_SIG}",
    "payload_12kb": f"{_HEAD}.{_b64({'sub': 'a', 'blob': 'z' * 9000})}.{_SIG}",
}


@pytest.mark.parametrize("prefix", ["", "Bearer "])
@pytest.mark.parametrize("case", sorted(LARGE_JWTS))
def test_large_jwt_is_replaced_whole(scrub, case, prefix):
    token = LARGE_JWTS[case]
    head, body, _ = token.split(".")
    assert len(head) > 1024 or len(body) > 8192
    clean = scrub(prefix + token)
    assert clean == prefix + MARK, (case, prefix, len(clean), clean[:80])
    for part in token.split("."):
        assert part[-6:] not in clean


# ---- differential: the catalog's jwt shape against the plain unbounded regex, on small inputs ----

REFERENCE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
PIECES = ["eyJ", "eyJaaaaaaaa", "aaaaaaaa", ".", "-", "_", " ", "x"]
ROUNDS = 20000


def test_value_shapes_match_unbounded_reference_regex():
    from fi_instrumentation.auto import _privacy

    shapes = _privacy._catalog().shapes

    def value_shapes(text):
        for shape, replacement in shapes:
            text = shape.sub(replacement, text)
        return text

    rng = random.Random(20240607)
    hits = 0
    for round_no in range(ROUNDS):
        text = "".join(rng.choice(PIECES) for _ in range(rng.randint(1, 20)))
        expected = REFERENCE.sub(MARK, text)
        hits += expected != text
        assert value_shapes(text) == expected, (round_no, text)
    assert hits > 100, hits  # the generator does reach the shape


# ---- adversarial differential: near-misses of a valid token, every one against the reference ----

E_ACUTE = "\N{LATIN SMALL LETTER E WITH ACUTE}"
_VALID = "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2Q"


def _near_misses():
    """A valid token broken at every position, three ways, each alone and between other text."""
    out = [_VALID]
    for i in range(len(_VALID)):
        out.append(_VALID[:i] + "!" + _VALID[i + 1:])          # one char replaced by a non-run char
        out.append(_VALID[:i] + " " + _VALID[i + 1:])
        out.append(_VALID[:i] + _VALID[i + 1:])                 # one char removed (a dot, or a letter)
        out.append(_VALID[:i] + "." + _VALID[i:])               # a dot added
        out.append(_VALID[:i] + "-" + _VALID[i:])               # a hyphen added
        out.append(_VALID[:i] + "_" + _VALID[i + 1:])           # a word character in place
        out.append(_VALID[:i] + E_ACUTE + _VALID[i:])           # a non-ASCII letter added
    return out


def _mixed(rng):
    """Runs mixing '.', '-' and '_' between eyJ starts and short or long segments."""
    atoms = ["eyJ", "eyJabcdefgh", "abcdefgh", "abcdefg", "a", ".", "..", "-", "_", "-.", ".-", "_.", "eyJ-", "-eyJ"]
    return "".join(rng.choice(atoms) for _ in range(rng.randint(1, 14)))


def _jwt_shape():
    """The catalog's jwt shape as the SDK scans it (the one that is not a plain compiled regex)."""
    from fi_instrumentation.auto import _privacy

    found = [shape for shape, _ in _privacy._catalog().shapes if not isinstance(shape, re.Pattern)]
    assert len(found) == 1
    return found[0]


def test_value_shape_matches_reference_on_adversarial_near_misses_and_mixed_runs():
    shape = _jwt_shape()

    def check(text, tag):
        assert shape.sub(MARK, text) == REFERENCE.sub(MARK, text), (tag, text)

    rng = random.Random(20240608)
    rounds = 0
    for miss in _near_misses():
        for text in (miss, "a " + miss + " b", "x-" + miss, E_ACUTE + miss, miss + E_ACUTE, miss + "." + miss,
                     miss + "-" + _VALID, "Bearer " + miss):
            check(text, "near-miss")
            rounds += 1
    for _ in range(ROUNDS):
        text = _mixed(rng)
        check(text, "mixed")
        rounds += 1
    # a non-ASCII letter next to eyJ: no word boundary before it, and a start after a hyphen is one
    for text in (E_ACUTE + _VALID, "1" + _VALID, "_" + _VALID, "-" + _VALID, "." + _VALID, E_ACUTE + "-" + _VALID,
                 _VALID + E_ACUTE, "\u0663" + _VALID, "\u00aa" + _VALID):
        check(text, "unicode")
        rounds += 1
    assert rounds >= ROUNDS, rounds


def test_value_shape_matches_reference_on_one_large_input():
    """64,000 characters (the text limit is 4,194,304): the backtracking reference takes about 1.5 s on it."""
    text = "eyJ-" * 15000 + _VALID + " " + "eyJ.eyJ-" * 500 + "eyJ-" * 1000 + _VALID + "." + E_ACUTE + _VALID
    assert len(text) >= 64000
    expected = REFERENCE.sub(MARK, text)
    assert _jwt_shape().sub(MARK, text) == expected
    assert expected != text
