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

---

## Releasing

A release is a pull request, like any other change. For the package you are releasing:

1. Bump `version` in its `pyproject.toml` (Python) or `package.json` (TypeScript). If the package
   carries a `__version__` (in `version.py` or `__init__.py`), set it to the same value: CI fails
   when the two disagree (`python3 .github/release/release_plan.py --check`).
2. Add the version's section to the package's `CHANGELOG.md` (`## [x.y.z] - YYYY-MM-DD`); it
   becomes the text of the GitHub release.
3. Merge to `dev`, and `dev` to `main` as usual.

When `main` changes, the [Release](.github/workflows/release.yml) workflow publishes every package
whose version is not on PyPI or npm yet: it builds the package, checks the distribution, installs it
in a clean environment and imports it, publishes it (the two core packages first), waits until the
registry serves the version, tags the commit (`traceai-livekit-v0.1.2`, `npm-traceai-openai-v0.1.13`)
and creates the GitHub release. A pull request that touches a version only shows what would be
published, in the run summary. `Actions > Release > Run workflow` releases by hand (optionally a
few named packages, or as a dry run).

Publishing needs credentials that an owner sets once: a PyPI trusted publisher on each project
(workflow `release-python.yml` of this repository) plus the repository variable
`PYPI_TRUSTED_PUBLISHING=true`, or the secret `PYPI_API_TOKEN`; and the secret `NPM_TOKEN` with
publish rights on `@traceai`. Until then the workflow builds, checks and keeps the artifacts, and
warns that nothing was published. Packages that must never be published are listed, with the
reason, in `.github/release/config.json`. Java (JitPack from git tags) and C# are released by hand.

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

