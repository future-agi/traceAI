"""Agent settings: arguments, then environment, then ``futureagi.toml``, then defaults."""

import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, get_args

from fi_instrumentation._configuration import (  # noqa: F401  (re-exported)
    CONFIG_FILE_ENV,
    DEFAULT_CONFIG_FILE,
    ConfigError,
    _FALSE,
    _TRUE,
    _as_bool,
    credential_pair,
    env_name,
    load_config_file,
    parse_config_text,
)

logger = logging.getLogger("fi_instrumentation.auto")

_SECRET_KEYS = ("api_key", "secret_key")
_SETTINGS = ("api_key", "secret_key", "base_url", "project_name", "project_type", "project_version_name",
             "instrumentations", "disabled_instrumentations", "sample_rate", "debug", "config_file")


def _as_list(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    items = value if isinstance(value, list) else str(value).split(",")
    return [str(item).strip().lower().replace("-", "_") for item in items if str(item).strip()]


def _privacy_keys(trace_config: Any) -> List[str]:
    """The TraceConfig settings (hide_inputs, ...), by their config-file names."""
    try:
        return [f.name.lstrip("_") for f in fields(trace_config)]
    except TypeError:
        return []


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

    def masked(self) -> Dict[str, Any]:
        """Every setting with its source; keys are shortened so they can be shown or logged."""
        out: Dict[str, Any] = {}
        for key in _SETTINGS:
            value = getattr(self, key)
            if key in _SECRET_KEYS and value:
                value = value[:4] + "…" if len(value) > 8 else "…"
            out[key] = {"value": value, "source": self.sources.get(key, "default")}
        for key in _privacy_keys(self.trace_config):
            out[key] = {"value": getattr(self.trace_config, key), "source": self.sources.get(key, "default")}
        return out


def _trace_config(file_values: Mapping[str, Any], path: Optional[Path], environ: Mapping[str, str],
                  sources: Dict[str, str]) -> Any:
    """A TraceConfig with the config file's privacy settings.

    Environment variables keep precedence over the file: TraceConfig reads them itself, exactly
    as it does without the agent.
    """
    from fi_instrumentation.instrumentation.config import TraceConfig

    kwargs: Dict[str, Any] = {}
    for f in fields(TraceConfig):
        key = f.name.lstrip("_")
        env = f.metadata.get("env_var")
        if env and environ.get(env) not in (None, ""):
            sources[key] = f"env {env}"
        elif key in file_values:
            value = file_values[key]
            if get_args(f.type)[:1] == (bool,) and isinstance(value, str) and value.strip().lower() in _TRUE + _FALSE:
                value = _as_bool(value, False)
            kwargs[key] = value
            sources[key] = f"file {path}"
    try:
        return TraceConfig(**kwargs)
    except (TypeError, ValueError) as error:
        # TraceConfig names the setting and the expected type, never the value.
        raise ConfigError(f"{path}: {error}") from None


def resolve(overrides: Optional[Mapping[str, Any]] = None, environ: Optional[Mapping[str, str]] = None,
            config_file: Optional[str] = None, *, trace_config: Any = None) -> AutoConfig:
    """Resolve settings with precedence: overrides, environment, config file, defaults.

    The Future AGI keys are resolved as one complete pair per source, in the same order.

    Raises:
        ConfigError: a setting is invalid, or a named config file does not exist.
    """
    environ = os.environ if environ is None else environ
    overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
    # File keys set by a higher-precedence source are not even read, so an invalid file
    # value cannot block a valid argument or environment variable.
    shadowed = set(overrides) | {key.lower()[3:] for key, value in environ.items() if key.startswith("FI_") and value}
    if trace_config is not None:
        shadowed.update(_privacy_keys(trace_config))
    # A partial higher-precedence pair shadows neither file key; a complete one shadows both.
    shadowed.difference_update(_SECRET_KEYS)
    pair, pair_source = credential_pair(overrides, "arguments"), "argument"
    if pair is None:
        pair = credential_pair({key: environ.get(env_name(key)) for key in _SECRET_KEYS}, "the environment")
        pair_source = "env"
    if pair is not None:
        shadowed.update(_SECRET_KEYS)
    if environ.get("FI_AUTO_DEBUG"):
        shadowed.add("debug")
    path, file_values = load_config_file(config_file, environ, shadowed)
    config = AutoConfig(config_file=str(path) if path else None)
    if pair is None:
        pair = credential_pair(file_values, f"file {path}")
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
        if key == "base_url" and source == f"file {path}" and (pair is None or pair_source != source):
            # The file may come with a checkout: it must not send keys from elsewhere, or the
            # traces, to a collector it names.
            logger.warning("traceAI: ignoring base_url from %s because the Future AGI keys do not come from "
                           "that file; set FI_BASE_URL to send to another collector", path)
            continue
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
            raise ConfigError(f"project_type must be observe or experiment ({source})")
        config.project_type, config.sources["project_type"] = text, source
    for key in ("instrumentations", "disabled_instrumentations"):
        value, source = pick(key)
        names = _as_list(value)
        if names is not None:
            setattr(config, key, names)
            config.sources[key] = source
    value, source = pick("debug")
    if value is not None:
        config.debug, config.sources["debug"] = _as_bool(value, False), source
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
        config.trace_config = _trace_config(file_values, path, environ, config.sources)
    else:
        config.trace_config = trace_config
        config.sources.update({key: "argument" for key in _privacy_keys(trace_config)})
    return config


CONFIG_TEMPLATE = """\
# Future AGI agent configuration. Every key can also be set as FI_<KEY> in the environment
# (debug is FI_AUTO_DEBUG), which takes precedence. Keep FI_API_KEY and FI_SECRET_KEY in the
# environment, not here.

project_name = "{project_name}"
project_type = "observe"          # observe | experiment

# Integrations to enable; omit to enable every installed one. `fi-instrument doctor` lists the names.
# instrumentations = ["openai", "langchain"]
disabled_instrumentations = []

sample_rate = 1.0                 # fraction of traces to keep
debug = false                     # print the integration report at startup

# Prompts and responses are captured unless you hide them.
# hide_inputs = true
# hide_outputs = true
"""
