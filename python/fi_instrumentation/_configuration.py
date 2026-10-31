"""The one configuration resolver: arguments, then environment, then the config file, then defaults.

Dependency-light on purpose: ``TraceConfig()`` (every instrumentor's default policy), the
exporters and the auto agent all resolve through this module, so a privacy setting means the
same thing on the zero-code, one-line ``init()`` and manual paths.

Once the agent has resolved the process configuration (S2: once per process), default policies
use that pinned policy. Before that (manual setups), they resolve the environment, then
``FI_CONFIG_FILE`` / ``./futureagi.toml``, then defaults. Values are never logged.
"""

import json
import logging
import os
import re
import sys
import threading
from dataclasses import fields
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple

logger = logging.getLogger("fi_instrumentation.auto")

CONFIG_FILE_ENV = "FI_CONFIG_FILE"
DEFAULT_CONFIG_FILE = "futureagi.toml"
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")
_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$")
# Content families an unreadable configuration file is treated as hiding (manual paths).
FAIL_CLOSED_PRIVACY = ("hide_inputs", "hide_outputs", "hide_llm_invocation_parameters")


class ConfigError(ValueError):
    pass


def _strip_comment(value: str) -> str:
    quoted = False
    backslashes = 0
    for i, ch in enumerate(value):
        if ch == '"' and backslashes % 2 == 0:
            quoted = not quoted
        elif ch == "#" and not quoted:
            return value[:i].rstrip()
        backslashes = backslashes + 1 if ch == "\\" else 0
    return value


def _parse_value(raw: str, where: str) -> Any:
    raw = _strip_comment(raw)
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        try:
            return json.loads(raw)
        except ValueError as error:
            raise ConfigError(f"{where}: invalid string") from error
    if raw in ("true", "false"):
        return raw == "true"
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        items = [item.strip() for item in inner.split(",") if item.strip()]
        values = []
        for item in items:
            if not (item.startswith('"') and item.endswith('"')):
                raise ConfigError(f"{where}: arrays may only hold strings")
            values.append(item[1:-1])
        return values
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        # Never echo the raw value: a misplaced credential must not reach logs.
        raise ConfigError(f"{where}: cannot read value (expected a quoted string, true/false, "
                          "a number or an array of strings)") from None


def parse_config_text(text: str, source: str = DEFAULT_CONFIG_FILE, ignored_keys: Iterable[str] = ()) -> Dict[str, Any]:
    """Parse the flat TOML subset: ``key = value`` lines; tables and comments are skipped."""
    values: Dict[str, Any] = {}
    in_table = False
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("["):
            in_table = True
            continue
        if in_table:
            continue
        found = _LINE.match(line)
        if not found:
            raise ConfigError(f"{source}:{number}: expected key = value")
        if found.group(1).lower() in ignored_keys:
            continue
        values[found.group(1).lower()] = _parse_value(found.group(2), f"{source}:{number}")
    return values


def env_name(key: str) -> str:
    return "FI_AUTO_DEBUG" if key == "debug" else f"FI_{key.upper()}"


def config_path(path: Optional[str] = None, environ: Optional[Mapping[str, str]] = None) -> Tuple[Path, bool]:
    """The config file to read, and whether it was named explicitly (so it must exist)."""
    environ = os.environ if environ is None else environ
    named = path or environ.get(CONFIG_FILE_ENV)
    return Path(named or DEFAULT_CONFIG_FILE), bool(named)


def load_config_file(path: Optional[str] = None, environ: Optional[Mapping[str, str]] = None,
                     ignored_keys: Iterable[str] = ()) -> Tuple[Optional[Path], Dict[str, Any]]:
    candidate, named = config_path(path, environ)
    if not candidate.is_file():
        if named:
            raise ConfigError(f"config file {candidate} does not exist")
        return None, {}
    return candidate, parse_config_text(candidate.read_text(encoding="utf-8"), str(candidate), ignored_keys)


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


# ---------------------------------------------------------------------------
# Privacy policy (TraceConfig fields)
# ---------------------------------------------------------------------------

_warned: set = set()
_warn_lock = threading.Lock()


def _warn_once(key: Any, message: str, *args: Any) -> None:
    with _warn_lock:
        if key in _warned or len(_warned) > 256:
            return
        _warned.add(key)
    logger.warning(message, *args)


def _privacy_fields() -> Any:
    from fi_instrumentation.instrumentation.config import TraceConfig

    return fields(TraceConfig)


def privacy_values(pick: Callable[[str], Tuple[Any, str]], sources: Optional[Dict[str, str]] = None,
                   strict: bool = True) -> Dict[str, Any]:
    """Resolve every TraceConfig field with one parser for every source.

    ``strict`` (the agent) raises ConfigError on an invalid value. Default policies
    (manual paths) never raise into the application: an invalid value warns once,
    naming the field and its source but never the value, and uses the default.
    """
    from typing import get_args

    sources = {} if sources is None else sources
    values: Dict[str, Any] = {}
    for f in _privacy_fields():
        key = f.name.lstrip("_")
        value, source = pick(key)
        default = f.metadata["default_value"]
        if value is not None:
            expected = get_args(f.type)[0]
            problem = None
            if expected is bool:
                if type(value) is str and value.strip().lower() in _TRUE + _FALSE:
                    value = _as_bool(value, False)
                if type(value) is not bool:
                    problem = f"{key} must be a boolean ({source})"
            elif expected is int:
                try:
                    if type(value) is bool or isinstance(value, float):
                        raise ValueError
                    value = int(value)
                    if value < 0 and strict:
                        raise ValueError
                except (TypeError, ValueError):
                    problem = f"{key} must be a non-negative integer ({source})"
            if problem is not None:
                if strict:
                    raise ConfigError(problem)
                _warn_once(("value", key, source), "traceAI: ignoring invalid %s; using the default", problem)
                value = None
            else:
                sources[key] = source
        values[f.name] = default if value is None else value
    return values


_file_cache: Dict[str, Tuple[Tuple[int, int, int], Optional[Dict[str, Any]], Optional[str]]] = {}


def _cached_file(path: Optional[str], environ: Mapping[str, str]) -> Tuple[Optional[Path], Dict[str, Any], Optional[str]]:
    """Parsed config file values, cached by file identity; (path, values, error)."""
    candidate, named = config_path(path, environ)
    try:
        stat = candidate.stat()
    except OSError:
        if named:
            return candidate, {}, f"config file {candidate} does not exist"
        return None, {}, None
    if not candidate.is_file():
        return (candidate, {}, f"config file {candidate} is not a file") if named else (None, {}, None)
    try:
        key = str(candidate.resolve())
    except (OSError, RuntimeError):
        key = str(candidate.absolute())
    identity = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
    cached = _file_cache.get(key)
    if cached is not None and cached[0] == identity:
        return candidate, dict(cached[1] or {}), cached[2]
    try:
        values: Optional[Dict[str, Any]] = parse_config_text(candidate.read_text(encoding="utf-8"), str(candidate))
        error = None
    except ConfigError as problem:
        values, error = None, str(problem)
    except (OSError, UnicodeError) as problem:
        values, error = None, f"config file {candidate} cannot be read ({type(problem).__name__})"
    if len(_file_cache) > 32:
        _file_cache.clear()
    _file_cache[key] = (identity, values, error)
    return candidate, dict(values or {}), error


def passive_privacy_values(environ: Optional[Mapping[str, str]] = None, config_file: Optional[str] = None,
                           sources: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Environment, then config file, then defaults, for a process the agent has not configured.

    An unreadable file (bad syntax, or a missing ``FI_CONFIG_FILE``) never raises here: it
    is treated as hiding content (inputs, outputs and invocation parameters), because
    its intent cannot be known. Higher-precedence environment values still apply.
    """
    environ = os.environ if environ is None else environ
    path, file_values, error = _cached_file(config_file, environ)
    if error is not None:
        # The parser's messages name the file and line, never a value.
        _warn_once(("file", str(path), error),
                   "traceAI: %s; hiding inputs, outputs and invocation parameters until it is fixed", error)
        file_values = {key: True for key in FAIL_CLOSED_PRIVACY}
    source = f"file {path}" if error is None else f"file {path} (unreadable: content hidden)"

    def pick(key: str) -> Tuple[Any, str]:
        env = env_name(key)
        if environ.get(env) not in (None, ""):
            return environ[env], f"env {env}"
        if key in file_values:
            return file_values[key], source
        return None, "default"

    return privacy_values(pick, sources, strict=False)


def active_policy() -> Any:
    """The policy the agent pinned for this process (S2), or None before/without the agent."""
    manager = sys.modules.get("fi_instrumentation.auto._manager")
    state = getattr(manager, "_state", None)
    return getattr(state, "trace_config", None)


def default_privacy_values() -> Dict[str, Any]:
    """Base values for TraceConfig fields that code did not set explicitly."""
    policy = active_policy()
    if policy is not None:
        try:
            return {f.name: getattr(policy, f.name) for f in _privacy_fields()}
        except Exception:
            pass  # An unexpected policy object falls back to the sources themselves.
    return passive_privacy_values()
