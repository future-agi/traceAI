"""Regression coverage for nullable custom-evaluation models (T1-PY-CORE-005)."""

import json
from unittest.mock import patch

import pytest

from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ModelChoices,
    ProjectType,
    prepare_eval_tags,
)
from fi_instrumentation.otel import EVAL_TAGS


@pytest.fixture
def custom_eval_template():
    with patch(
        "fi_instrumentation.fi_types.get_custom_eval_template",
        return_value={
            "isUserEvalTemplate": True,
            "evalTemplate": {"config": {"requiredKeys": ["output"]}},
        },
    ) as lookup:
        yield lookup


def test_custom_eval_without_model_serializes(custom_eval_template):
    tag = EvalTag(
        type=EvalTagType.OBSERVATION_SPAN,
        value=EvalSpanKind.LLM,
        eval_name="probe_custom_eval",
        mapping={"output": "output.value"},
    )

    assert tag.model is None
    custom_eval_template.assert_called_once_with("probe_custom_eval")
    serialized = tag.to_dict()
    assert serialized["model"] is None
    assert prepare_eval_tags([tag]) == [serialized]


@pytest.mark.parametrize(
    ("model_kwargs", "expected_model"),
    [
        ({}, None),
        ({"model": None}, None),
        ({"model": "custom-model"}, "custom-model"),
        ({"model": ModelChoices.TURING_LARGE}, ModelChoices.TURING_LARGE.value),
    ],
    ids=["omitted", "explicit-none", "string", "enum"],
)
def test_register_custom_eval_model(
    custom_eval_template, model_kwargs, expected_model
):
    tag = EvalTag(
        type=EvalTagType.OBSERVATION_SPAN,
        value=EvalSpanKind.LLM,
        eval_name="probe_custom_eval",
        mapping={"output": "output.value"},
        **model_kwargs,
    )
    expected_tags = [{
        "type": EvalTagType.OBSERVATION_SPAN.value,
        "value": EvalSpanKind.LLM.value,
        "eval_name": "probe_custom_eval",
        "config": {},
        "mapping": {"output": "output.value"},
        "custom_eval_name": "probe_custom_eval",
        "model": expected_model,
    }]

    with patch(
        "fi_instrumentation.otel.check_custom_eval_config_exists", return_value=False
    ) as check_config, patch(
        "fi_instrumentation.otel.TracerProvider"
    ) as provider_class, patch(
        "fi_instrumentation.otel.BatchSpanProcessor"
    ) as processor_class:
        provider = register(
            project_name="probe",
            project_version_name="test-version",
            eval_tags=[tag],
            verbose=False,
        )

    custom_eval_template.assert_called_once_with("probe_custom_eval")
    check_config.assert_called_once_with(
        project_name="probe",
        project_type=ProjectType.EXPERIMENT.value,
        eval_tags=expected_tags,
    )
    provider_class.assert_called_once()
    assert provider is provider_class.return_value
    resource = provider_class.call_args.kwargs["resource"]
    assert json.loads(resource.attributes[EVAL_TAGS]) == expected_tags
    processor_class.assert_called_once()
    provider.add_span_processor.assert_called_once_with(processor_class.return_value)


def test_system_eval_still_requires_model():
    with patch(
        "fi_instrumentation.fi_types.get_custom_eval_template",
        return_value={"isUserEvalTemplate": False, "evalTemplate": {}},
    ), pytest.raises(ValueError, match="model must be a of type ModelChoices"):
        EvalTag(
            type=EvalTagType.OBSERVATION_SPAN,
            value=EvalSpanKind.LLM,
            eval_name=EvalName.TOXICITY,
            mapping={"input": "input.value"},
        )
