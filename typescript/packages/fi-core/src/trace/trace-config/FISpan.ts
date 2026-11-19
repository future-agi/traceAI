import {
    Span,
    SpanContext,
    SpanStatus,
    SpanStatusCode,
    TimeInput,
    Attributes,
    Exception,
    AttributeValue,
    Link,
    diag,
  } from "@opentelemetry/api";
  import { TraceConfig } from "./types";
  import { mask, maskAttributes, redactValue } from "./maskingRules";
  import { redactPiiInString } from "./piiRedaction";
  import { privacyFilter, rememberAuthoredRedactions, rememberMediaAdmission, rememberProjectedAttributeDrops, rememberSpanPrivacyFailure } from "../../export-privacy";
  import { type PrivacyHints, providerResponseBinding } from "../../privacy-runtime";
  import { DROPPED_CONTENT_COUNT, LEGACY_DROPPED_COUNT, PROJECTION_METADATA, projectAttributes } from "../../attribute-projection";
  import {
    ERROR_TYPE,
    ERROR_TYPE_OTHER,
    EXCEPTION_EVENT_NAME,
    EXCEPTION_MESSAGE,
    EXCEPTION_STACKTRACE,
    EXCEPTION_TYPE,
    getErrorType,
    getExceptionEventAttributes,
    sanitizeStatus,
  } from "./errorDetails";
  
  /**
   * True when the value is a {@link TimeInput} (HrTime, epoch millis or Date) rather than {@link Attributes}
   */
  function isTimeInput(value: unknown): value is TimeInput {
    return (
      typeof value === "number" ||
      value instanceof Date ||
      (Array.isArray(value) &&
        value.length === 2 &&
        typeof value[0] === "number" &&
        typeof value[1] === "number")
    );
  }
  
  /**
   * A wrapper around the OpenTelemetry {@link Span} interface that masks sensitive information based on the passed in {@link TraceConfig}.
   *
   * It is also the single place where the error-detail policy is enforced (see `errorDetails.ts`),
   * for {@link recordException}, {@link setStatus}, {@link addEvent} and {@link setAttribute(s)}:
   * - always: an ERROR status description is the exception message only, never a stack trace, and a
   *   span that ends in ERROR gets `error.type`;
   * - with `hideErrorDetails` on: the ERROR status has no description, and `exception.message`,
   *   `exception.stacktrace`, `error.message` (and `*.error.message`-style keys) never reach the
   *   underlying span, neither as span attributes nor on events (`exception.type`,
   *   `exception.escaped` and other attributes are kept).
   *
   * The attributes passed when the span is created go through the same masking in
   * {@link FITracer}; pass them here as `startAttributes` (already masked) so the span knows about
   * an `error.type` set at creation.
   */
  export class FISpan implements Span {
    private readonly span: Span;
    private readonly config: TraceConfig;
    private isEnded: boolean = false;
    /** The instrumentation set `error.type` itself; keep its value */
    private hasExplicitErrorType: boolean = false;
    /** `error.type` derived from the last recorded exception */
    private recordedErrorType?: string;
    /** The last exception recorded on the span, used to reduce "Name: message" descriptions */
    private lastException?: Exception;
    /** The last status set on the span is ERROR */
    private hasErrorStatus: boolean = false;
    private pendingAttributes: Attributes;
    // Host onStart annotations may already be SDK-truncated; they cannot attest
    // to a complete MIME discriminator. Only caller-supplied keys can do so.
    private readonly admittedKeys: Set<string>;
    private readonly attributeCountLimit: number;
    private projectedDropped: number;
    private readonly privacy: ReturnType<typeof privacyFilter>;
    private admissionFailed = false;
    private readonly autoDestination: boolean;
    private authoredHints: PrivacyHints;

    /** @internal Native application context in auto mode; explicit standalone
     * callers keep the wrapper. Instrumentors continue writing through `this`. */
    getContextSpan(): Span { return this.autoDestination ? this.span : this; }

    constructor({
      span,
      config,
      startAttributes,
      attributeCountLimit = 128,
      projectedDropped = 0,
      autoDestination = false,
      startPrivacyHints = new Map(),
      startAdmissionFailed = false,
    }: {
      span: Span;
      config: TraceConfig;
      /** The (already masked) attributes the span was created with */
      startAttributes?: Attributes;
      attributeCountLimit?: number;
      projectedDropped?: number;
      /** @internal Selected by an auto-only tracer facade, not by telemetry. */
      autoDestination?: boolean;
      startPrivacyHints?: PrivacyHints;
      startAdmissionFailed?: boolean;
    }) {
      this.span = span;
      this.config = config;
      this.autoDestination = autoDestination;
      this.authoredHints = startPrivacyHints;
      this.admissionFailed = autoDestination && startAdmissionFailed;
      this.pendingAttributes = autoDestination ? {} : { ...startAttributes };
      this.admittedKeys = new Set(Object.keys(this.pendingAttributes));
      // A proxy/custom tracer can hide getSpanLimits while returning a real SDK
      // span. Its own code-configured capacity governs late re-admission too;
      // using the fallback 128 lets earlier content evict usage at a 16-slot host.
      const directCount = Object.getOwnPropertyDescriptor(span, "_attributeCountLimit");
      const limits = Object.getOwnPropertyDescriptor(span, "_spanLimits");
      const count = limits && "value" in limits && limits.value && typeof limits.value === "object"
        ? Object.getOwnPropertyDescriptor(limits.value, "attributeCountLimit") : undefined;
      const nativeLimit = directCount && "value" in directCount ? directCount.value
        : count && "value" in count ? count.value : undefined;
      this.attributeCountLimit = typeof nativeLimit === "number" && Number.isFinite(nativeLimit) && nativeLimit >= 0
        ? nativeLimit : attributeCountLimit;
      this.projectedDropped = projectedDropped;
      this.privacy = privacyFilter(config);
      if (startAttributes?.[ERROR_TYPE] != null) {
        this.hasExplicitErrorType = true;
      }
      const wrappedSpanContext = span.spanContext();
      const isNoOp = wrappedSpanContext.traceFlags === 0 && wrappedSpanContext.spanId === '0000000000000000';
      // diag.debug(
      //     `FISpan CONSTRUCTOR: Wrapping span. ID: ${wrappedSpanContext.spanId}, TraceID: ${wrappedSpanContext.traceId}, TraceFlags: ${wrappedSpanContext.traceFlags}, Is NoOp: ${isNoOp}`
      // );
    }
  
    /**
     * Returns the value to write for the attribute, or undefined when it must be dropped
     */
    private maskAttribute(key: string, value?: AttributeValue): AttributeValue | undefined {
      const maskedValue = mask({ config: this.config, key, value });
      if (key === ERROR_TYPE && maskedValue != null) {
        this.hasExplicitErrorType = true;
      }
      return maskedValue;
    }

    /**
     * Event attributes (the recorded exception included) get the value-level redaction span
     * attributes get (see {@link redactValue}): the base64 image cap and PII redaction. It runs
     * before {@link maskAttributes}, which then applies the key-based content rules to events too.
     * Returns the same object when nothing changed.
     */
    private redactEventAttributes(attributes: Attributes | undefined): Attributes | undefined {
      if (attributes == null) return attributes;
      let changed = false;
      const redacted: Attributes = {};
      for (const [key, value] of Object.entries(attributes)) {
        const result = value == null ? value : redactValue(this.config, value);
        if (result !== value) changed = true;
        redacted[key] = result;
      }
      return changed ? redacted : attributes;
    }

    private rememberAuthoredHints(attributes: Attributes): void {
      if (!this.autoDestination || this.admissionFailed) return;
      try { this.authoredHints = this.privacy.admissionHints(attributes, this.authoredHints); }
      catch { this.admissionFailed = true; this.authoredHints = new Map(); }
    }

    /** Auto-only accounting bridge: the NUMBER of caller-supplied values FI
     * withheld or changed at write time is bound to the export lineage. No key,
     * value or native storage is retained, read back or replayed. */
    private rememberRedactions(original: Attributes, admitted: Attributes): void {
      if (!this.autoDestination) return;
      try { rememberAuthoredRedactions(this.span.spanContext(), original, admitted); }
      catch { /* Accounting never changes application behavior. */ }
    }

    /** Reproject the complete bounded library view when late usage/output arrives.
     * SDK spans expose a writable attribute data slot. Only our wrapped span is
     * updated; custom span implementations retain their normal public API path.
     */
    private admit(attributes: Attributes, singleKey?: string, original: Attributes = attributes): void {
      if (this.autoDestination) {
        // Never read/merge/replay foreign attributes. Host processors and active
        // application code may have written to the same native span since our
        // last call. Keep FI write-time protection, not pre-admission capture.
        let clean: Attributes = {};
        try { if (!this.admissionFailed) clean = this.privacy.admissionAttributes(attributes, this.span.spanContext(), this.authoredHints); }
        catch { this.admissionFailed = true; }
        this.rememberRedactions(original, clean);
        if (singleKey !== undefined) {
          if (clean[singleKey] != null) this.span.setAttribute(singleKey, clean[singleKey]!);
        } else this.span.setAttributes(clean);
        return;
      }
      const slot = Object.getOwnPropertyDescriptor(this.span, "attributes");
      const native = Object.getOwnPropertyDescriptor(this.span, "_droppedAttributesCount");
      let clean: Attributes = {};
      if (!this.admissionFailed) {
        try {
          // Preserve annotations made by host onStart processors. Projected
          // metadata is already represented by the complete pending view.
          const host = native && slot && "value" in slot ? { ...slot.value } : {};
          // Our own projection marker is recomputed from the complete view; a
          // caller-supplied count stays in pendingAttributes and accumulates once.
          delete host[PROJECTION_METADATA]; delete host[LEGACY_DROPPED_COUNT]; delete host[DROPPED_CONTENT_COUNT];
          clean = this.privacy.admissionAttributes({ ...host, ...this.pendingAttributes, ...attributes }, this.span.spanContext());
          for (const key of this.admittedKeys) if (!Object.prototype.hasOwnProperty.call(clean, key)) this.admittedKeys.delete(key);
          for (const key of Object.keys(attributes)) if (Object.prototype.hasOwnProperty.call(clean, key)) this.admittedKeys.add(key);
        } catch { this.admissionFailed = true; this.admittedKeys.clear(); }
      }
      this.pendingAttributes = clean;
      const projected = projectAttributes(clean, this.attributeCountLimit);
      if (slot && "value" in slot && slot.writable && native && "value" in native && native.writable && typeof native.value === "number") {
        // Clear SDK-owned storage so a late canonical field can displace earlier
        // content, instead of being rejected by setAttributes at the host limit.
        const prior = Math.max(0, native.value - this.projectedDropped);
        Object.defineProperty(this.span, "attributes", { ...slot, value: {} });
        // SDK 2.7+ caches cardinality separately; resetting only the map makes
        // every projected key look new against a permanently full old count.
        const count = Object.getOwnPropertyDescriptor(this.span, "_attributesCount");
        if (count && "value" in count && count.writable && typeof count.value === "number") Object.defineProperty(this.span, "_attributesCount", { ...count, value: 0 });
        this.span.setAttributes(projected.attributes);
        // A custom/native setter can still incur genuine SDK drops here. Keep
        // that delta instead of replacing it with FI's projection contribution.
        const after = Object.getOwnPropertyDescriptor(this.span, "_droppedAttributesCount");
        const hostAdded = after && "value" in after && typeof after.value === "number"
          ? Math.max(0, after.value - native.value) : 0;
        (this.span as any)._droppedAttributesCount = prior + hostAdded + projected.dropped;
        rememberProjectedAttributeDrops(this.span, projected.dropped);
        this.projectedDropped = projected.dropped;
      } else if (singleKey !== undefined && clean[singleKey] != null) {
        this.span.setAttribute(singleKey, clean[singleKey]!);
      } else this.span.setAttributes(clean);
    }

    setAttribute(key: string, value: AttributeValue): this {
      if (this.isEnded) {
        diag.warn(`FISpan.setAttribute called on ended span ID: ${this.span.spanContext().spanId} for key: ${key}`);
        return this;
      }
      this.rememberAuthoredHints({ [key]: value });
      const maskedValue = this.maskAttribute(key, value);
      if (maskedValue != null) {
        this.admit({ [key]: maskedValue }, key, { [key]: value });
      } else this.rememberRedactions({ [key]: value }, {});
      return this;
    }
  
    setAttributes(attributes: Attributes): this {
      if (this.isEnded) {
        diag.warn(`FISpan.setAttributes called on ended span ID: ${this.span.spanContext().spanId}`);
        return this;
      }
      this.rememberAuthoredHints(attributes);
      // Auto accounting compares FI's final write with the caller's own view,
      // not with the provider pre-pass below.
      const original = attributes;
      const responseContext = this.span.spanContext();
      // Only parser-bound output enters this pre-mask pass. This lets PII/text
      // policy mask the whole logprobs block before per-token masking loses context.
      if (Object.entries(attributes).some(([key, value]) => providerResponseBinding(responseContext, key, value))) {
        try { attributes = this.privacy.admissionAttributes(attributes, responseContext, this.autoDestination ? this.authoredHints : undefined); }
        catch { this.admissionFailed = true; attributes = {}; }
      }
      const maskedAttributes = maskAttributes({ config: this.config, attributes });
      if (maskedAttributes[ERROR_TYPE] != null) {
        this.hasExplicitErrorType = true;
      }
      this.admit(maskedAttributes, undefined, original);
      return this;
    }
  
    addEvent(
      name: string,
      attributesOrStartTime?: Attributes | TimeInput,
      startTime?: TimeInput,
    ): this {
      if (this.isEnded) return this;
      // addEvent(name, startTime) carries no attributes
      const [attributes, time] = isTimeInput(attributesOrStartTime)
        ? [undefined, attributesOrStartTime]
        : [attributesOrStartTime, startTime];
      if (name === EXCEPTION_EVENT_NAME) {
        const exceptionType = attributes?.[EXCEPTION_TYPE];
        if (typeof exceptionType === "string" && exceptionType.length > 0) {
          this.recordedErrorType = exceptionType;
        }
      }
      // Events are another export path for the same content: apply error-detail hiding,
      // content masking and PII redaction, just as for span and link attributes. The
      // value-level redaction (base64 image cap, then PII) runs first, see redactEventAttributes.
      // Newer SDKs also truncate event/link values. Protect their exact values
      // before admission, while retaining native event names/times and identity.
      let clean = attributes;
      try { if (attributes) clean = this.privacy.admissionAttributes(maskAttributes({ config: this.config, attributes: this.redactEventAttributes(attributes) ?? {} })); }
      catch { clean = {}; }
      this.span.addEvent(name, clean, time);
      return this;
    }

    setStatus(status: SpanStatus): this {
      if (this.isEnded) return this;
      // The SDK keeps the last status set, so the last one decides whether error.type is added
      this.hasErrorStatus = status.code === SpanStatusCode.ERROR;
      const sanitized = sanitizeStatus(status, this.config.hideErrorDetails, this.lastException);
      // Hiding takes precedence; redact only retained text, without mutating caller-owned status.
      // Reuse the standalone cap-before-PII policy, then the PII pass when piiRedaction is on,
      // then S2c's privacy admission.
      // Status is not a span attribute and must not bypass either boundary.
      if (typeof sanitized.message === "string") {
        let message: string | undefined;
        try {
          const capped = redactValue(this.config, sanitized.message);
          const redacted = this.config.piiRedaction && typeof capped === "string" ? redactPiiInString(capped) : capped;
          // Already-known secrets must be masked before any native exporter
          // sees this FI-authored write. Late registration still only rechecks
          // the detached auto view; end() does not replay this status.
          const clean = this.privacy.admissionAttributes({ "status.description": redacted });
          if (typeof clean["status.description"] === "string") message = clean["status.description"];
        } catch { /* Keep status code, never original detail, on privacy failure. */ }
        this.span.setStatus({ code: sanitized.code, ...(message === undefined ? {} : { message }) });
      } else this.span.setStatus(sanitized);
      return this;
    }
  
    updateName(name: string): this {
      if (this.isEnded) return this;
      this.span.updateName(name);
      return this;
    }
  
    end(endTime?: TimeInput): void {
      if (this.isEnded) {
        diag.warn(`FISpan.end called on already ended span ID: ${this.span.spanContext().spanId}`);
        return;
      }
      // A failing final write (a host span whose setters throw) must never skip the
      // underlying end(): the span API must not throw and the span must still close.
      try {
        if (this.hasErrorStatus && !this.hasExplicitErrorType) {
          const native = Object.getOwnPropertyDescriptor(this.span, "attributes");
          // Do not overwrite a foreign error.type. Unknown custom native storage
          // does not grant permission to synthesize over an application value.
          if (!this.autoDestination || native && "value" in native && native.value &&
              !Object.getOwnPropertyDescriptor(native.value, ERROR_TYPE)) {
            // Derived classifications need the same masking as explicit error.type attributes.
            try { this.setAttribute(ERROR_TYPE, this.recordedErrorType ?? ERROR_TYPE_OTHER); }
            catch (error) {
              // Logged here and rethrown, so the catch below still withholds FI content.
              diag.warn(`FISpan.end: final error.type write failed: ${error instanceof Error ? error.name : typeof error}`);
              throw error;
            }
          }
        }
        // Standalone keeps its native-wrapper re-admission. Auto's late-secret
        // recheck happens only on the detached export view, never by rewriting
        // application attributes or reconstructing what the host SDK discarded.
        if (!this.autoDestination) {
          this.admit({});
          rememberMediaAdmission(this.span, Object.fromEntries([...this.admittedKeys].map(key => [key, this.pendingAttributes[key]])));
        }
      } catch {
        this.admissionFailed = true;
        // Withhold the FI destination, not application-owned native storage.
        try { rememberSpanPrivacyFailure(this.span.spanContext()); } catch { /* Never prevent native end. */ }
        try { diag.warn("FISpan.end: final telemetry write failed; FI content withheld"); } catch { /* Diagnostics are not application failures. */ }
      } finally {
        try { this.span.end(endTime); }
        finally {
          this.isEnded = true;
          this.pendingAttributes = {};
          this.authoredHints = new Map();
          this.admittedKeys.clear();
        }
      }
      // diag.debug(
      //     `FISpan.end CALLED for span ID: ${this.span.spanContext().spanId}`
      // );
    }
  
    isRecording(): boolean {
      return this.span.isRecording();
    }
  
    /**
     * Records the "exception" event the way the OpenTelemetry SDK does (`exception.type`,
     * `exception.message`, `exception.stacktrace`), with an `exception.type` that is the same in
     * both modes (see {@link getExceptionEventAttributes}); the message and stack trace are left
     * out when error details are hidden. Retained attributes use the shared masking/PII policy.
     */
    recordException(exception: Exception, time?: TimeInput): void {
      if (this.isEnded) return;
      this.recordedErrorType = getErrorType(exception);
      this.lastException = exception;
      // The message and stack trace get the same value-level redaction as attributes (image cap,
      // PII); the error type is taken from the exception itself, above
      const raw = getExceptionEventAttributes(exception, this.config.hideErrorDetails);
      const redacted = this.redactEventAttributes(raw) ?? {};
      // A message that is a complete document is recognized on its own, but not
      // inside the stack prose ("Error: {...}"): after the stack's own redaction,
      // replace a message that survived verbatim with the message's redacted form.
      const message = raw[EXCEPTION_MESSAGE], stack = redacted[EXCEPTION_STACKTRACE];
      if (typeof message === "string" && message.length > 0 && typeof stack === "string" && stack.includes(message)) {
        const redactedMessage = redactValue(this.config, message);
        if (typeof redactedMessage === "string" && redactedMessage !== message) {
          redacted[EXCEPTION_STACKTRACE] = stack.split(message).join(redactedMessage);
        }
      }
      // Then the shared masking/PII policy, as for span attributes
      const attributes = maskAttributes({
        config: this.config,
        attributes: redacted,
      });
      if (Object.keys(attributes).length === 0) {
        // Same as the SDK: an exception event needs a type or a message
        diag.warn(`FISpan.recordException: nothing to record for ${typeof exception}`);
        return;
      }
      const errorType = this.recordedErrorType;
      this.addEvent(EXCEPTION_EVENT_NAME, attributes, time);
      // exception.type can be a generic class while error.type is the provider's
      // normalized name/code. addEvent's explicit-event fallback must not replace it.
      this.recordedErrorType = errorType;
    }
  
    spanContext(): SpanContext {
      return this.span.spanContext();
    }
  
    /** Link attributes get the same masking as span attributes */
    private maskLink(link: Link): Link {
      if (link.attributes == null) return link;
      try { return { ...link, attributes: this.privacy.admissionAttributes(link.attributes) }; }
      catch { return { ...link, attributes: {} }; }
    }

    addLink(link: Link): this {
      if (this.isEnded) return this;
      this.span.addLink(this.maskLink(link));
      return this;
    }
  
    addLinks(links: Link[]): this {
      if (this.isEnded) return this;
      this.span.addLinks(links.map((link) => this.maskLink(link)));
      return this;
    }
  }