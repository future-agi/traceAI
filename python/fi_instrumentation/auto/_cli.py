"""``fi-instrument``: run a program with Future AGI tracing, and inspect or set up an environment.

    fi-instrument python app.py          run a program with the agent loaded (also: fi-instrument run ...)
    fi-instrument doctor                 configuration, integrations and collector check
    fi-instrument bootstrap [--install]  find libraries without their traceAI package and install them
    fi-instrument init-config            write a futureagi.toml template
    fi-instrument validate               check the configuration and credentials; exit 1 on a problem
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from fi_instrumentation.auto import _manager
from fi_instrumentation.auto._config import CONFIG_TEMPLATE, DEFAULT_CONFIG_FILE, AutoConfig, ConfigError, resolve

SITECUSTOMIZE_DIR = Path(__file__).resolve().parent / "_sitecustomize"
COMMANDS = ("run", "doctor", "bootstrap", "init-config", "validate")


def _run(argv: Sequence[str]) -> int:
    if not argv:
        print("fi-instrument: give the program to run, e.g. fi-instrument python app.py", file=sys.stderr)
        return 2
    env = dict(os.environ)
    paths = [str(SITECUSTOMIZE_DIR)] + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["FI_AUTO_INSTRUMENT"] = "true"
    program = shutil.which(argv[0]) or argv[0]
    try:
        os.execve(program, list(argv), env)
    except OSError as error:
        print(f"fi-instrument: cannot run {argv[0]}: {error}", file=sys.stderr)
        return 127
    return 0  # pragma: no cover - execve does not return


def check_collector(settings: AutoConfig, timeout: float = 5.0) -> Tuple[bool, str]:
    """Send an empty OTLP export with the configured keys and report whether it was accepted."""
    import requests
    from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY, attach, detach, set_value

    from fi_instrumentation.otel import _normalized_endpoint

    if not settings.has_credentials:
        return False, "FI_API_KEY and FI_SECRET_KEY are not set"
    _, endpoint = _normalized_endpoint(settings.base_url) if settings.base_url else _normalized_endpoint(None)
    token = attach(set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
    try:
        with requests.Session() as session:
            response = session.post(
                endpoint,
                data=b"",
                headers={"Content-Type": "application/x-protobuf", "X-Api-Key": settings.api_key or "",
                         "X-Secret-Key": settings.secret_key or ""},
                timeout=timeout,
            )
    except requests.RequestException as error:
        return False, f"{endpoint} unreachable: {type(error).__name__}"
    finally:
        detach(token)
    code = response.status_code
    if code in (401, 403):
        return False, f"{endpoint} rejected the keys (HTTP {code})"
    if code == 404 or code >= 500:
        return False, f"{endpoint} answered HTTP {code}; check FI_BASE_URL"
    if code >= 400:
        return True, f"{endpoint} is reachable and did not reject the keys (HTTP {code} for an empty export)"
    return True, f"{endpoint} accepted a test export (HTTP {code})"


def _doctor(args: argparse.Namespace) -> int:
    try:
        settings = resolve(config_file=args.config)
    except ConfigError as error:
        print(f"Configuration error: {error}")
        return 1
    statuses = _manager.plan(settings)
    collector = None if args.offline else check_collector(settings)
    if args.json:
        print(json.dumps({
            "config": settings.masked(),
            "integrations": [s.as_dict() for s in statuses],
            "collector": None if collector is None else {"ok": collector[0], "detail": collector[1]},
        }, indent=2, default=str))
        return 0 if collector is None or collector[0] else 1
    print("Configuration:")
    for key, entry in settings.masked().items():
        print(f"  {key:<26} {entry['value']!s:<32} ({entry['source']})")
    print()
    print(_manager.format_report(statuses, include_absent=args.all))
    missing = [s for s in statuses if s.status == _manager.MISSING_INSTRUMENTOR]
    if missing:
        print()
        print("Richer spans are available for installed libraries; run `fi-instrument bootstrap --install`.")
    if collector is not None:
        print()
        print(("Collector: OK, " if collector[0] else "Collector: PROBLEM, ") + collector[1])
    if not settings.has_credentials:
        print()
        print("Set FI_API_KEY and FI_SECRET_KEY to start exporting traces.")
    return 0 if collector is None or collector[0] else 1


# Libraries in these categories are often used for other things (boto3 for S3, redis for
# caching, a web framework without AI routes), so bootstrap suggests them only with --all.
_OPT_IN_CATEGORIES = ("vector_db", "web")


def _missing_packages(settings: AutoConfig, include_all: bool) -> List[str]:
    packages = []
    for status in _manager.plan(settings):
        if status.status != _manager.MISSING_INSTRUMENTOR:
            continue
        if status.category in _OPT_IN_CATEGORIES and not include_all:
            continue
        if status.package not in packages:
            packages.append(status.package)
    return packages


def _bootstrap(args: argparse.Namespace) -> int:
    try:
        settings = resolve(config_file=args.config)
    except ConfigError as error:
        print(f"Configuration error: {error}")
        return 1
    packages = _missing_packages(settings, include_all=args.all)
    if not packages:
        print("Every installed library already has its traceAI package.")
        return 0
    command = [sys.executable, "-m", "pip", "install", *packages]
    if not args.install:
        print("Install these packages for richer spans:")
        print("  " + " ".join(command[1:] if shutil.which("pip") is None else ["pip", "install", *packages]))
        return 0
    print("Running: " + " ".join(command))
    return subprocess.call(command)


def _init_config(args: argparse.Namespace) -> int:
    path = Path(args.path or DEFAULT_CONFIG_FILE)
    if path.exists() and not args.force:
        print(f"{path} already exists; pass --force to overwrite it.")
        return 1
    project = args.project or Path.cwd().name
    path.write_text(CONFIG_TEMPLATE.format(project_name=project), encoding="utf-8")
    print(f"Wrote {path}. Set FI_API_KEY and FI_SECRET_KEY in the environment, then run: fi-instrument python app.py")
    return 0


def _validate(args: argparse.Namespace) -> int:
    try:
        settings = resolve(config_file=args.config)
    except ConfigError as error:
        print(f"Invalid: {error}")
        return 1
    if args.offline:
        print("Configuration is valid." if settings.has_credentials else "Configuration is valid; credentials are not set.")
        return 0 if settings.has_credentials else 1
    ok, detail = check_collector(settings)
    print(("Valid: " if ok else "Invalid: ") + detail)
    return 0 if ok else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fi-instrument", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="run a program with the agent loaded")
    run.add_argument("program", nargs=argparse.REMAINDER)
    doctor = sub.add_parser("doctor", help="show configuration, integrations and collector status")
    doctor.add_argument("--json", action="store_true", help="machine-readable output")
    doctor.add_argument("--all", action="store_true", help="also list libraries that are not installed")
    doctor.add_argument("--offline", action="store_true", help="skip the collector check")
    doctor.add_argument("--config", help="config file (default: FI_CONFIG_FILE or ./futureagi.toml)")
    boot = sub.add_parser("bootstrap", help="find (and install) traceAI packages for installed libraries")
    boot.add_argument("--install", action="store_true", help="install them with pip")
    boot.add_argument("--all", action="store_true",
                      help="also cover vector databases and web servers (request spans)")
    boot.add_argument("--config", help="config file")
    init = sub.add_parser("init-config", help="write a futureagi.toml template")
    init.add_argument("--project", help="project name (default: this directory's name)")
    init.add_argument("--path", help=f"where to write it (default: {DEFAULT_CONFIG_FILE})")
    init.add_argument("--force", action="store_true", help="overwrite an existing file")
    validate = sub.add_parser("validate", help="check configuration and credentials")
    validate.add_argument("--offline", action="store_true", help="do not contact the collector")
    validate.add_argument("--config", help="config file")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        return _run(argv)
    args = _parser().parse_args(argv)
    if args.command == "run":
        return _run(args.program[1:] if args.program[:1] == ["--"] else args.program)
    handlers = {"doctor": _doctor, "bootstrap": _bootstrap, "init-config": _init_config, "validate": _validate}
    if args.command in handlers:
        return handlers[args.command](args)
    _parser().print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
