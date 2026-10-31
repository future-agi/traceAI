"""A7 deterministic attribute projection and loss accounting for detached SDK snapshots.

spec/auto-instrumentation/README.md "A7 deterministic attribute projection": protected
semantics are reserved before any content; bounded content fits the remainder (output first,
then the newest contiguous input messages); groups are atomic (a MIME companion never leaves
its payload); ``traceai.capture.dropped_content_count`` reports omitted content keys.

Media context survives destructive admission. The SDK container evicts oldest-first under the
attribute count limit, and the projection omits keys, so a MIME/type sibling could disappear
while its short payload stayed. ``AdmissionAttributes`` records the media context of MIME/type
keys the SDK evicts, and the projection adds that of its whole candidate set (bounded); the one
S1 sanitizer seeds its sibling hints with that record, so the payload is still recognized
whatever admission or projection dropped.

A record is trusted only when it covers every key the container lost. Some admission happens
before any traceAI hook can record it: constructor-supplied span and link attributes (admitted
before ``on_start``), event and ``add_link`` containers (no hook), and spans that reach an owned
exporter through a host processor. ``unaccounted_loss`` detects such a container (it dropped keys
no record covers) and the sanitizer then fails closed for it: media context unknown, so every
catalog payload field is omitted while identity, timing and metadata are kept. Sanitized
snapshots are stamped as fully accounted, so a later boundary does not omit clean content again.
"""

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from fi_instrumentation._privacy_catalog import PRIVACY as _PRIVACY
from fi_instrumentation._privacy_catalog import leaf as _leaf

_MIME_NAMES = frozenset(_PRIVACY.mime["names"])

DROPPED_CONTENT_COUNT = "traceai.capture.dropped_content_count"
_USAGE_PREFIXES = ("gen_ai.usage.", "llm.token_count.")
# Protected families, in the order kept when even they exceed a host limit (rule 4).
PROTECTED_FAMILIES: Tuple[Tuple[str, ...], ...] = (
    (DROPPED_CONTENT_COUNT, "traceai.wire.body_unavailable", "traceai.wire.incomplete",
     "traceai.wire.timing_truncated", "traceai.wire.truncated", "traceai.wire.undecoded_encoding"),
    ("gen_ai.response.id", "gen_ai.span.kind", "project_name", "project_type",
     "project_version_id", "project_version_name"),
    ("error.type", "gen_ai.response.finish_reasons", "http.response.status_code", "traceai.moderation.flagged"),
    ("gen_ai.operation.name", "gen_ai.provider.name", "gen_ai.request.model", "gen_ai.response.model",
     "gen_ai.system"),
    (),  # usage: every known gen_ai.usage.* counter
    ("http.request.method", "server.address", "server.port", "traceai.capture.layer", "traceai.wire.endpoint"),
)
_FAMILY = {key: index for index, family in enumerate(PROTECTED_FAMILIES) for key in family}
_USAGE_FAMILY = 4
# The finite explicit set FiSpan keeps outside the SDK container (plus bounded usage keys).
ESSENTIAL_ATTRIBUTES = tuple(key for family in PROTECTED_FAMILIES for key in family)
ESSENTIAL_NAMES = frozenset(ESSENTIAL_ATTRIBUTES)
MAX_RESERVED_USAGE = 32
# Media-context keys an admission record retains per span; past it the sanitizer fails closed
# (identity/timing only), as it does when its own sibling-hint budget is exceeded.
MAX_ADMISSION_HINTS = 4096

_MESSAGE = re.compile(r"^(gen_ai\.(input|output)\.messages|llm\.(input|output)_messages)\.(\d+)\.")
_INDEXED = re.compile(r"^(gen_ai\.tool\.definitions|gen_ai\.embeddings|embedding\.embeddings|"
                      r"retrieval\.documents|gen_ai\.retrieval\.documents)\.(\d+)\.")
_INPUT_CONTENT = ("input.", "gen_ai.input.", "gen_ai.tool.", "gen_ai.system_instructions", "gen_ai.prompt",
                  "llm.prompt", "embedding.", "gen_ai.embeddings.", "llm.input_messages")
_OUTPUT_CONTENT = ("output.", "gen_ai.output.", "llm.output_messages", "retrieval.", "gen_ai.retrieval.")


def protected_family(key: str) -> Optional[int]:
    family = _FAMILY.get(key)
    if family is None and key.startswith(_USAGE_PREFIXES):
        return _USAGE_FAMILY
    return family


def is_protected(key: str) -> bool:
    return protected_family(key) is not None


def _content_group(key: str, operation: str) -> Optional[Tuple[Any, ...]]:
    """(tier, sort key, group id) for a content key; None for noncontent metadata.

    Tiers: 0 output messages (ascending), 1 output value/transcript representations,
    2 input messages (newest first), 3 other input content, 4 aggregate input.
    """
    found = _MESSAGE.match(key)
    if found:
        index = int(found.group(4))
        if (found.group(2) or found.group(3)) == "output":
            return (0, index, found.group(0))
        return (2, -index, found.group(0))
    if key in ("output.value", "output.mime_type"):
        return (1, 0, "output.value")
    if key in ("input.value", "input.mime_type"):
        return (4, 0, "input.value")
    if key == "gen_ai.audio.transcript":
        return (3, 0, key) if operation == "text_to_speech" else (1, 2, key)
    if key == "raw.output":
        return (1, 1, key)
    found = _INDEXED.match(key)
    if found:
        output = found.group(1).endswith("retrieval.documents") or found.group(1) == "retrieval.documents"
        return (1 if output else 3, int(found.group(2)) + 10, found.group(0))
    if key in ("raw.input", "gen_ai.request.parameters", "llm.invocation_parameters") or key.startswith(_INPUT_CONTENT):
        return (3, 1 << 30, key)
    if key.startswith(_OUTPUT_CONTENT):
        return (1, 1 << 30, key)
    return None


def _companion_groups(groups: Dict[Tuple[Any, ...], List[str]], values: Mapping[str, Any]) -> Dict[Tuple[Any, ...], List[str]]:
    """Merge generic content keys with their media MIME/type companion into one atomic group.

    Message and indexed groups are already atomic. A generic (per-key) content key whose leaf is
    a MIME name or ``type`` with a media/audio value anchors its parent path (the parent the
    sanitizer binds the hint to); every generic content key under that parent joins the group,
    which takes the highest-priority tier among its members. A root-level companion is not
    grouped (it would bind everything); the sanitizer's admission record still covers it.
    """
    PRIVACY, leaf = _PRIVACY, _leaf

    def is_generic(group: Tuple[Any, ...], keys: List[str]) -> bool:
        # The per-key fallback group: its id is the key itself (message/indexed ids are prefixes).
        return group[1] == 1 << 30 and len(keys) == 1 and group[2] == keys[0]

    generic = [(group, keys[0]) for group, keys in groups.items() if is_generic(group, keys)]
    anchors: set = set()
    for _, key in generic:
        if leaf(key) in PRIVACY.mime["names"] or leaf(key) == "type":
            try:
                media, audio = PRIVACY.hint(key, values[key])
            except ValueError:
                media = audio = True  # oversized hint: keep it with its siblings
            parent = PRIVACY.hint_parent(key)
            if (media or audio) and parent:
                anchors.add(parent)
    if not anchors:
        return groups
    outermost_first = sorted(anchors, key=lambda anchor: (len(anchor), anchor))
    merged: Dict[Tuple[Any, ...], List[str]] = {}
    members: Dict[str, List[Tuple[Tuple[Any, ...], str]]] = {}
    for group, keys in groups.items():
        key = keys[0]
        parent = None
        if is_generic(group, keys):
            # The outermost anchor wins, so nested companions share one group.
            parent = next((anchor for anchor in outermost_first if key.startswith(anchor + ".")), None)
        if parent is None:
            merged[group] = keys
        else:
            members.setdefault(parent, []).append((group, key))
    for parent, entries in members.items():
        tier = min(group[0] for group, _ in entries)
        merged[(tier, 1 << 30, parent + ".")] = sorted(key for _, key in entries)
    return merged


def project_attributes(candidates: Mapping[str, Any], limit: Optional[int], operation: str = "",
                       reserved: int = 0) -> Tuple[Dict[str, Any], int]:
    """Select the attributes to admit under ``limit``: (selected, number of candidate keys omitted).

    Deterministic for the same candidates and limit, whatever the insertion order. ``reserved``
    slots are consumed by attributes the caller adds afterwards (they are never evicted).
    """
    values = dict(candidates)
    if limit is None or len(values) + reserved <= limit:
        return values, 0
    capacity = max(0, limit - reserved)
    previous = values.pop(DROPPED_CONTENT_COUNT, 0)
    previous = previous if type(previous) is int and previous > 0 else 0
    protected, content, metadata = [], {}, []
    for key in values:
        family = protected_family(key)
        if family is not None:
            protected.append((family, key))
            continue
        group = _content_group(key, operation)
        if group is None:
            metadata.append(key)
        else:
            content.setdefault(group, []).append(key)
    protected.sort()
    groups = sorted(_companion_groups(content, values).items())  # (tier, order, id) is total and deterministic
    content_keys = sum(len(keys) for keys in content.values())

    def select(room: int) -> Tuple[List[str], int]:
        """Content keys admitted into ``room`` slots and the content keys omitted."""
        chosen: List[str] = []
        output_omitted = input_omitted = False
        for (tier, _, _), keys in groups:
            if tier >= 2 and output_omitted:
                break  # never admit input while a higher-priority output group is omitted
            if tier == 2 and input_omitted:
                continue  # retained input messages are a contiguous newest suffix
            if tier == 4 and input_omitted:
                continue  # the aggregate input would restore the evicted conversation
            if len(keys) <= room:
                chosen.extend(sorted(keys))
                room -= len(keys)
            elif tier <= 1:
                output_omitted = True
            elif tier == 2:
                input_omitted = True
        return chosen, content_keys - len(chosen)

    room = capacity - len(protected)
    chosen, omitted = select(max(0, room))
    marker = previous > 0 or omitted > 0
    if marker:
        # One slot for the count, computed before anything is written.
        room -= 1
        chosen, omitted = select(max(0, room))
    if room < 0:
        # Even protected semantics overflow: no content, family-ordered protected prefix.
        ordered = ([DROPPED_CONTENT_COUNT] if marker else []) + [key for _, key in protected]
        kept = ordered[:capacity]
        selected = {key: values[key] for key in kept if key != DROPPED_CONTENT_COUNT}
        if DROPPED_CONTENT_COUNT in kept:
            selected[DROPPED_CONTENT_COUNT] = previous + content_keys
        omitted_total = len(values) - (len(kept) - (1 if DROPPED_CONTENT_COUNT in kept else 0))
        return selected, omitted_total
    selected = {key: values[key] for _, key in protected}
    for key in chosen:
        selected[key] = values[key]
    room -= len(chosen)
    for key in sorted(metadata):
        if room <= 0:
            break
        selected[key] = values[key]
        room -= 1
    if marker:
        selected[DROPPED_CONTENT_COUNT] = previous + omitted
    omitted_total = len(values) - (len(selected) - (1 if marker else 0))
    return selected, omitted_total


def reserved_attribute(key: str, reservoir: Mapping[str, Any]) -> bool:
    """Whether FiSpan keeps ``key`` outside the SDK container (bounded protected set)."""
    if key in ESSENTIAL_NAMES:
        return True
    return key.startswith(_USAGE_PREFIXES) and (key in reservoir or len(reservoir) < len(ESSENTIAL_NAMES) + MAX_RESERVED_USAGE)


def span_attribute_limit(tracer: Any) -> Optional[int]:
    """The effective attribute count limit of an SDK tracer (default 128); never raised."""
    limits = getattr(tracer, "_span_limits", None)
    if limits is None:
        inner = getattr(tracer, "__wrapped__", None)
        limits = getattr(inner, "_span_limits", None)
    if limits is None:
        return 128
    value = getattr(limits, "max_span_attributes", 128)
    return value if value is None or type(value) is int else 128


def project_span_attributes(span, retained, operation: str = ""):
    """Project a live span's attributes (SDK container plus FiSpan reservoir) before it ends.

    The SDK remains the bounded content store; only the finite protected set is retained
    separately. The OTel all-attribute drop count keeps SDK drops plus projection omissions;
    the content count is separate (``traceai.capture.dropped_content_count``).
    """
    from opentelemetry.attributes import BoundedAttributes

    original = getattr(span, "_attributes", None)
    if not isinstance(original, BoundedAttributes):
        for key, value in retained.items():
            span.set_attribute(key, value)
        return
    with span._lock:
        if span._end_time is not None:
            return
        values = {**dict(original), **retained}
        limit = original.maxlen
        dropped = original.dropped
        if not operation:
            operation = str(values.get("gen_ai.operation.name") or "")
        if dropped > 0 or (limit is not None and len(values) > limit):
            projected, omitted = project_attributes(values, limit, operation)
            dropped += omitted
        else:
            projected = values
        bounded = AdmissionAttributes(maxlen=limit, attributes=projected, immutable=False,
                                      max_value_len=original.max_value_len)
        # Media context of everything the SDK admitted and of the full pre-projection
        # candidate set survives into the projected container for the S1 sanitizer.
        bounded.adopt_hints(original)
        for key, value in values.items():
            bounded.note(key, value)
        bounded.dropped += dropped
        span._attributes = bounded


def snapshot_attributes(cleaned, original):
    """Preserve SDK accounting without revalidating sanitized mixed-type arrays."""
    from opentelemetry.attributes import BoundedAttributes

    if not isinstance(original, BoundedAttributes):
        return cleaned
    copied = BoundedAttributes(maxlen=None)
    copied._dict = cleaned
    copied.dropped = original.dropped
    # Already sanitized with the full record (or failed closed): its drops are accounted.
    copied._traceai_lossy = False
    return copied


def snapshot_sequence(cleaned, original):
    from opentelemetry.sdk.util import BoundedList

    if not isinstance(original, BoundedList):
        return cleaned
    copied = BoundedList.from_seq(len(cleaned), cleaned)
    copied.dropped = original.dropped + max(0, len(original) - len(cleaned))
    return copied


def _hint_name(key: Any) -> bool:
    """Exact cheap pre-filter: only MIME-name (``*type``) and ``type`` leaves carry media context."""
    # C-speed superset of the catalog fold (ASCII lowercase, separators removed); the exact
    # catalog leaf test follows, so a false positive here only costs that test.
    if type(key) is not str or "type" not in key.lower().replace("-", "").replace("_", "").replace(".", ""):
        return False
    folded = _leaf(key)
    return folded in _MIME_NAMES or folded == "type"


try:
    from opentelemetry.attributes import BoundedAttributes as _BoundedAttributes
except ImportError:  # pragma: no cover - the OpenTelemetry API is a hard dependency
    _BoundedAttributes = dict  # type: ignore[assignment,misc]


class AdmissionAttributes(_BoundedAttributes):  # type: ignore[misc,valid-type]
    """The SDK's bounded span attribute container, unchanged, plus a bounded media-context record.

    When the SDK evicts its oldest key to admit a new one, the victim's media/audio hint (if it is
    a MIME/type key with a media value) is recorded; re-admitting a key forgets its stale record,
    so its present value decides again. ``project_span_attributes`` adds the full pre-projection
    candidate set. Values, eviction order and drop counts are exactly the SDK's: nothing here reads
    or changes what the application or its exporters see, and the common write path costs one
    length check. Recording never raises; an unreadable or excessive record makes the sanitizer
    fail closed.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._traceai_hints: Dict[str, Tuple[bool, bool]] = {}
        self._traceai_hint_overflow = False
        # Keys lost without a record (only ever set from an adopted container's history).
        self._traceai_lossy = False
        super().__init__(*args, **kwargs)

    @classmethod
    def adopt(cls, original: Any) -> Any:
        """The same container state (shared dict, lock and counters) behind the recording type.

        Keys ``original`` already dropped (constructor admission, before any hook) were never
        recorded, so the adopted container is marked lossy and the sanitizer fails closed.
        """
        adopted = object.__new__(cls)
        adopted.__dict__.update(original.__dict__)
        adopted._traceai_hints = {}
        adopted._traceai_hint_overflow = False
        adopted._traceai_lossy = False
        adopted.adopt_hints(original)
        return adopted

    def adopt_hints(self, original: Any) -> None:
        hints = getattr(original, "_traceai_hints", None)
        if hints:
            for key, hint in list(hints.items()):
                self._record(key, hint)
        if getattr(original, "_traceai_hint_overflow", False):
            self._traceai_hint_overflow = True
        if unaccounted_loss(original):
            self._traceai_lossy = True

    def _record(self, key: str, hint: Tuple[bool, bool]) -> None:
        if hint[0] or hint[1]:
            if key not in self._traceai_hints and len(self._traceai_hints) >= MAX_ADMISSION_HINTS:
                self._traceai_hint_overflow = True
                return
            self._traceai_hints[key] = hint
        else:
            self._traceai_hints.pop(key, None)

    def note(self, key: Any, value: Any) -> None:
        """Record ``key``'s media context from ``value`` (the latest value wins)."""
        try:
            if _hint_name(key):
                self._record(key, _PRIVACY.hint(key, value))
        except Exception:
            self._traceai_hint_overflow = True

    def _admit(self, key: Any) -> None:
        # Called before the SDK stores ``key``: the same eviction test the SDK applies next.
        try:
            hints = self._traceai_hints
            if hints and key in hints:
                del hints[key]
            store = self._dict
            maxlen = self.maxlen
            if maxlen is not None and store and len(store) >= maxlen and key not in store:
                victim = next(iter(store))
                self.note(victim, store[victim])
        except Exception:
            self._traceai_hint_overflow = True

    if hasattr(_BoundedAttributes, "_setitem_locked"):
        # Every admission path (set_attribute, set_attributes, the constructor) ends here.
        def _setitem_locked(self, key: Any, value: Any) -> None:
            # Inline fast path of _admit: no record to forget and no eviction ahead.
            if self._traceai_hints or (self.maxlen is not None and len(self._dict) >= self.maxlen):
                self._admit(key)
            _BoundedAttributes._setitem_locked(self, key, value)  # type: ignore[attr-defined]
    else:  # OpenTelemetry 1.29-1.39 admit and evict in __setitem__ (the constructor uses it)
        def __setitem__(self, key: Any, value: Any) -> None:
            if not getattr(self, "_immutable", False):
                self._admit(key)
            _BoundedAttributes.__setitem__(self, key, value)


def adopt_admission_hints(span: Any) -> None:
    """Install the recording container on a live SDK span (start time); never raises."""
    try:
        original = getattr(span, "_attributes", None)
        if type(original) is _BoundedAttributes and not getattr(span, "_end_time", None):
            span._attributes = AdmissionAttributes.adopt(original)
    except Exception:
        # Never fail the application's span. The container stays a plain SDK container, so any
        # key it later drops is an unaccounted loss and the sanitizer fails closed for it.
        import logging

        logging.getLogger(__name__).debug("traceAI: media admission record unavailable", exc_info=True)


def unaccounted_loss(container: Any) -> bool:
    """Whether an SDK attribute container dropped keys whose media context no record holds.

    A recording container answers from its own history; a sanitized snapshot is accounted; any
    other SDK container (event/link attributes, spans no traceAI hook adopted) lost unrecorded
    keys exactly when its drop count is nonzero. Unknown mappings report no loss (they have no
    drop counter); an unreadable SDK container counts as lossy.
    """
    if not isinstance(container, _BoundedAttributes):
        return False
    state = getattr(container, "__dict__", None)
    if state is None:
        return False
    try:
        flag = state.get("_traceai_lossy")
        if flag is not None:
            return flag is not False
        dropped = state.get("dropped", 0)
        return type(dropped) is not int or dropped > 0
    except Exception:
        return True


def admission_hints(container: Any) -> Optional[Dict[str, Tuple[bool, bool]]]:
    """Sibling hints (parent path -> (media, audio)) a container recorded at admission.

    A container with an unaccounted loss gets root media/audio context (``""``), so every
    catalog payload field in it is omitted: its evicted MIME/type siblings are unknown.
    Raises ValueError when the record overflowed, so the sanitizer fails closed.
    """
    if getattr(container, "_traceai_hint_overflow", False):
        raise ValueError("privacy admission hint budget")
    hints = getattr(container, "_traceai_hints", None)
    lossy = unaccounted_loss(container)
    if not hints and not lossy:
        return None
    parents: Dict[str, Tuple[bool, bool]] = {}
    for key, (media, audio) in list((hints or {}).items()):
        parent = _PRIVACY.hint_parent(key)
        previous = parents.get(parent, (False, False))
        parents[parent] = (previous[0] or media, previous[1] or audio)
    if lossy:
        parents[""] = (True, True)
    return parents
