"""Version guard for AG2 Classic (``import autogen``, 0.14.x).

Three different projects are easy to confuse:

* ``autogen`` 0.14.x (AG2 Classic, ``ag2ai/ag2classic``): this package.
* ``ag2`` 1.x (AG2, ``import ag2``): use ``traceAI-ag2``.
* ``autogen-agentchat`` (Microsoft AutoGen): use ``traceAI-autogen``.

``pyautogen`` is an older alias and is out of scope for this release.

Installed-layout facts this guard is written against (read, not assumed):

* ``autogen==0.14.1`` is a standalone distribution that ships the ``autogen``
  module itself (Source: ``ag2ai/ag2classic``).
* ``autogen==0.14.0`` is an 8-file alias whose only runtime requirement is
  ``ag2==0.14.0``; the ``autogen`` module is shipped by the ``ag2`` 0.14.0
  distribution. Its ``autogen/opentelemetry`` tree is byte-identical to the
  0.14.1 one. That exact pairing is accepted.
* ``ag2`` 1.x ships only an ``ag2`` top-level module, never ``autogen``.

The guard runs before anything is instrumented. It never imports ``ag2`` and
never imports Microsoft AutoGen.
"""

from __future__ import annotations

import importlib
import importlib.util
from typing import Callable, Iterable, List, Mapping, Optional

SUPPORTED_SERIES = "0.14"

_GUIDANCE = (
    "traceAI-ag2-classic supports only AG2 Classic: the `autogen` distribution 0.14.x "
    "(import `autogen`, ships `autogen.opentelemetry`). "
    "Three packages are easy to confuse: "
    "`autogen` 0.14.x -> traceAI-ag2-classic; "
    "`ag2` 1.x -> traceAI-ag2; "
    "`autogen-agentchat` (Microsoft AutoGen) -> traceAI-autogen. "
    "`pyautogen` is not supported by this package."
)


class AG2ClassicCompatibilityError(RuntimeError):
    """Raised when the importable ``autogen`` is not AG2 Classic 0.14.x."""

    def __init__(self, reason: str) -> None:
        super().__init__("{0} {1}".format(reason, _GUIDANCE))
        self.reason = reason


def _normalize(name: str) -> str:
    return name.strip().lower().replace("_", "-").replace(".", "-")


def _default_distributions_for(top_level: str) -> List[str]:
    from importlib import metadata

    try:
        mapping: Mapping[str, Iterable[str]] = metadata.packages_distributions()
    except Exception:  # pragma: no cover - defensive, metadata is stdlib on 3.10+
        return []
    return list(mapping.get(top_level, []))


def _default_distribution_version(name: str) -> Optional[str]:
    from importlib import metadata

    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _default_find_spec(name: str) -> Optional[object]:
    try:
        return importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return None


def _is_supported(version: Optional[str]) -> bool:
    if not version:
        return False
    return version == SUPPORTED_SERIES or version.startswith(SUPPORTED_SERIES + ".")


def check_autogen_classic(
    *,
    distributions_for: Callable[[str], Iterable[str]] = _default_distributions_for,
    distribution_version: Callable[[str], Optional[str]] = _default_distribution_version,
    import_module: Callable[[str], object] = importlib.import_module,
    find_spec: Callable[[str], Optional[object]] = _default_find_spec,
) -> str:
    """Return the installed AG2 Classic version, or raise.

    Raises :class:`AG2ClassicCompatibilityError` when:

    * the top-level ``autogen`` module is installed by ``pyautogen``;
    * it is installed by ``ag2``, except the ``autogen==0.14.x`` alias layout
      (``autogen`` 0.14.x distribution plus ``ag2`` 0.14.x);
    * ``autogen`` cannot be imported (for example only ``ag2`` 1.x or
      Microsoft ``autogen-agentchat`` is installed);
    * ``autogen.version.__version__`` or ``autogen.opentelemetry`` is missing;
    * ``autogen.version.__version__`` does not start with ``0.14``.

    The keyword arguments exist so tests can supply a fake module layout
    without installing any of the other packages.
    """
    owners = sorted({_normalize(d) for d in distributions_for("autogen")})
    if "pyautogen" in owners:
        raise AG2ClassicCompatibilityError("The top-level `autogen` module is installed by `pyautogen`.")
    if "ag2" in owners:
        ag2_version = distribution_version("ag2")
        alias_version = distribution_version("autogen")
        if not (_is_supported(ag2_version) and _is_supported(alias_version)):
            raise AG2ClassicCompatibilityError(
                "The top-level `autogen` module is installed by `ag2` {0} (`autogen` distribution: {1}).".format(
                    ag2_version or "unknown", alias_version or "not installed"
                )
            )

    try:
        import_module("autogen")
    except ImportError as error:
        hints = []
        if find_spec("ag2") is not None:
            hints.append("`ag2` (AG2 1.x, `import ag2`) is installed")
        if find_spec("autogen_agentchat") is not None:
            hints.append("Microsoft AutoGen (`autogen_agentchat`) is installed")
        hint = (" " + "; ".join(hints) + ".") if hints else ""
        raise AG2ClassicCompatibilityError("`autogen` is not importable ({0}).{1}".format(error, hint)) from error

    try:
        version_module = import_module("autogen.version")
        version = str(getattr(version_module, "__version__"))
    except (ImportError, AttributeError) as error:
        raise AG2ClassicCompatibilityError(
            "`autogen.version.__version__` was not found, so this is not the AG2 Classic layout."
        ) from error

    if find_spec("autogen.opentelemetry") is None:
        raise AG2ClassicCompatibilityError("`autogen` {0} has no `autogen.opentelemetry` module.".format(version))

    if not _is_supported(version):
        raise AG2ClassicCompatibilityError(
            "`autogen` {0} is installed; only {1}.x is supported.".format(version, SUPPORTED_SERIES)
        )

    return version
