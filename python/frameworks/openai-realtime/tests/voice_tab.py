"""Voice-tab assertions for exported spans.

LOCAL STAND-IN for the shared helper the voice family architecture assigns to
TH-8339 (``assert_voice_tab`` / ``assert_not_voice_tab``). That helper does not
exist in traceAI yet; this file implements the same contract so the package can
be tested now. Replace it with the shared helper when TH-8339 lands.

The Voice tab lists a call when the latest root has no parent and the collector
stored ``observation_type = 'conversation'``. The collector derives that from
``fi.span.kind`` first (fi-collector ``clickhouse25exporter/converter.go``,
``resolveObservationType``), so a lower-cased ``fi.span.kind`` of ``conversation``
on the parentless root stands in for the stored row. Nothing here calls a network.
"""

from __future__ import annotations

import json
from typing import Iterable, Sequence

CREDENTIAL_TOKENS = ("sk-", "Bearer ", "api_key=", "openai-insecure-api-key.")


def _root(spans: Sequence) -> object:
    roots = [s for s in spans if s.parent is None]
    assert len(roots) == 1, f"expected exactly one parentless root, got {[s.name for s in roots]}"
    return roots[0]


def all_values(spans: Iterable) -> str:
    """Every attribute value and span-event attribute value, as one string."""
    out = []
    for span in spans:
        out.extend(str(v) for v in (span.attributes or {}).values())
        for event in span.events:
            out.extend(str(v) for v in (event.attributes or {}).values())
        if span.status.description:
            out.append(span.status.description)
    return json.dumps(out, ensure_ascii=False)


def assert_voice_tab(spans: Sequence, *, omissions: Iterable[str] = (), forbidden_substrings: Iterable[str] = ()) -> None:
    root = _root(spans)
    attrs = dict(root.attributes or {})
    assert str(attrs.get("fi.span.kind", "")).lower() == "conversation", attrs.get("fi.span.kind")
    assert attrs.get("gen_ai.system"), "gen_ai.system missing: the server would parse the call as Vapi"
    assert attrs.get("session.id"), "session.id missing on the conversation root"
    for key in omissions:
        assert key not in attrs, f"{key} must be absent (omitted), found {attrs[key]!r}"
    blob = all_values(spans)
    for token in CREDENTIAL_TOKENS:
        assert token not in blob, f"credential-shaped value {token!r} on a span"
    for needle in forbidden_substrings:
        assert needle not in blob, f"{needle!r} found on a span"


def assert_not_voice_tab(spans: Sequence) -> None:
    root = _root(spans)
    kind = str(dict(root.attributes or {}).get("fi.span.kind", "")).lower()
    assert kind != "conversation", "a text session must not be listed as a voice call"
