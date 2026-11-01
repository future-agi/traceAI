"""Minimal Voice-tab assertion, local to this package's tests.

TEMPORARY STAND-IN, flagged: the TH-8103 voice family architecture (D-V06) puts one
shared ``assert_voice_tab`` helper under TH-8339. On 2026-10-15 neither traceAI ``dev``
nor the open harness PR (#203, ``python/tests/harness``: ``post_otlp``/``compare``)
has it. This file implements the documented interface so the gates run now; replace
the import with the shared helper when it lands and delete this file.

The listing predicate it mirrors is the collector's ``resolveObservationType``
(fi-collector ``exporter/clickhouse25exporter/converter.go``): the first non-empty of
``fi.span.kind``, ``gen_ai.span.kind``, ``llm.request.type``,
``openinference.span.kind``, lowercased, must be ``conversation`` on the parentless
root. This helper does not call ClickHouse or the network.
"""

from typing import Any, Dict, Iterable, List, Mapping, Sequence

from traceai_elevenlabs_agents.map import attributes_to_dict

_KIND_KEYS = ("fi.span.kind", "gen_ai.span.kind", "llm.request.type", "openinference.span.kind")
_CREDENTIAL_TOKENS = ("sk-", "Bearer ", "api_key=", "openai-insecure-api-key.")


def _flat(spans: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for span in spans:
        attrs = span.get("attributes")
        out.append(
            {
                "name": span.get("name"),
                "span_id": span.get("spanId"),
                "parent": span.get("parentSpanId") or "",
                "attrs": attributes_to_dict(attrs) if isinstance(attrs, list) else dict(attrs or {}),
            }
        )
    return out


def _kind(attrs: Mapping[str, Any]) -> str:
    for key in _KIND_KEYS:
        value = attrs.get(key)
        if value:
            return str(value).strip().lower()
    return ""


def _root(flat: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    roots = [s for s in flat if not s["parent"]]
    assert len(roots) == 1, "expected exactly one parentless root, got %d" % len(roots)
    return roots[0]


def assert_voice_tab(spans, *, omissions: Sequence[str] = (), forbidden_substrings: Sequence[str] = ()) -> None:
    flat = _flat(spans)
    root = _root(flat)
    attrs = root["attrs"]
    # converter.go resolveObservationType -> observation_type; voice_call_list.py filters 'conversation'.
    assert _kind(attrs) == "conversation", "root kind %r would not store observation_type=conversation" % _kind(attrs)
    assert attrs.get("gen_ai.system"), "gen_ai.system missing on the root (the server would parse the call as Vapi)"
    assert attrs.get("session.id"), "session.id missing on the root"
    for key in omissions:
        assert key not in attrs, "%s must be omitted (absent key), found %r" % (key, attrs.get(key))
    for span in flat:
        blob = repr(span["attrs"])
        for token in _CREDENTIAL_TOKENS:
            assert token not in blob, "credential-shaped token %r on span %s" % (token, span["name"])
        for needle in forbidden_substrings:
            assert needle not in blob, "forbidden value on span %s" % span["name"]


def assert_not_voice_tab(spans) -> None:
    root = _root(_flat(spans))
    assert _kind(root["attrs"]) != "conversation"
