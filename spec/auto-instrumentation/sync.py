"""Check byte identity of shared catalogs and the generated privacy fixture bundle.

    python3 -I spec/auto-instrumentation/sync.py
    python3 -I spec/auto-instrumentation/sync.py --write
    python3 -I spec/auto-instrumentation/sync.py --require-privacy-copies

--write only refreshes copies that already exist; SDK owners adopt by adding
privacy.json and privacy-fixtures.json next to their wire catalog. Missing privacy
copies are reported, not errors, until REQUIRE_PRIVACY_COPIES is flipped or the
strict CLI flag is supplied. Existing-but-stale privacy copies ALWAYS fail.
"""

import argparse
import os
import sys
from pathlib import Path

SPEC_DIR = Path(__file__).resolve().parent
ROOT = SPEC_DIR.parents[1]
CATALOG = SPEC_DIR / "wire-protocols.json"
REQUIRE_PRIVACY_COPIES = False  # Main flips this after all six SDKs adopt S6.
FILES = ("wire-protocols.json", "privacy.json", "privacy-fixtures.json")
SDK_ROOTS = {
    "python": "python/fi_instrumentation",
    "typescript": "typescript/packages/traceai_auto",
    "go": "go/auto",
    "java": "java/traceai-javaagent",
    "csharp": "csharp/src/TraceAI.AutoInstrumentation",
    "ruby": "ruby/lib/traceai/auto",
}
# These canonical shipping destinations cannot be satisfied by test/data copies
# elsewhere under the SDK root. Also require every discovered wire catalog's
# parent, which covers launcher-embedded agents and future duplicate runtimes.
SDK_WIRE_DIRS = {
    "python": "python/fi_instrumentation/auto/wire",
    "typescript": "typescript/packages/traceai_auto/src/wire",
    "go": "go/auto/internal/wire",
    "java": "java/traceai-javaagent/src/main/resources/ai/traceai/auto",
    "csharp": "csharp/src/TraceAI.AutoInstrumentation/Wire",
    "ruby": "ruby/lib/traceai/auto",
}
SKIP = {"node_modules", "dist", "build", "target", "bin", "obj", ".git", ".venv", "venv",
        "__pycache__", ".scratch", ".cache", ".dotnet", ".pytest_cache"}


def all_copies():
    """Prune build/cache trees before traversal; include vendored runtime copies.

    Launcher embedded agents are shipping copies, not build output. In contrast,
    launcher .scratch trees contain old checkouts and must never be synced.
    """
    found = {name: [] for name in FILES}
    for directory, dirs, files in os.walk(ROOT, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in SKIP and not name.startswith((".venv-", "venv-")))
        for name in FILES:
            path = Path(directory) / name
            if name in files and path.parent != SPEC_DIR:
                found[name].append(path)
    return {name: sorted(paths) for name, paths in found.items()}


def copies(name="wire-protocols.json"):
    """Retain the old helper's default for callers that only check wire catalogs."""
    return all_copies()[name]


def check(write=False, require_privacy=False):
    found = all_copies()
    destinations = {ROOT / directory: sdk for sdk, directory in SDK_WIRE_DIRS.items()}
    for path in found["wire-protocols.json"]:
        destinations.setdefault(path.parent, "shipping/embedded")
    failed, matched, updated, missing = 0, 0, 0, 0
    for name in FILES:
        source = SPEC_DIR / name
        if not source.is_file():
            print("missing spec source: " + name, file=sys.stderr)
            failed += 1
            continue
        expected = source.read_bytes()
        for path in found[name]:
            stale = path.read_bytes() != expected
            if stale and write:
                path.write_bytes(expected)
                updated += 1
                label = "updated"
            elif stale:
                failed += 1
                label = "stale  "
            else:
                matched += 1
                label = "ok     "
            print(label + " " + str(path.relative_to(ROOT)))
        if name == "wire-protocols.json":
            if not found[name]:
                print("no SDK wire catalog copies found", file=sys.stderr)
                failed += 1
            continue
        present = set(found[name])
        for directory, sdk in sorted(destinations.items()):
            if directory / name not in present:
                missing += 1
                relative = (directory / name).relative_to(ROOT)
                print(f"missing {sdk}: {relative} (S6 adoption {'required' if require_privacy else 'pending; non-blocking'})")
                if require_privacy:
                    failed += 1
    print(f"sync: {matched} byte-identical; {updated} updated; {missing} pending/missing; {failed} errors")
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write", action="store_true", help="overwrite stale existing copies (never adopt missing copies)")
    parser.add_argument("--require-privacy-copies", action="store_true", default=REQUIRE_PRIVACY_COPIES,
                        help="fail unless every SDK has both privacy files")
    args = parser.parse_args()
    return check(args.write, args.require_privacy_copies)


if __name__ == "__main__":
    sys.exit(main())
