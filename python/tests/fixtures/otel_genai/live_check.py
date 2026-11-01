"""Check stored rows from a running Future AGI against columns.golden.json.

Post the three fixtures to a real collector (rewrite resource ``project_name``
to a scratch project), export the stored rows as JSON (list of objects with
``name, model, provider, gen_ai_system, prompt_tokens, completion_tokens,
total_tokens, observation_type``, e.g. ``fi_verify.py --json``), then run::

    python live_check.py spans.json

Exit 0 when every golden span is stored with the golden columns. Not run by
CI; it needs a live stack.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

COLUMNS = ("model", "provider", "gen_ai_system", "prompt_tokens", "completion_tokens", "total_tokens")


def check(rows: list[dict], golden: dict) -> list[str]:
    by_name = {r["name"]: r for r in rows}
    problems: list[str] = []
    for fixture, spans in golden["columns"].items():
        for name, want in spans.items():
            row = by_name.get(name)
            if row is None:
                problems.append(f"{fixture}: span {name!r} not stored")
                continue
            for col in COLUMNS:
                if row.get(col) in (None, "") and want[col] in (0, ""):
                    continue
                if row.get(col) != want[col]:
                    problems.append(f"{fixture}/{name}: {col} stored {row.get(col)!r}, golden {want[col]!r}")
    return problems


def main(argv: list[str]) -> int:
    rows = json.loads(Path(argv[1]).read_text())
    golden = json.loads((Path(__file__).parent / "columns.golden.json").read_text())
    problems = check(rows, golden)
    for p in problems:
        print("FAIL", p)
    print("PASS" if not problems else f"{len(problems)} mismatch(es)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
