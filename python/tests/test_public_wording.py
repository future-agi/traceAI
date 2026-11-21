"""The shipped text about endpoints states behaviour only: no word from the banned list appears in it."""
import io
import pathlib
import re
import tokenize

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORDS = (
    "evil", "attacker", "attack", "leak", "forward your keys", "cleartext", "look like", "resembles",
    "fixed", "now", "previously", "vulnerab", "exploit", "never", "just",
)
_PATTERNS = {
    word: re.compile(
        r"(?<![a-z0-9])" + ("clear[ _-]?text" if word == "cleartext" else re.escape(word)) + (r"" if word == "vulnerab" else r"(?![a-z0-9])")
    )
    for word in WORDS
}


def banned(text: str) -> list[str]:
    """The banned words in ``text``; underscores read as spaces, so identifiers are covered."""
    lowered = text.lower().replace("_", " ") if text.isidentifier() else text.lower()
    return [word for word, pattern in _PATTERNS.items() if pattern.search(lowered)]


def python_text(source: str) -> dict[str, list[str]]:
    """Comments, strings (docstrings included) and identifiers of a module, each with the words found in it."""
    found: dict[str, list[str]] = {}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in (tokenize.COMMENT, tokenize.STRING, tokenize.NAME) or token.type == getattr(tokenize, "FSTRING_MIDDLE", -1):
            words = banned(token.string)
            if words:
                found[f"line {token.start[0]}: {token.string[:50]}"] = words
    return found


def changelog_section(text: str, version: str) -> str:
    start = text.index(f"## [{version}]")
    end = text.find("\n## [", start + 1)
    return text[start : end if end != -1 else len(text)]


@pytest.mark.parametrize("module", ["otel.py", "settings.py", "auto/_cli.py"])
def test_the_comments_strings_and_identifiers_of_the_changed_modules_use_the_public_wording(module):
    source = (ROOT / "fi_instrumentation" / module).read_text()
    assert python_text(source) == {}


def test_the_1_5_2_changelog_section_uses_the_public_wording():
    section = changelog_section((ROOT / "CHANGELOG.md").read_text(), "1.5.2")
    assert "### Fixed" not in section and "### Changed" in section
    assert banned(section) == []


def test_the_readme_lines_about_endpoints_use_the_public_wording():
    lines = [line for line in (ROOT / "README.md").read_text().splitlines() if re.search(r"FI_BASE_URL|FI_GRPC_URL|insecure|Future AGI host", line)]
    assert [line for line in lines if banned(line)] == []


# ---- the control: every word is caught at every kind of site ---------------------------------------------------

PLANTS = {
    "changelog": lambda w: f"## [9.9.9]\n\n### Changed\n- The exporter {w} here.\n",
    "readme": lambda w: f"Set FI_BASE_URL to a URL; the client {w} here.\n",
    "docstring": lambda w: f'def f():\n    """The helper {w} here."""\n',
    "comment": lambda w: f"x = 1  # the helper {w} here\n",
    "string": lambda w: f'message = "the helper {w} here"\n',
    "identifier": lambda w: f"{re.sub(r'[^a-z]+', '_', w)} = 1\n",
}


@pytest.mark.parametrize("word", WORDS)
@pytest.mark.parametrize("site", list(PLANTS))
def test_a_planted_word_is_caught_at_every_kind_of_site(word, site):
    planted = PLANTS[site](word if word != "vulnerab" else "vulnerable")
    if site in ("changelog", "readme"):
        assert word in banned(planted)
    else:
        assert any(word in words for words in python_text(planted).values())


def test_the_scanner_ignores_words_that_only_contain_a_banned_word():
    assert banned("known, adjusted, nowhere, justify, neverland") == []


def test_clear_text_spellings_are_caught():
    for spelling in ("clear_text", "clear-text", "clear text", "cleartext", "Clear_Text"):
        assert "cleartext" in banned(f"x {spelling} y"), spelling


# ---- the tests of the plain channel read the same way ------------------------------------------------------------

PLAIN_CHANNEL_TESTS = [
    "test_plain_channel_adapters.py", "test_plain_channel_fresh_adapter.py", "test_plain_channel_isolated_sender.py",
    "test_plain_channel_sender_type.py", "test_plaintext_post_build_session.py",
]


@pytest.mark.parametrize("module", PLAIN_CHANNEL_TESTS)
def test_the_docstrings_comments_and_names_of_the_plain_channel_tests_use_the_public_wording(module):
    assert python_text((ROOT / "tests" / module).read_text()) == {}


# Each refusal of a bullet that names `proxied` has its own "Breaking:" label, so the number of labels is the number of
# refusals the bullet lists. A bullet that is added or that loses a refusal changes this table on purpose.
PROXIED_BULLET_LABELS = {
    "- A `requests.Session` you pass to an exporter is checked when the channel is plain": 1,
    "- On a plain channel that carries Future AGI keys, what is checked is:": 1,
    "- On a plain channel that carries Future AGI keys the exporter sends with `requests.Session` itself": 1,
    "- On a plain channel that carries Future AGI keys, each adapter of the sending session": 4,
    "- `UnsupportedFutureAGIEndpointError` has a `code` attribute:": 1,
}
ADAPTER_BULLET_REFUSALS = (
    "Breaking: an adapter on a plain-http Future AGI channel whose `max_retries` is a `Retry` subclass is refused with the code `proxied`",
    "Breaking: a custom `auth` callable, or `auth` given as a list, raises `proxied`",
    "Breaking: a routing function replaced on the session or adapter object itself",
    "Breaking: a subclass of `requests.Session` that overrides a routing function raises `proxied`",
)


def proxied_bullets():
    section = changelog_section((ROOT / "CHANGELOG.md").read_text(), "1.5.2")
    return [line for line in section.splitlines() if line.startswith("- ") and "`proxied`" in line]


def test_each_changelog_bullet_that_names_the_proxied_code_carries_a_breaking_label_for_every_refusal():
    counts = {}
    for bullet in proxied_bullets():
        prefixes = [prefix for prefix in PROXIED_BULLET_LABELS if bullet.startswith(prefix)]
        assert len(prefixes) == 1, bullet[:80]
        counts[prefixes[0]] = bullet.count("Breaking:")
    assert counts == PROXIED_BULLET_LABELS


def test_every_refusal_of_the_adapter_bullet_starts_with_its_own_breaking_label():
    (bullet,) = [b for b in proxied_bullets() if b.startswith("- On a plain channel that carries Future AGI keys, each adapter")]
    for refusal in ADAPTER_BULLET_REFUSALS:
        assert bullet.count(refusal) == 1, refusal
    assert bullet.count("Breaking:") == len(ADAPTER_BULLET_REFUSALS)


def test_no_line_of_the_changelog_section_has_a_stray_separator_before_a_semicolon():
    section = changelog_section((ROOT / "CHANGELOG.md").read_text(), "1.5.2")
    stray = re.compile(r",\s*;|\s{2,};|\s;")
    assert [match.group(0) + " in: " + line[:60] for line in section.splitlines() for match in [stray.search(line)] if match] == []


def test_the_changelog_names_why_a_futures_session_is_refused_and_the_user_action_for_each_refusal():
    section = changelog_section((ROOT / "CHANGELOG.md").read_text(), "1.5.2")
    assert "`requests_futures.FuturesSession` is refused because it overrides `request`, `send` and `prepare_request`" in section
    for phrase in ("a custom `auth` callable", "routing function replaced on the session or adapter object itself",
                   "subclass of `requests.Session` that overrides a routing function"):
        assert phrase in section, phrase
