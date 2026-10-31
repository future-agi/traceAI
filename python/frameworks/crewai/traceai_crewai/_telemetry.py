"""Disable CrewAI's separate native exporter without disabling the OTel SDK."""

from importlib import import_module
from inspect import getattr_static
from typing import Any, List, Tuple


class _NativeTelemetryGuard:
    """Reversible, version-aware opt-out while traceAI owns CrewAI tracing.

    Instrument before constructing agents/crews. This cannot retract telemetry
    queued before instrumentation or stop unrelated application exporters.
    """

    def __init__(self) -> None:
        self._originals: List[Tuple[Any, str, Any, bool]] = []

    def _replace(self, owner: Any, name: str, replacement: Any) -> None:
        owned = name in vars(owner)
        self._originals.append((owner, name, getattr_static(owner, name), owned))
        setattr(owner, name, replacement)

    def enable(self) -> None:
        telemetry = import_module("crewai.telemetry").Telemetry
        classes = [telemetry]
        # Maintained CrewAI factors the exporter/disable checks into crewai-core.
        try:
            classes.append(import_module("crewai_core.telemetry").Telemetry)
        except ModuleNotFoundError as exc:
            if exc.name not in ("crewai_core", "crewai_core.telemetry"):
                raise
        for cls in classes:
            if hasattr(cls, "_is_telemetry_disabled"):
                self._replace(
                    cls, "_is_telemetry_disabled", classmethod(lambda cls: True)
                )
            else:
                # CrewAI 0.41.1 ignores disable flags and creates an HTTP exporter
                # in __init__. Do not construct that exporter in the first place.
                def disabled(instance: Any) -> None:
                    instance.ready = False
                    instance.trace_set = False

                self._replace(cls, "__init__", disabled)
                # Also stop future calls on instances constructed before instrument().
                for name in (
                    "set_tracer",
                    "crew_creation",
                    "task_started",
                    "task_ended",
                    "tool_repeated_usage",
                    "tool_usage",
                    "tool_usage_error",
                    "crew_execution_span",
                    "end_crew",
                ):
                    if hasattr(cls, name):
                        self._replace(cls, name, lambda *args, **kwargs: None)

    def disable(self) -> None:
        for owner, name, original, owned in reversed(self._originals):
            if owned:
                setattr(owner, name, original)
            else:
                delattr(owner, name)
        self._originals.clear()
