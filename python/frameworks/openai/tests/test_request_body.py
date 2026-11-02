"""Request parsing follows the SDK's shallow extra_body merge without mutation."""
from types import SimpleNamespace

import openai
import pytest

from traceai_openai._request import _parse_request_args


@pytest.mark.parametrize("json_data,extra_json,expected", [
    pytest.param({"n": 2}, {"n": 1}, {"n": 1}, id="override"),
    pytest.param({"n": 2}, {"n": openai.Omit()}, {}, id="remove"),
    pytest.param({"n": 2}, {"n": None}, {"n": None}, id="preserve-null"),
    pytest.param(None, {"n": 2}, {"n": 2}, id="extra-only"),
    pytest.param({"n": 2}, None, {"n": 2}, id="no-extra"),
    pytest.param(None, None, {}, id="no-body"),
    pytest.param(
        {"metadata": {"old": "value", "shared": "old"}},
        {"metadata": {"shared": "new"}},
        {"metadata": {"shared": "new"}},
        id="shallow-replacement",
    ),
])
def test_parse_effective_body(json_data, extra_json, expected):
    original_json = dict(json_data) if json_data is not None else None
    original_extra = dict(extra_json) if extra_json is not None else None
    options = SimpleNamespace(json_data=json_data, extra_json=extra_json)
    cast_to = openai.types.chat.ChatCompletion

    actual_type, actual = _parse_request_args((cast_to, options))

    assert actual_type is cast_to
    assert actual == expected
    assert options.json_data == original_json
    assert options.extra_json == original_extra


def test_options_without_extra_json_remain_supported():
    options = SimpleNamespace(json_data={"n": 2})
    _, actual = _parse_request_args((openai.types.chat.ChatCompletion, options))
    assert actual == {"n": 2}
