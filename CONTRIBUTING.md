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

