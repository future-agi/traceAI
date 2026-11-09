"""Compose FI privacy policy with OpenInference's attribute conventions."""

import os
import re
from dataclasses import fields, replace
from functools import wraps
from inspect import signature
from typing import Any, Callable, cast

from fi_instrumentation.instrumentation import TraceConfig as FITraceConfig
from fi_instrumentation.instrumentation._image_cap import _normalize_image_cap
from fi_instrumentation.instrumentation._spans import FiSpan
from openinference.instrumentation import OITracer, TraceConfig
from opentelemetry.util.types import AttributeValue
from wrapt import ObjectProxy


def privacy_configs(config: TraceConfig | None) -> tuple[TraceConfig, FITraceConfig]:
    """Either configuration may hide content; neither can undo the other's hide.

    Keep OpenInference-only options and the caller's immutable config intact.
    FI's error policy derives from the effective content flags unless explicitly
    overridden by FI_HIDE_ERROR_DETAILS, just as for the shared FI tracer.
    """
    fi_config = FITraceConfig()
    # A bridge-created config uses FI default/env policy; a supplied OI
    # config is explicit policy because constructor provenance is absent.
    supplied = config is not None
    config = config if config is not None else TraceConfig(
        base64_image_max_length=fi_config.base64_image_max_length
    )
    if not isinstance(config, TraceConfig):
        raise TypeError("config must be an OpenInference TraceConfig")
    shared_flags: dict[str, Any] = {
        field.name: bool(getattr(config, field.name) or getattr(fi_config, field.name))
        for field in fields(fi_config)
        if field.name.startswith("hide_") and hasattr(config, field.name)
    }
    # An explicit FI limit may tighten OI's limit. Do not replace a caller's
    # larger OI limit with an FI default when no FI limit was requested.
    image_limit = _normalize_image_cap(config.base64_image_max_length)
    # Match FI's nonnegative decimal cap grammar, including explicit zero.
    # Empty, negative or unparseable OI env caps do not authorize a default cap.
    oi_cap_env = os.getenv("OPENINFERENCE_BASE64_IMAGE_MAX_LENGTH")
    oi_cap_is_explicit = False
    if oi_cap_env is not None and re.fullmatch(r"[+-]?[0-9]+", oi_cap_env.strip()):
        try:
            oi_cap_is_explicit = int(oi_cap_env, 10) >= 0
        except ValueError:
            # Python can reject overlong decimal strings; treat them as absent.
            pass
    if supplied and not oi_cap_is_explicit:
        # Constructor provenance is absent: even an explicitly supplied value
        # equal to OI's default takes the FI ceiling. Read the invariant default
        # from the dataclass, never TraceConfig() under the rejected OI env cap.
        try:
            cap_field = next(
                field for field in fields(TraceConfig)
                if field.name == "base64_image_max_length"
            )
            oi_default = cap_field.metadata.get("default_value", cap_field.default)
            if (
                not isinstance(oi_default, int)
                or isinstance(oi_default, bool)
                or oi_default < 0
            ):
                oi_default = None
        except (StopIteration, TypeError):
            oi_default = None
        if oi_default is None or image_limit == oi_default:
            # Never above r5 (F2-R6-DELTA-SEC-001): the FI cap may only tighten OI's default.
            image_limit = min(image_limit, _normalize_image_cap(fi_config.base64_image_max_length))
    if os.getenv("FI_BASE64_IMAGE_MAX_LENGTH") is not None:
        image_limit = min(image_limit, cast(int, fi_config.base64_image_max_length))
    return (
        replace(config, **shared_flags, base64_image_max_length=image_limit),
        replace(fi_config, **shared_flags, base64_image_max_length=image_limit),
    )


class _PrivacyConfig(ObjectProxy):
    """Compose FI masking into OI's sampling and final attribute-write paths."""

    def __init__(self, config: TraceConfig, fi_config: FITraceConfig):
        super().__init__(config)
        self._self_fi_config = fi_config
        self._self_externalize = "externalize" in signature(config.mask).parameters

    # OI inspects mask's signature to detect its side-effect-free sampling mode
    # (externalize=False). Preserve that contract and all OI-only options without
    # mutating the caller's config or requiring a newer mask signature.
    @wraps(TraceConfig.mask)
    def mask(
        self,
        key: str,
        value: AttributeValue | Callable[[], AttributeValue],
        **kwargs: Any,
    ) -> AttributeValue | None:
        masked = self._self_fi_config.mask(key, value)
        if masked is None:
            return None
        if not self._self_externalize:
            # Match OI's fallback for custom configs with a two-argument mask.
            kwargs.pop("externalize", None)
        return cast(TraceConfig, self.__wrapped__).mask(key, masked, **kwargs)


class AgnoTracer(OITracer):
    """Retain OI spans while applying shared FI event/status/attribute privacy.

    The policy belongs to this tracer, not an exporter or provider processor, so
    it also applies to caller-owned providers and never changes unrelated spans.
    Inherited start_as_current_span dispatches through this start_span method.
    """

    def __init__(self, wrapped: Any, config: TraceConfig, fi_config: FITraceConfig):
        super().__init__(
            wrapped, config=cast(TraceConfig, _PrivacyConfig(config, fi_config))
        )
        self._self_fi_config = fi_config

    def start_span(self, *args: Any, **kwargs: Any) -> FiSpan:
        # OI adds context attributes inside start_span, before FiSpan exists.
        # Its composed config must protect those writes and sampler inputs too.
        return FiSpan(super().start_span(*args, **kwargs), config=self._self_fi_config)
