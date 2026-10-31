import pytest


@pytest.mark.parametrize(
    "flag,keys",
    [
        ("FI_HIDE_INPUTS", ["input.value", "gen_ai.tool.call.arguments"]),
        (
            "FI_HIDE_INPUT_MESSAGES",
            ["gen_ai.input.messages", "llm.input_messages.0.message.content"],
        ),
        (
            "FI_HIDE_INPUT_TEXT",
            ["gen_ai.input.messages", "gen_ai.input.messages.0.message.content"],
        ),
        ("FI_HIDE_INPUT_IMAGES", ["gen_ai.input.messages", "raw.input"]),
        ("FI_HIDE_OUTPUTS", ["output.value", "gen_ai.tool.call.result"]),
        ("FI_HIDE_OUTPUT_MESSAGES", ["gen_ai.output.messages", "raw.output"]),
        (
            "FI_HIDE_OUTPUT_TEXT",
            ["gen_ai.output.messages", "gen_ai.output.messages.0.message.content"],
        ),
        (
            "FI_HIDE_LLM_INVOCATION_PARAMETERS",
            ["gen_ai.request.parameters", "llm.invocation_parameters"],
        ),
    ],
)
def test_each_privacy_ceiling(monkeypatch, tracing, flag, keys):
    monkeypatch.setenv(flag, "true")
    handle, output = tracing(capture_content=True, keep_error_text=True)
    with handle.provider.get_tracer("app").start_as_current_span(
        "model", attributes={key: "PRIVATE" for key in keys}
    ):
        pass
    assert handle.force_flush()
    assert "PRIVATE" not in output.get_finished_spans()[0].to_json()


def test_supported_flattened_messages_opt_in_and_unknown_fields(tracing):
    handle, output = tracing(capture_content=True)
    with handle.provider.get_tracer("app").start_as_current_span(
        "model",
        attributes={
            "gen_ai.input.messages.0.message.content": "question",
            "gen_ai.output.messages.0.message.content": "answer",
            "gen_ai.input.messages.0.message.secret_future_field": "PRIVATE",
            "exception.message": "PRIVATE",
            "error.message": "PRIVATE",
        },
    ):
        pass
    assert handle.force_flush()
    attrs = output.get_finished_spans()[0].attributes
    assert attrs["gen_ai.input.messages.0.message.content"] == "question"
    assert attrs["gen_ai.output.messages.0.message.content"] == "answer"
    assert "PRIVATE" not in output.get_finished_spans()[0].to_json()


def test_content_limit_does_not_truncate_session_identity(tracing):
    handle, output = tracing(capture_content=True, max_content_length=2)
    workflow_id = "opaque-workflow-" + "x" * 800
    with handle.provider.get_tracer("app").start_as_current_span(
        "activity",
        attributes={
            "temporalWorkflowID": workflow_id,
            "input.value": "content",
        },
    ):
        pass
    assert handle.force_flush()
    attrs = output.get_finished_spans()[0].attributes
    assert attrs["session.id"] == workflow_id
    assert attrs["temporalWorkflowID"] == workflow_id
    assert attrs["input.value"] == "co"
