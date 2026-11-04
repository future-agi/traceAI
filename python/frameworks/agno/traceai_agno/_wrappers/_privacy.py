"""Compose FI privacy policy with OpenInference's attribute conventions."""

import os
from dataclasses import fields, replace
from typing import Any, cast

from fi_instrumentation.instrumentation import TraceConfig as FITraceConfig
from fi_instrumentation.instrumentation._spans import FiSpan
from openinference.instrumentation import OITracer, TraceConfig


def privacy_configs(config: TraceConfig | None) -> tuple[TraceConfig, FITraceConfig]:
    """Either configuration may hide content; neither can undo the other's hide.

    Keep OpenInference-only options and the caller's immutable config intact.
    FI's error policy derives from the effective content flags unless explicitly
    overridden by FI_HIDE_ERROR_DETAILS, just as for the shared FI tracer.
    """
    config = config if config is not None else TraceConfig()
    if not isinstance(config, TraceConfig):
        raise TypeError("config must be an OpenInference TraceConfig")
    fi_config = FITraceConfig()
    shared_flags: dict[str, Any] = {
        field.name: bool(getattr(config, field.name) or getattr(fi_config, field.name))
        for field in fields(fi_config)
        if field.name.startswith("hide_") and hasattr(config, field.name)
    }
    # An explicit FI limit may tighten OI's limit. Do not replace a caller's
    # larger OI limit with an FI default when no FI limit was requested.
    image_limit = cast(int, config.base64_image_max_length)
    if os.getenv("FI_BASE64_IMAGE_MAX_LENGTH") is not None:
        image_limit = min(image_limit, cast(int, fi_config.base64_image_max_length))
    return (
        replace(config, **shared_flags, base64_image_max_length=image_limit),
        replace(fi_config, **shared_flags, base64_image_max_length=image_limit),
    )


class AgnoTracer(OITracer):
    """Retain OI spans while applying shared FI event/status/attribute privacy.

    The policy belongs to this tracer, not an exporter or provider processor, so
    it also applies to caller-owned providers and never changes unrelated spans.
    Inherited start_as_current_span dispatches through this start_span method.
    """

    def __init__(self, wrapped: Any, config: TraceConfig, fi_config: FITraceConfig):
        super().__init__(wrapped, config=config)
        self._self_fi_config = fi_config

    def start_span(self, *args: Any, **kwargs: Any) -> FiSpan:
        if attributes := kwargs.get("attributes"):
            kwargs["attributes"] = {
                key: masked
                for key, value in attributes.items()
                if (masked := self._self_fi_config.mask(key, value)) is not None
            }
        return FiSpan(super().start_span(*args, **kwargs), config=self._self_fi_config)
