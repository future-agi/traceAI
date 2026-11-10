"""Local stand-in for the TH-8339 Voice-tab assertion helper.

The shared helper (``assert_voice_tab(spans, *, omissions=(), forbidden_substrings=())``,
voice family architecture, D-V06) was not merged when this package was written. This file
implements the same contract for these tests only and should be replaced by an import of
the shared helper when it lands. It does not call ClickHouse or the collector: the
collector's ``resolveObservationType`` lowercases ``fi.span.kind`` and stores
``conversation`` only for that token, and the Voice tab lists parentless roots with
``observation_type = 'conversation'``.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Sequence

CREDENTIAL_SHAPES = ("sk-", "Bearer ", "api_key=", "openai-insecure-api-key.")


def _root(spans: Sequence[Any]) -> Any:
    roots = [s for s in spans if s.parent is None]
    assert len(roots) == 1, f"expected exactly one parentless root, got {[s.name for s in roots]}"
    return roots[0]


def _values(spans: Iterable[Any]) -> str:
    out = []
    for s in spans:
        out.append(json.dumps(dict(s.attributes or {}), default=str))
        for ev in s.events:
            out.append(json.dumps(dict(ev.attributes or {}), default=str))
        out.append(str(s.status.description))
    return "\n".join(out)


def assert_voice_tab(spans: Sequence[Any], *, omissions: Iterable[str] = (),
                     forbidden_substrings: Iterable[str] = ()) -> Any:
    root = _root(spans)
    attrs = dict(root.attributes or {})
    assert str(attrs.get("fi.span.kind", "")).lower() == "conversation", attrs.get("fi.span.kind")
    assert attrs.get("gen_ai.system"), "gen_ai.system missing: the server would parse the call as Vapi"
    assert attrs.get("session.id"), "session.id missing"
    for key in omissions:
        assert key not in attrs, f"{key} must be absent, found {attrs.get(key)!r}"
    text = _values(spans)
    for shape in CREDENTIAL_SHAPES:
        assert shape not in text, f"credential-shaped token {shape!r} on a span"
    for needle in forbidden_substrings:
        assert needle not in text, f"forbidden value {needle!r} on a span"
    return root


def assert_not_voice_tab(spans: Sequence[Any]) -> Any:
    root = _root(spans)
    kind = str((root.attributes or {}).get("fi.span.kind", "")).lower()
    assert kind != "conversation", "a text session must not be listed as a call"
    return root
