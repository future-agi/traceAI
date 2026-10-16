"""Process configuration, read once at startup from the environment.

The Future AGI project and keys are process config: a webhook payload can never
choose a different project.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Optional

from .verify import DEFAULT_TOLERANCE_SECS

DEFAULT_FI_BASE_URL = "https://api.futureagi.com"
DEFAULT_STATE_PATH = os.path.join("~", ".traceai-elevenlabs-agents", "forwarded.sqlite3")
DEFAULT_ELEVENLABS_BASE_URL = "https://api.elevenlabs.io"

_TRUE = ("1", "true", "yes", "on")


def _flag(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() in _TRUE


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ForwarderConfig:
    fi_api_key: str = field(repr=False)
    fi_secret_key: str = field(repr=False)
    project_name: str
    fi_base_url: str = DEFAULT_FI_BASE_URL
    webhook_secret: str = field(default="", repr=False)
    capture_content: bool = False
    state_path: str = DEFAULT_STATE_PATH
    signature_tolerance_secs: int = DEFAULT_TOLERANCE_SECS
    paused: bool = False
    elevenlabs_api_key: str = field(default="", repr=False)
    elevenlabs_base_url: str = DEFAULT_ELEVENLABS_BASE_URL

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None, *, require_webhook_secret: bool = True) -> "ForwarderConfig":
        env = os.environ if env is None else env
        missing = [n for n in ("FI_API_KEY", "FI_SECRET_KEY", "FI_PROJECT_NAME") if not env.get(n)]
        if require_webhook_secret and not env.get("ELEVENLABS_WEBHOOK_SECRET"):
            missing.append("ELEVENLABS_WEBHOOK_SECRET")
        if missing:
            raise ConfigError("missing environment variable(s): " + ", ".join(missing))
        tolerance_raw = env.get("FI_ELEVENLABS_SIGNATURE_TOLERANCE_SECS", "").strip()
        try:
            tolerance = int(tolerance_raw) if tolerance_raw else DEFAULT_TOLERANCE_SECS
        except ValueError:
            raise ConfigError("FI_ELEVENLABS_SIGNATURE_TOLERANCE_SECS must be an integer") from None
        if tolerance <= 0:
            raise ConfigError("FI_ELEVENLABS_SIGNATURE_TOLERANCE_SECS must be positive")
        state_path = env.get("FI_ELEVENLABS_STATE_PATH", "").strip() or DEFAULT_STATE_PATH
        if state_path != ":memory:":
            state_path = os.path.expanduser(state_path)
        return cls(
            fi_api_key=env["FI_API_KEY"],
            fi_secret_key=env["FI_SECRET_KEY"],
            project_name=env["FI_PROJECT_NAME"],
            fi_base_url=env.get("FI_BASE_URL", "").strip() or DEFAULT_FI_BASE_URL,
            webhook_secret=env.get("ELEVENLABS_WEBHOOK_SECRET", ""),
            capture_content=_flag(env, "FI_ELEVENLABS_CAPTURE_CONTENT"),
            state_path=state_path,
            signature_tolerance_secs=tolerance,
            paused=_flag(env, "FI_ELEVENLABS_PAUSED"),
            elevenlabs_api_key=env.get("ELEVENLABS_API_KEY", ""),
            elevenlabs_base_url=env.get("FI_ELEVENLABS_API_BASE_URL", "").strip() or DEFAULT_ELEVENLABS_BASE_URL,
        )

    def secrets(self) -> "tuple[str, ...]":
        """Values that must never appear in a span, a log line or a response body."""
        return tuple(
            s for s in (self.fi_api_key, self.fi_secret_key, self.webhook_secret, self.elevenlabs_api_key) if s
        )
