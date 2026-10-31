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

Use Conventional Commit PR titles **and commits** (`fix(openai): ...`, `feat: ...`).
Send changes to **dev**. A maintainer moves dev to **main**; release-please then maintains
one release PR with per-package version bumps and changelogs. Do not hand-bump routine
changes. Merging the release PR publishes the changed versions through the build/verify/
publish pipeline, with Python and npm core packages first.

The publisher is the only owner of tags and GitHub releases; release-please does not tag.
Python, npm, Java (Maven Central under `com.futureagi`), Go module tags and NuGet are covered.
Go begins at 0.1.0. JitPack remains available for Java commit previews.

See [RELEASING.md](RELEASING.md) for the one-time owner checklist, exact secret names,
PyPI trusted publishers (**workflow `release.yml`, not `release-python.yml`**), manual
recovery and dry runs. Pull requests never publish; workflow dispatch defaults to a dry
run and only main is eligible to publish. The workflow never auto-merges a release PR.

When adding a package, add its release-please config and manifest entries, and Python
runtime-version `extra-files`/markers if applicable. The initial release also includes audited
one-time patches for fixes merged after their current registry version was uploaded; see
`RELEASING.md` and `.github/release/bootstrap-audit.json`. Validate locally:

```bash
python3 -m unittest discover -s .github/release -p 'test_*.py'
python3 .github/release/release_plan.py --check
python3 .github/release/release_plan.py --offline
```

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

