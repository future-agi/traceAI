"""Version-1 shared JSON configuration; no Node process or IPC at runtime.

Only Future AGI-owned files are read. Hermes supplies its active profile path and
scoped environment accessor. Invalid/untrusted configuration fails closed for
tracing, never for the agent. The env-only Python installation stays independent.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

MAX_BYTES = 65536
DEFAULT_ENDPOINT = "https://api.futureagi.com/tracer/v1/traces"


def read_json(path: Path, private: bool = True) -> dict:
    try:
        for parent in reversed(path.absolute().parents):
            st = parent.lstat()
            if not stat.S_ISDIR(st.st_mode) or (st.st_mode & 0o022 and not st.st_mode & stat.S_ISVTX):
                raise ValueError("UNSAFE_DIRECTORY")
        # O_NOFOLLOW alone does not protect against an unwritten FIFO: open
        # must return before fstat can reject special files.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1
                or st.st_size > MAX_BYTES or (private and st.st_mode & 0o077)):
            raise ValueError("UNSAFE_FILE")
        raw = os.read(fd, MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("OVERSIZE_FILE")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("INVALID_CONFIG")
        return data
    finally:
        os.close(fd)


def boolean(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if value in ("true", "1", "yes", "on"):
        return True
    if value in ("false", "0", "no", "off"):
        return False
    raise ValueError("INVALID_BOOLEAN")


def endpoint(value: str) -> str:
    u = urlsplit(value)
    if (u.scheme != "https" and not (u.scheme == "http" and u.hostname in {"localhost", "127.0.0.1", "::1"})
            or not u.hostname or u.username or u.password or u.query or u.fragment):
        raise ValueError("UNSAFE_ENDPOINT")
    return value


def settings(value: dict) -> dict:
    if value == {}:
        return {}
    if not isinstance(value, dict):
        raise ValueError("INVALID_CONFIG")
    allowed = {"schemaVersion", "enabled", "profile", "projectName", "captureContent", "userId",
               "environment", "tags", "transport"}
    if value.get("schemaVersion") != 1 or set(value) - allowed:
        raise ValueError("INVALID_CONFIG")
    for k in ("enabled", "captureContent"):
        if k in value and not isinstance(value[k], bool):
            raise ValueError("INVALID_CONFIG")
    for k in ("profile", "projectName", "userId", "environment"):
        if k in value and (not isinstance(value[k], str) or not 0 < len(value[k]) <= 256
                           or any(ord(c) < 32 for c in value[k])):
            raise ValueError("INVALID_CONFIG")
    if "tags" in value and (not isinstance(value["tags"], list) or len(value["tags"]) > 32
                            or any(not isinstance(t, str) or len(t) > 128 for t in value["tags"])):
        raise ValueError("INVALID_CONFIG")
    if "transport" in value:
        transport = value["transport"]
        if (not isinstance(transport, dict) or set(transport) - {"endpoint", "baseUrl"}
                or any(not isinstance(v, str) for v in transport.values())):
            raise ValueError("INVALID_CONFIG")
        if "endpoint" in transport:
            endpoint(transport["endpoint"])
        if "baseUrl" in transport:
            endpoint(transport["baseUrl"])
            if urlsplit(transport["baseUrl"]).path not in {"", "/"}:
                raise ValueError("BASE_URL_MUST_BE_ORIGIN")
    return value


def header_value(value: Any) -> str:
    """Reject malformed HTTP headers without ever including their value in an error."""
    if (not isinstance(value, str) or not value or len(value) > 4096
            or value[0].isspace()
            or any(ord(c) < 32 or ord(c) == 127 or ord(c) > 255 for c in value)):
        raise ValueError("INVALID_HEADER_VALUE")
    return value


def resolve(get: Callable[[str], str | None], *, host: Path | None = None,
            project: Path | None = None) -> dict:
    # Query the scoped accessor first: an unscoped gateway must not fall back to
    # launch-profile files or os.environ credentials.
    api, secret = get("FI_API_KEY"), get("FI_SECRET_KEY")
    if (api is None) != (secret is None) or (api is not None and not (api and secret)):
        raise ValueError("INCOMPLETE_CREDENTIAL_OVERRIDE")
    home = Path.home()
    config_dir = Path(get("FI_AGENT_CONFIG_DIR") or str(Path(get("XDG_CONFIG_HOME") or home / ".config") / "futureagi/agents"))
    state_dir = Path(get("XDG_STATE_HOME") or home / ".local/state") / "futureagi/agents"
    if host is None:
        try:
            from hermes_constants import get_hermes_home
            host = get_hermes_home()
        except ImportError:
            host = Path(get("HERMES_HOME") or home / ".hermes")
    user = settings(read_json(Path(host) / "futureagi.json", private=False))
    issues, repository = [], {}
    if project:
        try:
            repository = settings(read_json(project / ".futureagi/agents.json", private=False).get("hermes", {}))
        except Exception:
            # External/shared volumes cannot supply a trusted project layer,
            # but must not disable an independently configured user profile.
            issues.append("PROJECT_CONFIG_IGNORED")
    store = read_json(config_dir / "credentials.json")
    if store and (store.get("schemaVersion") != 1 or not isinstance(store.get("profiles"), dict)):
        raise ValueError("INVALID_CREDENTIAL_STORE")
    binding = hashlib.sha256(str(project.resolve()).encode()).hexdigest() if project else None
    profile_id = repository.get("profile", user.get("profile"))
    candidate = store.get("profiles", {}).get(profile_id, {})
    # The shared default is not consent from this Hermes home. A repository may
    # select saved credentials only through an approval for this canonical path.
    project_approved = bool(binding and candidate.get("projectBinding") == binding)
    home_approved = bool(user.get("profile") == profile_id and profile_id
                         and not candidate.get("projectBinding"))
    host_binding = hashlib.sha256(os.path.abspath(host).encode()).hexdigest()
    matching_home = not candidate.get("hostBinding") or candidate["hostBinding"] == host_binding
    saved = candidate if matching_home and (home_approved or project_approved) else {}
    merged = {"enabled": bool(saved) or bool(api and secret), "captureContent": saved.get("captureContent", True),
              "projectName": saved.get("projectName", "hermes-agent"), **user, **repository}
    project_name = get("FI_PROJECT_NAME") or merged["projectName"]
    transport = merged.get("transport", {})
    env_base = get("FI_BASE_URL")
    exact = get("FI_OTLP_ENDPOINT") or (None if env_base else transport.get("endpoint"))
    base = None if exact else env_base or transport.get("baseUrl")
    if base and urlsplit(base).path not in {"", "/"}:
        raise ValueError("BASE_URL_MUST_BE_ORIGIN")
    url = exact or (base.rstrip("/") + "/tracer/v1/traces" if base else saved.get("endpoint") or DEFAULT_ENDPOINT)
    endpoint(url)
    saved_approval = bool(saved and saved.get("endpoint") == url and saved.get("projectName") == project_name)
    # Independently resolve the trusted user/environment route, just like the
    # unified core. Inherited keys do not approve a repository project or profile
    # any more than they approve its collector URL.
    trusted_profile_id = user.get("profile")
    trusted_saved = store.get("profiles", {}).get(trusted_profile_id, {})
    if ((trusted_saved.get("hostBinding") and trusted_saved["hostBinding"] != host_binding)
            or (trusted_saved.get("projectBinding") and trusted_saved["projectBinding"] != binding)):
        trusted_saved = {}
    trusted_transport = user.get("transport", {})
    trusted_exact = get("FI_OTLP_ENDPOINT") or (None if env_base else trusted_transport.get("endpoint"))
    trusted_base = None if trusted_exact else env_base or trusted_transport.get("baseUrl")
    trusted_url = trusted_exact or (trusted_base.rstrip("/") + "/tracer/v1/traces" if trusted_base
                                   else trusted_saved.get("endpoint") or DEFAULT_ENDPOINT)
    trusted_project = get("FI_PROJECT_NAME") or user.get("projectName") or trusted_saved.get("projectName", "hermes-agent")
    approved = (bool(saved_approval and project_approved)
                or (profile_id == trusted_profile_id and url == trusted_url and project_name == trusted_project)) if api and secret else saved_approval
    capture_env = get("FI_AGENT_CAPTURE_CONTENT")
    legacy_capture = get("FI_HERMES_CAPTURE_CONTENT")
    # Privacy consent comes only from the host, an approved protected profile or
    # explicit environment settings, never from the repository's mere existence.
    ceiling = boolean(capture_env, boolean(legacy_capture, (user.get("captureContent", saved.get("captureContent", False)) and saved.get("captureContent", True))))
    capture = ceiling
    if capture_env is None and legacy_capture is None:
        capture = bool(merged.get("captureContent", ceiling)) and ceiling
    manifest = read_json(config_dir / "installation.json")
    enabled = boolean(get("FI_AGENT_ENABLED"), merged.get("enabled", False)) and approved
    enabled = enabled and not manifest.get("disabled", {}).get("hermes", False)
    hide_inputs = not capture or any(boolean(get(k)) for k in ("FI_HIDE_INPUTS", "FI_HIDE_INPUT_TEXT", "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_INPUT_IMAGES"))
    hide_outputs = not capture or any(boolean(get(k)) for k in ("FI_HIDE_OUTPUTS", "FI_HIDE_OUTPUT_TEXT", "FI_HIDE_OUTPUT_MESSAGES"))
    api, secret = api or saved.get("apiKey"), secret or saved.get("secretKey")
    for value in (api, secret):
        if value is not None:
            header_value(value)
    header_value(project_name)
    generation = manifest.get("generations", {}).get("hermes", 0)
    # Cross-language v1 tuple. Only its digest is persisted: no credentials or
    # private paths in diagnostics. Account rotation also invalidates acceptance.
    diagnostic_binding = hashlib.sha256(json.dumps([
        "hermes-delivery-v1", host_binding, profile_id, url, project_name,
        binding, generation, api, secret,
    ], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return dict(enabled=enabled, api_key=api, secret_key=secret, issues=issues,
                endpoint=url, project_name=project_name, capture_content=capture,
                max_chars=max(256, min(20000, int(get("FI_HERMES_MAX_CHARS") or 20000))),
                hide_inputs=hide_inputs, hide_outputs=hide_outputs,
                hide_errors=not capture or boolean(get("FI_HIDE_ERROR_DETAILS"), hide_inputs or hide_outputs),
                user_id=get("FI_USER_ID") or merged.get("userId") or saved.get("userId"),
                manifest=config_dir / "installation.json", state=state_dir / ("hermes-" + diagnostic_binding + ".json"),
                diagnostic_binding=diagnostic_binding,
                generation=generation, user_file=Path(host) / "futureagi.json")


def permitted(config: dict) -> bool:
    """Fence pending batches too. Never re-resolve their destination/credentials."""
    try:
        if os.environ.get("FI_AGENT_ENABLED") in {"false", "0"}:
            return False
        manifest = read_json(config["manifest"])
        user = read_json(config["user_file"], private=False)
        return (config["enabled"] and user.get("enabled", True)
                and not manifest.get("disabled", {}).get("hermes", False)
                and manifest.get("generations", {}).get("hermes", 0) == config["generation"])
    except Exception:
        return False
