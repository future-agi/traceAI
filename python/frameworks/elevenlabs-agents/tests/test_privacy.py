"""The never-exported layer and the TraceConfig side mapping."""

import pytest
from fi_instrumentation import TraceConfig

from traceai_elevenlabs_agents import privacy
from traceai_elevenlabs_agents.privacy import Capture, error_summary, looks_like_stack_trace, scrub_identity


@pytest.mark.parametrize(
    "text, expected",
    [
        ("mail alice.smith+x@corp.example.com now", "mail <EMAIL_ADDRESS> now"),
        ("failed in /home/alice/app/agent.py:12", "failed in <FILE_PATH>:12"),
        ("see /Users/bob/Library/x.log", "see <FILE_PATH>"),
        ("open C:\\Users\\bob\\agent\\run.py", "open <FILE_PATH>"),
        ("cfg at ~/.config/agent.toml", "cfg at <FILE_PATH>"),
        ("loaded file:///Users/bob/x.json", "loaded <FILE_PATH>"),
        ("on alice-laptop.local and db.corp", "on <HOSTNAME> and <HOSTNAME>"),
        # Not identity: URLs, dates, ratios, a bare root path, public host names.
        ("GET https://api.elevenlabs.io/v1/convai/x", "GET https://api.elevenlabs.io/v1/convai/x"),
        ("on 10/16/2026, and/or 3/4 of /tmp", "on 10/16/2026, and/or 3/4 of /tmp"),
        ("api.openai.com timed out", "api.openai.com timed out"),
    ],
)
def test_scrub_identity(text, expected):
    assert scrub_identity(text) == expected


def test_error_summary_keeps_one_line_and_no_frames():
    py = 'Traceback (most recent call last):\n  File "/srv/app/a.py", line 3, in f\nValueError: bad /srv/app/x'
    assert error_summary(py) == "ValueError: bad <FILE_PATH>"
    java = "java.io.IOException: closed\n\tat com.acme.Agent.run(Agent.java:42)\n\tat java.lang.Thread.run(Thread.java:1)"
    assert error_summary(java) == "java.io.IOException: closed"
    assert error_summary("upstream timeout") == "upstream timeout"
    assert error_summary("x" * 2000).endswith("\u2026") and len(error_summary("x" * 2000)) == privacy.MAX_ERROR_TEXT
    assert error_summary("  \n ") is None and error_summary(None) is None


def test_stack_trace_detection():
    assert looks_like_stack_trace("Traceback (most recent call last):\n  x")
    assert looks_like_stack_trace('oops\n  File "a.py", line 1, in <module>')
    assert not looks_like_stack_trace("The file is at line 4 of the form")
    assert not looks_like_stack_trace('File "a.py", line 1')  # single line: a sentence, not a trace


@pytest.mark.parametrize(
    "config, sides",
    [
        (TraceConfig(), (True, True)),
        (TraceConfig(hide_inputs=True), (False, True)),
        (TraceConfig(hide_input_messages=True), (False, True)),
        (TraceConfig(hide_input_text=True), (False, True)),
        (TraceConfig(hide_outputs=True), (True, False)),
        (TraceConfig(hide_output_messages=True), (True, False)),
        (TraceConfig(hide_output_text=True), (True, False)),
        (TraceConfig(hide_inputs=True, hide_outputs=True), (False, False)),
        # Not a content switch here: there are no images or embeddings in a call trace.
        (TraceConfig(hide_input_images=True, hide_embedding_vectors=True), (True, True)),
    ],
)
def test_trace_config_sides(config, sides):
    capture = Capture.from_trace_config(config)
    assert (capture.input, capture.output) == sides
    assert capture.everything is all(sides)


def test_classification():
    usage = lambda v: {"tokens": 1} if v == "ok" else None  # noqa: E731
    assert privacy.classify("elevenlabs.user.text", "hi", parse_llm_usage=usage) == privacy.INPUT
    assert privacy.classify("elevenlabs.dynamic_variable.name", "Bo", parse_llm_usage=usage) == privacy.INPUT
    assert privacy.classify("elevenlabs.tool.result", "{}", parse_llm_usage=usage) == privacy.OUTPUT
    assert privacy.classify("elevenlabs.data_collection.age", 41, parse_llm_usage=usage) == privacy.OUTPUT
    assert privacy.classify("elevenlabs.event.data", "{}", parse_llm_usage=usage) == privacy.CONTENT
    assert privacy.classify("elevenlabs.turn.index", 3, parse_llm_usage=usage) == privacy.SAFE
    assert privacy.classify("elevenlabs.llm_usage.m.input", "ok", parse_llm_usage=usage) == privacy.SAFE
    assert privacy.classify("elevenlabs.llm_usage.m.input", "??", parse_llm_usage=usage) == privacy.CONTENT
    assert privacy.classify("host.name", "box", parse_llm_usage=usage) == privacy.NEVER
    assert privacy.classify("exception.stacktrace", "...", parse_llm_usage=usage) == privacy.NEVER
    assert privacy.classify("gen_ai.usage.output_tokens", 4, parse_llm_usage=usage) == privacy.CONTENT
