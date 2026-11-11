Attachment contract v1
======================

Use these schemas and fixtures to implement interoperable attachment producers,
ingestion, storage and viewers without placing media bytes in OTLP.

This directory freezes ``fi.attachments.v1``. It contains contracts and test-only
validators, not an uploader, recorder, media service or integration-support badge.
The first delivery is Python voice. Image/video/document enum values and privacy
fixtures reserve the shared vocabulary; they do not enable those integrations.

Contract files
--------------

* ``contract.schema.json``: JSON Schema 2020-12 definitions. Select a ``$defs``
  member, not the permissive document root, when validating a payload.
* ``openapi.json``: OpenAPI 3.1 control, import, upload, occurrence, manifest and
  authenticated playback routes. Paths have no trailing slash. The trusted
  control origin is configured separately from OTLP; never derive it from tool
  output or append these paths to an arbitrary collector origin.
* ``constants.json``: semantic names, byte ceilings and bounded default budgets.
* ``privacy-table.json`` and ``security-table.json``: ordered, executable
  deny predicates using ordinary JSON Schema. Unknown or missing facts deny.
* ``*-fixtures.json``: shared expected results, consumable by Python, TypeScript,
  Go ingestion and the backend without importing a runtime SDK.
* ``otel-schema-pin.json``: immutable OTel schema revision and digests. The
  message URI is a custom ``GenericPart`` extension at that revision, not a
  standardized upload protocol or provider-readable URL. Existing semantic
  convention defaults are unchanged.

Schemas are closed: unlisted members are rejected, not silently retained.
Implementations also enforce the cross-field/serialization rules below; JSON
Schema ``contentSchema`` is an annotation, not automatic inner-JSON validation.
A schema-valid capability claim is not a verified capability: signature, issuer,
audience, expiry, current authority and exact bindings must all be checked by
its issuer/consumer. These files define claims, not a new token signing format.

Identity and span projection
----------------------------

A trace is the same nonzero 16 bytes in both spellings: lowercase 32-hex in SDKs,
lowercase dashed UUID in API responses/storage. API requests accept either
spelling. Normalize by inserting dashes, never by generating a UUID or changing
version/variant bits. Span IDs are nonzero lowercase 16-hex. Public attachment
and occurrence IDs are canonical nonzero lowercase UUIDs, scoped rather than
globally authoritative. Uppercase, malformed and all-zero IDs are rejected.

A span carries ``fi.attachment.schema_version=1`` and ``fi.attachment.refs`` as a
JSON string containing ``Reference[]``. Parse the string strictly: reject duplicate
JSON members, duplicate occurrence IDs, unknown fields, invalid identities,
MIME/modality mismatches, more than 64 entries or more than 16,384 UTF-8 bytes.
Project only an admitted bounded prefix; the scoped listing is authoritative
for the rest. A denied capture emits no reference, filename, hash, placeholder
or source-derived count. Unknown reference versions are unsupported; they never
fall through to a legacy URL renderer.

The optional paired attributes ``fi.attachment.capture_session_id`` and
``fi.attachment.correlation_nonce`` carry nonsecret correlation only. Ingestion
must verify the authenticated writer and stamp the exact session/trace/span
binding server-side. IDs or the nonce alone are never association authority.
An adapter uses the actual emitted span identity, not a later ambient span.
Ended spans are not mutated or held open while upload completes.

``MessageURI`` uses ``fi-attachment:<occurrence UUID>``. It resolves an occurrence,
not the potentially shared attachment/blob. Reference metadata permits only
bounded direction, modality, MIME, purpose, message-part, role, channel and
opaque conversation/turn/tool correlation. Correlation IDs must be application
identifiers, never human names, URLs, filenames or free text. The allowlist cannot
establish their semantic safety; adapters must establish provenance before use.

Python capture surface
----------------------

The shared manager contract is::

    attach(span_or_context, source, *, mime_type, direction, purpose=None,
           conversation_id=None, turn_id=None, tool_call_id=None,
           message_index=None, part_index=None, role=None, channel=None)
    start_attachment_stream(context, options)  # try_write / finish / abort

Sources are discriminated ``Bytes``, ``Blob``, ``RemoteUrl``, ``LocalFile``,
``OwnedRecordingFile`` or ``ProviderFile``. Never infer a path/URL from an arbitrary
string or recursively scrape objects. The returned handle has stable client
attachment/occurrence UUIDs, a safe admission reason and nonthrowing completion.
``queued`` is not stored, validated, bound or readable. The handle's private IDs
must not be projected for a denied operation.

One manager belongs to the configured provider. Explicit capture, auto and
adapters share it and its policy. Admit before copying, decoding, hashing or
retrieval. Unknown ownership is unsupported; instrumentation never advances
caller streams/file positions or creates an eager clone/tee. Bounded owned copies
are permitted only after admission. Flush/shutdown are idempotent and deadline
bounded; they preserve host signal handlers. Hard exit can lose unfinished data.

Privacy evaluation
------------------

Attachments are OFF by default. Creating audio and video recordings has separate
OFF switches. Importing an already-produced recording does not start a recorder.
Enabling attachments grants neither arbitrary filesystem/network access nor
ambient microphone, screen, browser, paid Egress or provider-credential access.

The table consumes trusted normalized policy/provenance facts. It is not a
request body a caller can use to assert its own authorization. Evaluate server
restriction, credential/session scope, integration restrictions, all directional
and content hide flags, PII/metadata requirements, modality/source policy and
per-call request. Any denial wins. The ordered first matching denial chooses only
the safe diagnostic reason; a lower opt-in cannot override a higher denial.

Semantic ancestry is monotone. Tool-output screenshots intended as model input
inherit input-image/input-text restrictions. ``mixed`` includes both directions
even if a caller mislabels an ancestry boolean. Hiding either side denies a mixed
recording. A genuinely isolated permitted track can be captured independently;
uploading a mix and hiding a UI track is not removal. Qualified adapters derive
these facts, and backend policy remains independently authoritative.

Text-only PII scanning is not audio, pixel or PDF sanitization. With PII redaction
on, raw opaque media is denied unless a qualified client-side sanitizer satisfies
the policy. The separately informed ``allow_unredacted_media_with_text_pii``
exception needs both explicit client configuration and server permission; it
never overrides mandatory server media redaction. Quarantine-side redaction is
too late to meet a pre-egress rule. Required metadata removal likewise needs a
qualified pre-egress transform. No OCR/transcription service is enabled implicitly.

Do not export default filenames, speaker names, authors, EXIF/GPS/tags or arbitrary
source metadata in spans, lists, audits or diagnostics. Use generic display labels
and session-local roles/tracks. Originals can contain embedded metadata; setup
must disclose this and deny when required removal cannot be done before egress.
Inline base64 image limits remain zero independently of attachment opt-in.

Recheck before read, upload, retry and serve. Tightening discards queued owned
bytes and revokes capabilities/readability. Re-enabling does not resurrect them.
Unknown bootstrap policy retains no bytes. No authorization/policy/validation
retry; bounded transport retry retains the same upload and remaining reservation.

Control and transfer protocol
-----------------------------

``POST /tracer/v1/attachment-sessions`` negotiates v1, finite limits and source
permissions. It shares ingestion's exact organization/workspace/name/type project
resolution, uniqueness and reserved-project behavior. Invalid explicit workspace
never falls back; a legacy default must be the same single authorized active
workspace as ingestion. Project-version ID is not project ID. Ambiguity fails
``scope_unresolved``. Project creation requires existing ingestion authority.
The bootstrap request asks for no manually supplied project UUID.

Do not broaden generic API-key authentication. Restricted ingest/coding-agent
keys need explicit attachment-write eligibility through a narrow broker. Viewer
keys cannot upload. Write capabilities are bound to issuer-verified actor, full
scope, session, exact parent and upload/occurrence IDs, current policy/deletion
generation, bytes, operations and expiry. They grant only create/upload/complete/
abort and content-free own-job status: no list, resolve, download, arbitrary
relink, delete-existing or provider-connection access. Browser capabilities and
origins require separate qualification; v1 Wave A adds no browser upload mode.

Under ``/tracer/v1/projects/{project_id}/attachments``:

* ``POST /uploads`` authenticates the explicitly eligible original identity,
  reserves capacity idempotently and returns the secret upload-bound
  ``write_capability``. Do not log/cache/export that response. Subsequent content,
  complete, abort and status requests use that capability in the Authorization
  header. ``PUT /uploads/{upload_id}/content`` transfers bounded bytes with exact
  reserved Content-Length. Do not redirect or log request bodies. Streaming is
  the only negotiated mode in this freeze.
* ``POST /uploads/{upload_id}/complete`` requests validation, not readiness.
  Server-only multipart completion and immutable validation follow. ``/abort``
  releases this job once; ``GET /status`` exposes only safe state/reason.
* ``POST /imports`` accepts an opaque encrypted-source-envelope reference or an
  already registered provider output/connection, never a raw URL, path, arbitrary
  headers or provider credentials. Source-envelope creation/resolution belongs
  to an independently authorized trusted importer, not the generic import body.
* ``POST /occurrences`` requires ordinary scoped authority over both readable
  source and target. ``GET /occurrences`` is paginated and trace-scoped.
* ``POST /resolve`` authorizes each occurrence and expected parent separately.
  It is a read-only handler, never a read-only exemption on mutation classes.
* ``DELETE /occurrences/{occurrence_id}`` tombstones only that association.
  Whole-attachment deletion is not exposed by this capability/API freeze.
* ``PUT /recordings/{recording_id}/manifest`` is recording completion metadata,
  authorized only for the capability's registered recording and exact associated
  segment occurrences (the ``complete`` operation). It cannot relink arbitrary
  occurrences. The read handler checks expected parent and every surviving link.

Every lookup, uniqueness key, reservation, job, provider connection, cache and
idempotency decision includes full resolved organization/workspace/project scope
and relevant parent/actor/session/operation. Normalize identities before comparing
requests; reject duplicate JSON members. Same scoped key plus identical normalized
request replays; changed request conflicts. Repeated public IDs across tenants
remain distinct. Unauthorized and nonexistent foreign objects are indistinguishable;
batches never inherit the first item's authorization. Error bodies contain safe
codes and request IDs, never raw exceptions or supplied values.

The schema caps standalone uploads at 50 MiB, images at 20 MiB and recording
segments at 8 MiB AND 30 seconds. Recordings stop at 1 GiB OR 120 minutes with an
explicit truncation gap. Manifests are at most 1 MiB of serialized UTF-8 and use
bounded update windows; reaching a bound is incomplete, not silent omission.
Server/global admission also reserves decoded resources, parts, objects and links.
Unknown budgets fail readiness. Numeric schema ceilings are safety envelopes,
not billing/edition limits or promised achievable recording lengths.

Nine mandatory security boundaries
----------------------------------

1. **Private storage.** Use a distinct private bucket/provisioning path, never a
   new prefix under legacy public media helpers. Verify public-access blocking,
   encryption, IAM, ACL/CORS and lifecycle/backend primitives at startup. Original,
   quarantine and derivative anonymous GET/HEAD/Range must all fail. Unverifiable
   storage prevents readiness; legacy behavior is not globally changed.
2. **Narrow identities.** Attachment-specific eligibility and exact scoped writer
   binding replace ID-based trust. A write capability cannot acquire read or
   provider-connection authority. Public/shared trace links confer no media access.
3. **PII intersection.** Enforce the preceding deny-wins rules before egress,
   including metadata and opaque modalities, and independently on the server.
4. **Immutable promotion.** Validate a fixed quarantine generation or immutable
   snapshot, then server-only promote those exact verified bytes. Store immutable
   version, verified length/MIME/hash internally; never expose object keys/hashes
   in the public metadata schema. No client can overwrite serving identities.
   Replay after deletion cannot resurrect data; keep fences through capability
   expiry plus accepted transfer duration and sweep late writes/versions/parts.
   Direct upload remains disabled until backend-specific guarantees qualify.
5. **Complete scope.** Occurrences, not blob IDs, authorize access. Deduplicate only
   verified bytes in the same project with compatible policy/retention; no hash
   existence oracle. Relinks and ancestor listings cannot bypass child policy.
6. **Safe retrieval.** SDK and importer both require approved HTTPS provenance,
   checking all A/AAAA answers, denying private/reserved/metadata destinations,
   pinned-IP TLS hostname verification and revalidation of every bounded redirect.
   No ambient cookies, netrc, arbitrary headers or environment proxies. Do not
   forward secrets across origins. Secret URLs live only in encrypted envelopes
   for <=10 minutes, bounded by source expiry; queues/history/retries/dead letters
   contain only opaque references. Browser automatic RemoteUrl fetch is denied.
7. **Session-bound playback.** Resolution returns only the same-origin gateway.
   Media session PLUS an occurrence/representation grant binds the original
   viewer/application session/scope/parent/policy/generation/expiry and GET/HEAD.
   A copied grant under another session fails. Exchange/mutations use CSRF and
   exact Origin; cookies are host-only Secure HttpOnly SameSite=Strict with narrow
   media path. No wildcard credentialed CORS. Reauthorize GET/HEAD/Range/conditional
   before 200/206/304 and cached metadata; private,no-store is mandatory. Logout,
   revocation and deletion deny subsequent requests; open streams recheck within
   the qualified <=5s bound. Downloaded/browser-buffered bytes cannot be recalled.
8. **Source retention.** Occurrence expiry is derived from authoritative source
   trace/session lineage, never upload time, relink time or untrusted event time.
   Unknown finite-retention provenance denies. Unbound jobs expire within 24 hours
   or sooner with the source. Deletion generations fence initialization, binding,
   validation, promotion, derivatives and retries. Wire every trace/session and
   tenant deletion/retention path, including ClickHouse-only sources and enterprise
   sweeps. Outbox cleanup covers versions, derivatives, temp files, parts and caches;
   backups need expiry and restore tombstone replay, not an instant-erasure claim.
9. **Aggregate and decoder bounds.** Enforce atomic project/org/global reservations,
   rate/egress/concurrency/response/time bounds, settlement exactly once and bounded
   tombstones. Decoders are credential-free, network-disabled, read-only-input
   isolated workers with memory/CPU/time/disk/FD/protocol limits. A timeout around an
   unrestricted decoder is insufficient. Probe actual MIME/expansion/duration/
   channels and corruption. Never execute HTML/SVG; active document previews need
   separately sandboxed inert derivatives.

Recording and late-arrival truth
--------------------------------

Upload lifecycle, occurrence binding and capture completeness are separate.
``ready`` bytes become readable only with a live, retained, authorized, observed
binding. A root arriving after upload can release quarantined bytes without an SDK
retry. Missing/tail-sampled roots never become readable. Terminal deletion/expiry
cannot be reversed by late validation or binding.

``RecordingManifest`` records session-relative milliseconds, sample format/rate,
codec/container, actual channel/track roles, ordered segment occurrences, gaps and
playout evidence. Channel indexes cover each channel once; segment indexes are
contiguous and ordered by start time. Segments and explicit gaps cover the full
duration without overlaps. For ``separate_tracks``, each segment and gap includes
its ``track_id`` and coverage is checked independently per track; simultaneous
segments on different tracks are valid. Other layouts use one shared timeline
and omit per-segment track IDs. Every interval stays within the declared duration.
``capture_complete=true`` forbids capture gaps. A playable partial recording can
still have ``state=ready``. Unknown playout is labelled unknown, not asserted heard.
Mixed direction is mixed; left channel is never implicitly the customer.

Pipecat binding observes an application's existing processor without starting it;
creating one needs the separate recording opt-in and explicit pipeline/session
hookup. LiveKit report paths alone confer no filesystem authority: binding must
establish a trusted session-owned regular-file lease, no-follow traversal and an
independent reader held through bounded finalization. Egress start/stop is not
completion. Only authorized terminal outputs, or explicitly labelled allowed
partials, are importable. Exact upstream versions and real-session evidence belong
to adapter qualification, not this schema freeze.

New recordings use server-produced playable channel derivatives and bounded peaks.
They must bypass whole-file browser stereo decode/splitting. Grant renewal preserves
seek; mixed and multitrack playback expose the same play/pause/seek/time/events
interface. New occurrence IDs never fall back to legacy URLs. Legacy playback is
a separate compatibility/security tier, not retroactively private or revocable.

Conformance and revisions
-------------------------

Run on an isolated cloud runner, not in an application process::

    python3 -m pip install -r spec/attachments/v1/requirements.txt
    python3 -I spec/attachments/v1/validate.py
    python3 -I spec/attachments/v1/test_contract.py
    python3 -I spec/attachments/v1/test_protocol.py

The validator never fetches remote schemas. Unit tests include deletion of every
privacy/security rule as a negative control. The synthetic loopback protocol test
checks HTTP schema composition and nonreflection of invalid canary fields; it does
not qualify production auth, storage, decoders, networking, providers or playback.
Those boundaries require the same fixtures against real implementations plus the
integrated security and voice gates. No component can claim support from fixture
success or a registry entry alone.

Consumers pin the exact contract commit and retain fixture IDs/expectations.
Changes to wire shapes, defaults, semantic facts, limits or expected outcomes need
an explicit contract revision and coordinated consumer updates; do not silently
weaken a predicate to make an implementation pass. Local fixture validators are
reference oracles, not production authorization libraries.
