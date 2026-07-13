## [0.1.9] - 2026-07-13
### Fixed
- Fix `crew.kickoff()` crashing with `TypeError: '_NotSpecified' object is not
  iterable` when a `Task` is created without an explicit `context`. CrewAI
  defaults `Task.context` to a `NOT_SPECIFIED` sentinel that is truthy but not
  iterable, so the wrapper now guards on type before iterating. Affected `crewai`
  0.203.2 and 1.15.1; the `context=[]` workaround is no longer needed. The
  emitted `crew_tasks` payload is unchanged for every input that already worked.

  Known limitation: an unspecified context is recorded as `null`, the same as an
  explicit `None`/`[]`. CrewAI treats these differently — unspecified means "use
  every upstream task's output", whereas `None`/`[]` mean "no context" — so
  `null` does not distinguish the two. This matches the value the span carried
  before CrewAI introduced the sentinel.
- Fix stale `__version__` in `traceai_crewai/version.py` (was `0.1.0` while the
  published wheel was `0.1.8`), which mislabelled the instrumentation scope
  version on every span.

## [0.1.6] - 2025-05-29
### Feature
- Updated dependencies to the latest versions.

## [0.1.5] - 2025-05-23
### Feature
- Added support for FutureAGI's protect

## [0.1.4] - 2025-05-08
### Feature
- Updated dependencies to the latest versions.

## [0.1.3] - 2025-04-14
### Changed
- Updated dependencies to the latest versions.
