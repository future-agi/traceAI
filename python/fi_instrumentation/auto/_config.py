"""Agent configuration: arguments, then environment, then ``futureagi.toml``, then defaults."""

import json
import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Iterable, Mapping, Optional, Tuple

from fi_instrumentation._credentials import credential_pair as _credential_pair
from fi_instrumentation.auto.wire._core import DEFAULT_MAX_BODY_BYTES

# The parser and privacy resolution live in the dependency-light core resolver, which
# TraceConfig() and the exporters use too; this module adds the agent's own settings.
from fi_instrumentation._configuration import (  # noqa: F401  (re-exported)
    CONFIG_FILE_ENV,
    DEFAULT_CONFIG_FILE,
    ConfigError,
    _FALSE,
    _TRUE,
    _as_bool,
    _parse_value,
    _strip_comment,
    env_name,
    load_config_file,
    parse_config_text,
    privacy_values,
)

_SECRET_KEYS = ("api_key", "secret_key")


def _as_list(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    items = value if isinstance(value, list) else str(value).split(",")
    names = [str(item).strip().lower().replace("-", "_") for item in items if str(item).strip()]
    return names


@dataclass
class AutoConfig:
    api_key: Optional[str] = None
    secret_key: Optional[str] = None
    base_url: Optional[str] = None
    project_name: Optional[str] = None
    project_type: str = "observe"
    project_version_name: Optional[str] = None
    instrumentations: Optional[List[str]] = None
    disabled_instrumentations: List[str] = field(default_factory=list)
    wire_capture: bool = True
    wire_max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    sample_rate: float = 1.0
    debug: bool = False
    sdk_disabled: bool = False
    config_file: Optional[str] = None
    sources: Dict[str, str] = field(default_factory=dict)
    trace_config: Any = None

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.secret_key)

    def is_selected(self, name: str, default: bool = True) -> bool:
        name = name.lower()
        if self.sdk_disabled or name in self.disabled_instrumentations:
            return False
        if self.instrumentations is not None:
            return name in self.instrumentations
        return default

    def wire_enabled(self) -> bool:
        return self.wire_capture and self.is_selected("wire")

    def masked(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key in ("api_key", "secret_key", "base_url", "project_name", "project_type", "project_version_name",
                    "instrumentations", "disabled_instrumentations", "wire_capture", "wire_max_body_bytes",
                    "sample_rate", "debug", "config_file"):
            value = getattr(self, key)
            if key in _SECRET_KEYS and value:
                value = value[:4] + "…" if len(value) > 8 else "…"
            out[key] = {"value": value, "source": self.sources.get(key, "default")}
        if self.trace_config is not None:
            for f in fields(self.trace_config):
                key = f.name.lstrip("_")
                out[key] = {"value": getattr(self.trace_config, key),
                            "source": self.sources.get(key, "default")}
        return out


def _resolve_privacy(pick: Any, sources: Dict[str, str]) -> Any:
    """Resolve every TraceConfig field from the same sources as the agent (strict)."""
    from fi_instrumentation.instrumentation.config import TraceConfig

    return TraceConfig._from_resolved(privacy_values(pick, sources, strict=True))


def resolve(overrides: Optional[Mapping[str, Any]] = None, environ: Optional[Mapping[str, str]] = None,
            config_file: Optional[str] = None, *, trace_config: Any = None) -> AutoConfig:
    """Resolve settings with precedence: overrides, environment, config file, defaults."""
    environ = os.environ if environ is None else environ
    overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
    shadowed = set(overrides) | {key.lower()[3:] for key, value in environ.items() if key.startswith("FI_") and value}
    if trace_config is not None:
        shadowed.update(f.name.lstrip("_") for f in fields(trace_config))
    # Partial higher-precedence credentials must not shadow either file field.
    # A complete pair shadows both, even if the ignored file value is malformed.
    shadowed.difference_update(_SECRET_KEYS)
    pair = _credential_pair(overrides, "argument")
    pair_source = "argument"
    if pair is None:
        pair = _credential_pair({key: environ.get(env_name(key)) for key in _SECRET_KEYS}, "environment")
        pair_source = "env"
    if pair is not None:
        shadowed.update(_SECRET_KEYS)
    if environ.get("FI_AUTO_DEBUG"):
        shadowed.add("debug")
    path, file_values = load_config_file(config_file, environ, shadowed)
    config = AutoConfig(config_file=str(path) if path else None)
    if pair is None:
        pair = _credential_pair(file_values, f"file {path}")
        pair_source = f"file {path}"
    if pair is not None:
        config.api_key, config.secret_key = pair
        for key in _SECRET_KEYS:
            config.sources[key] = f"env {env_name(key)}" if pair_source == "env" else pair_source

    def pick(key: str) -> Tuple[Any, str]:
        if key in overrides:
            return overrides[key], "argument"
        env = env_name(key)
        if environ.get(env) not in (None, ""):
            return environ[env], f"env {env}"
        if key in file_values:
            return file_values[key], f"file {path}"
        return None, "default"

    for key in ("base_url", "project_name", "project_version_name"):
        value, source = pick(key)
        if value is not None:
            setattr(config, key, str(value))
            config.sources[key] = source
    if config.project_name is None:
        # Name the project after the service rather than lumping every app into one default.
        if environ.get("OTEL_SERVICE_NAME"):
            config.project_name, config.sources["project_name"] = environ["OTEL_SERVICE_NAME"], "env OTEL_SERVICE_NAME"
        else:
            try:
                directory_name = Path.cwd().name
            except OSError:
                directory_name = ""
            config.project_name = directory_name or "DEFAULT_PROJECT_NAME"
            config.sources["project_name"] = "working directory name" if directory_name else "default"
    value, source = pick("project_type")
    if value is not None:
        text = str(value).strip().lower()
        if text not in ("observe", "experiment"):
            raise ConfigError(f"project_type must be observe or experiment, not {value!r} ({source})")
        config.project_type, config.sources["project_type"] = text, source
    for key in ("instrumentations", "disabled_instrumentations"):
        value, source = pick(key)
        names = _as_list(value)
        if names is not None:
            setattr(config, key, names)
            config.sources[key] = source
    for key, default in (("wire_capture", True), ("debug", False)):
        value, source = pick(key)
        if value is not None:
            setattr(config, key, _as_bool(value, default))
            config.sources[key] = source
    value, source = pick("wire_max_body_bytes")
    if value is not None:
        try:
            config.wire_max_body_bytes = max(0, int(value))
            config.sources["wire_max_body_bytes"] = source
        except (TypeError, ValueError):
            raise ConfigError(f"wire_max_body_bytes must be an integer ({source})") from None
    value, source = pick("sample_rate")
    if value is not None:
        try:
            rate = float(value)
        except (TypeError, ValueError):
            raise ConfigError(f"sample_rate must be a number ({source})") from None
        if not 0.0 <= rate <= 1.0:
            raise ConfigError(f"sample_rate must be between 0 and 1 ({source})")
        config.sample_rate, config.sources["sample_rate"] = rate, source
    config.sdk_disabled = _as_bool(environ.get("OTEL_SDK_DISABLED"), False)
    if trace_config is None:
        config.trace_config = _resolve_privacy(pick, config.sources)
    else:
        config.trace_config = trace_config
        config.sources.update({f.name.lstrip("_"): "argument" for f in fields(trace_config)})
    return config


CONFIG_TEMPLATE = """\
# Future AGI agent configuration. Every key can also be set as FI_<KEY> in the environment,
# which takes precedence. Keep FI_API_KEY and FI_SECRET_KEY in the environment, not here.

project_name = "{project_name}"
project_type = "observe"          # observe | experiment

# Integrations to enable; omit to enable everything installed. "wire" is the HTTP-level layer.
# instrumentations = ["openai", "langchain", "wire"]
disabled_instrumentations = []

wire_capture = true               # trace AI API calls made by any client, even without a dedicated package
sample_rate = 1.0                 # fraction of traces to keep
debug = false                     # print the integration report at startup
"""
