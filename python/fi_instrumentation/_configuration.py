"""Reads ``futureagi.toml`` and Future AGI credential pairs for the zero-code agent.

The file uses the flat TOML subset every traceAI SDK can read without a TOML library: one
``key = value`` per line, where a value is a quoted string, ``true``/``false``, a number or a
one-line array of strings. ``#`` starts a comment and ``[tables]`` are skipped. Each key maps to
the environment variable ``FI_<KEY>`` (``debug`` maps to ``FI_AUTO_DEBUG``). Values are never logged.
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

logger = logging.getLogger("fi_instrumentation.auto")

CONFIG_FILE_ENV = "FI_CONFIG_FILE"
DEFAULT_CONFIG_FILE = "futureagi.toml"
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")
_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$")


class ConfigError(ValueError):
    """An argument, environment variable or config-file value is invalid."""


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
        values = []
        for item in (part.strip() for part in inner.split(",")):
            if not item:
                continue
            if not (item.startswith('"') and item.endswith('"') and len(item) >= 2):
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


def parse_config_text(text: str, source: str = DEFAULT_CONFIG_FILE,
                      ignored_keys: Iterable[str] = ()) -> Dict[str, Any]:
    """Parse the flat TOML subset. Keys in ``ignored_keys`` are skipped without being read."""
    ignored = set(ignored_keys)
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
        key = found.group(1).lower()
        if key in ignored:
            continue
        values[key] = _parse_value(found.group(2), f"{source}:{number}")
    return values


def env_name(key: str) -> str:
    """The environment variable for a config-file key."""
    return "FI_AUTO_DEBUG" if key == "debug" else f"FI_{key.upper()}"


def config_path(path: Optional[str] = None, environ: Optional[Mapping[str, str]] = None) -> Tuple[Path, bool]:
    """The config file to read, and whether it was named explicitly (so it must exist)."""
    environ = os.environ if environ is None else environ
    named = path or environ.get(CONFIG_FILE_ENV)
    return Path(named or DEFAULT_CONFIG_FILE), bool(named)


def load_config_file(path: Optional[str] = None, environ: Optional[Mapping[str, str]] = None,
                     ignored_keys: Iterable[str] = ()) -> Tuple[Optional[Path], Dict[str, Any]]:
    """Read the named file, or ``./futureagi.toml`` when it exists. A named file must exist."""
    candidate, named = config_path(path, environ)
    if not candidate.is_file():
        if named:
            raise ConfigError(f"config file {candidate} does not exist")
        return None, {}
    try:
        text = candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ConfigError(f"config file {candidate} cannot be read ({type(error).__name__})") from None
    return candidate, parse_config_text(text, str(candidate), ignored_keys)


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


def credential_pair(values: Mapping[str, Any], source: str) -> Optional[Tuple[str, str]]:
    """The complete ``(api_key, secret_key)`` pair from one source, or None.

    A source that sets only one half is ignored as a whole, with one warning that names the
    missing half and the source, never the values.
    """
    api, secret = values.get("api_key"), values.get("secret_key")
    if api and secret:
        return str(api), str(secret)
    if api or secret:
        logger.warning("traceAI: ignoring an incomplete Future AGI key pair from %s (missing %s)",
                       source, "FI_SECRET_KEY" if api else "FI_API_KEY")
    return None
