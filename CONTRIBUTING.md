# Contributing to traceAI

Thanks for your interest in contributing! 🎉

## Quick Start

1. **Fork & Clone**
   ```bash
   git clone https://github.com/YOUR_USERNAME/traceAI.git
   cd traceAI
   ```

2. **Create a branch**
   ```bash
   git checkout -b feature/your-feature
   ```

3. **Make changes & test**

4. **Commit** (use [Conventional Commits](https://www.conventionalcommits.org/))
   ```bash
   git commit -m "feat: add xyz support"
   ```

5. **Push & open a PR**
   ```bash
   git push origin feature/your-feature
   ```

---

## Development Setup

### Python

```bash
cd python
poetry install              # Install core
cd frameworks/openai        # Work on specific framework
poetry install
pytest                      # Run tests
```

**Code Quality:** Black, isort, flake8, mypy

### TypeScript

```bash
cd typescript
pnpm install                # Install all packages
pnpm run build              # Build
pnpm test                   # Run tests
```

**Code Quality:** ESLint, Prettier, TypeScript strict mode

---

## Adding a New Framework

### Python
```bash
mkdir python/frameworks/your-framework
cd python/frameworks/your-framework
```

Create structure:
```
traceai_your_framework/
├── __init__.py
├── instrumentation.py
└── version.py
tests/
examples/
pyproject.toml
CHANGELOG.md
README.md
```

### TypeScript
```bash
mkdir typescript/packages/traceai_your_framework
```

Create structure similar to existing packages, then update `pnpm-workspace.yaml`.

**Don't forget:** Update main README with your new integration!

### Compatibility definition of done

Register every package-language target in [the canonical registry](.github/compat/registry.json).
A manifest range is a declaration, not evidence that every admitted version works. An
integration with tests on disk but no executed contract remains **unqualified**.

Before marking a new integration qualified:

1. Name one accountable human owner, tier, exact upstream floor and companion tuple.
   Record runtime constraints and the source of each dependency/runtime declaration.
   Verify package metadata, runtime guards and documentation agree. Do not silently
   raise floors, remove historical families or add speculative caps to obtain green CI.
2. Exercise the real installed SDK over mocked HTTP, a loopback server or a deterministic
   local fixture. Do not replace the method/class that instrumentation hooks. Assert
   return values and errors, finished span count/parentage, model/provider/usage and
   privacy. Cover sync/async, tools, streaming and early close/cancellation where applicable.
3. Run exact floor, locked baseline, latest eligible supported stable and applicable
   historical/runtime boundaries. Record exact resolved versions, runtime, source SHA,
   dependency snapshot, suite digest, JUnit and Actions URL. "Latest eligible" uses a
   recorded 72-hour cooldown and excludes yanked releases; it is not absolute latest.
4. Validate built wheels/tarballs/modules and core from a clean consumer directory with
   no source shadowing. Distinguish source passes from installable released evidence.
   A development fix does not make a broken release compatible.
5. Wire the target into the existing planners, require nonzero execution and zero
   unexpected skips, and attach evidence. A successful install, an empty suite or a
   skipped job cannot qualify an integration. A scheduled success is required before
   advertising continuously checked support; a cron only on `dev` is dormant because
   this repository's default branch is `main`.
6. Publish an exact compatibility row with freshness and reproduction instructions.
   Keep unqualified, blocked, stale and quarantined targets visible. Do not infer
   TypeScript, Go, Java or .NET coverage from Python results.

Run declaration and gate checks from the repository root using Python 3.11 or newer:

```bash
python -m pip install jsonschema==4.23.0 packaging==24.2
python .github/compat/validate.py registry --root .
python -m unittest discover -s .github/compat -p 'test_validate.py' -v
python -m unittest discover -s .github/scripts -p 'test_*.py' -v
python -m unittest discover -s .github/integration-ci -p 'test_*.py' -v
```

Only the three Python pilot targets are enabled for floating discovery initially. They
remain `pilot_pending` until their full execution/declaration requirements are met.
The first Google GenAI offline contract is a baseline, not floor/latest qualification.
`regression_ci: true` explicitly identifies existing integration-workflow targets;
the presence of `contract_test` alone does not mean that workflow executes it.

### Temporary exact exceptions

`.github/python-tests-known-broken.txt` is legacy provenance/selection metadata only.
It no longer grants a package permission to fail. Existing failures without legitimate
scoped exceptions stay red until repaired; do not invent issues to restore a green badge.

A temporary exception in that target's registry record requires exact repository-relative
pytest node IDs (including parameter suffixes), named upstream constraints, interpreter
implementation/range, expected failure/error outcome and normalized failure signature,
a real linked GitHub issue, an accountable human, added/expiry dates (at most 14 days)
and exit criteria. Collection/install failures and runner crashes are never quarantined.
Matched outcomes are **quarantined**, not compatible. Unrelated failures, changed failure
signatures, empty suites and unexpected skips fail; unexpected passes fail until the
exception is removed. Exceptions continue executing. Remove repaired legacy-list lines
and exceptions in the repair PR.

JUnit must include repository-relative `file` attributes: run pytest with
`--rootdir="$PWD" -o junit_family=xunit1 --junitxml=<path>`. Preserve every own/core suite.
The compatibility validator normalizes failures deterministically; do not copy arbitrary
runner text or secrets into exception signatures. Schema-only checks do not verify that
an issue exists: reviewers must verify the linked issue and its accountable owner.

Install/test jobs must have no provider keys, publishing credentials or write tokens.
Pointing exporters at `127.0.0.1:9` is supplementary, **not** network isolation. Offline
SDK jobs execute in a Linux network namespace with loopback only and fail if isolation
cannot be established. Live provider calls and package publishing are not part of these
checks. Reporting/issue writes belong to a separate trusted job, never dependency execution.

---

## Guidelines

- ✅ Write tests for all changes
- ✅ Follow existing code style
- ✅ Update documentation
- ✅ Update CHANGELOG.md
- ✅ Keep PRs focused and small

---

## Need Help?

- 💬 [Open a Discussion](https://github.com/future-agi/traceAI/discussions)
- 🐛 [Report a Bug](https://github.com/future-agi/traceAI/issues)
- 💡 [Request a Feature](https://github.com/future-agi/traceAI/issues)

---

**Happy Contributing! 🚀**

