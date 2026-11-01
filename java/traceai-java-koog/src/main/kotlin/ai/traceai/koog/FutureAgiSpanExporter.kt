package ai.traceai.koog

import io.opentelemetry.api.common.AttributeKey
import io.opentelemetry.api.common.Attributes
import io.opentelemetry.api.trace.StatusCode
import io.opentelemetry.sdk.common.CompletableResultCode
import io.opentelemetry.sdk.trace.data.DelegatingSpanData
import io.opentelemetry.sdk.trace.data.EventData
import io.opentelemetry.sdk.trace.data.SpanData
import io.opentelemetry.sdk.trace.data.StatusData
import io.opentelemetry.sdk.trace.export.SpanExporter
import java.util.concurrent.TimeUnit
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/**
 * Wraps the OTLP exporter that sends Koog spans to Future AGI.
 *
 * It does three things, and nothing else:
 *
 * 1. Privacy. Unless [captureContent] is true, it keeps message content out of the export even if
 *    Koog's verbose mode was switched on elsewhere: the content attributes Koog masks by default are
 *    masked again, the raw error text in the span status is blanked (Koog does not mask it), and span
 *    events are dropped. The status code and `error.type` are kept. Stack traces are never exported:
 *    with content on, the `stackTrace` fields Koog serializes into node inputs/outputs are emptied and
 *    `exception.stacktrace` is removed from span events.
 * 2. Display. It adds `fi.span.kind` where Future AGI would otherwise show `unknown`
 *    (`invoke_agent` / `create_agent` become AGENT; Koog strategy, node and subgraph spans become CHAIN),
 *    and copies `gen_ai.conversation.id` to `session.id` so runs show up in Sessions.
 *    `chat` and `execute_tool` spans are left as Koog wrote them; Future AGI already maps those.
 * 3. Delivery. Koog 1.3.0 bridges Java exporters through opentelemetry-kotlin 0.3.0, which does not
 *    wait for the Java exporter's asynchronous result. This wrapper waits for each export to finish,
 *    and [shutdown] waits for in-flight exports. Failed or timed-out operations return failure.
 */
class FutureAgiSpanExporter(
    private val delegate: SpanExporter,
    private val captureContent: Boolean,
    private val exportTimeoutMillis: Long = 30_000,
) : SpanExporter {

    init {
        require(exportTimeoutMillis > 0) { "Export timeout must be positive." }
    }

    private var shutdownResult: CompletableResultCode? = null

    // The Kotlin bridge discards asynchronous Java results. Serialize lifecycle operations and
    // await each result on the batch worker so shutdown cannot pass an admitted export.
    @Synchronized
    override fun export(spans: Collection<SpanData>): CompletableResultCode {
        if (shutdownResult != null) return CompletableResultCode.ofFailure()
        return await { delegate.export(spans.map { transform(it, captureContent) }) }
    }

    @Synchronized
    override fun flush(): CompletableResultCode {
        if (shutdownResult != null) return CompletableResultCode.ofFailure()
        return await { delegate.flush() }
    }

    @Synchronized
    override fun shutdown(): CompletableResultCode {
        shutdownResult?.let { return it }
        // Mark closed before entering the delegate, including when it throws.
        shutdownResult = CompletableResultCode.ofFailure()
        return await { delegate.shutdown() }.also { shutdownResult = it }
    }

    private fun await(operation: () -> CompletableResultCode): CompletableResultCode = try {
        val result = operation().join(exportTimeoutMillis, TimeUnit.MILLISECONDS)
        if (result.isSuccess) CompletableResultCode.ofSuccess() else CompletableResultCode.ofFailure()
    } catch (_: RuntimeException) {
        // An exporter must not turn an application failure into a telemetry exception.
        CompletableResultCode.ofFailure()
    }

    companion object {
        /** The placeholder Koog itself writes for masked content. */
        const val HIDDEN = "HIDDEN:non-empty"

        /** Attributes Koog masks when verbose mode is off (Koog 1.3.0 `HiddenString` attributes). */
        val CONTENT_KEYS: Set<String> = setOf(
            "gen_ai.input.messages",
            "gen_ai.output.messages",
            "gen_ai.system_instructions",
            // Koog 1.3.0 writes system instructions under this key, not gen_ai.system_instructions.
            "system_instructions",
            "gen_ai.tool.definitions",
            "gen_ai.tool.call.arguments",
            "gen_ai.tool.call.result",
            "koog.node.input",
            "koog.node.output",
            "koog.subgraph.input",
            "koog.subgraph.output",
            "koog.moderation.result",
        )

        private val FI_SPAN_KIND = AttributeKey.stringKey("fi.span.kind")
        private val GEN_AI_SPAN_KIND = AttributeKey.stringKey("gen_ai.span.kind")
        private val OPERATION = AttributeKey.stringKey("gen_ai.operation.name")
        private val CONVERSATION_ID = AttributeKey.stringKey("gen_ai.conversation.id")
        private val SESSION_ID = AttributeKey.stringKey("session.id")
        private val KOOG_CHAIN_KEYS = listOf("koog.strategy.name", "koog.node.id", "koog.subgraph.id")
        private val EXCEPTION_STACKTRACE = AttributeKey.stringKey("exception.stacktrace")

        /** Remove serialized traces, including JSON strings nested inside node results. */
        internal fun stripStackTraces(text: String): String = runCatching {
            scrubJson(Json.parseToJsonElement(text), 0).toString()
        }.getOrElse { HIDDEN }

        private fun scrubJson(value: JsonElement, depth: Int): JsonElement {
            require(depth < 64) { "Telemetry content is too deeply nested." }
            return when (value) {
                is JsonObject -> JsonObject(value.mapValues { (key, child) ->
                    if (key == "stackTrace" || key == "exception.stacktrace") JsonPrimitive("")
                    else scrubJson(child, depth + 1)
                })
                is JsonArray -> JsonArray(value.map { scrubJson(it, depth + 1) })
                is JsonPrimitive -> if (value.isString &&
                    ("stackTrace" in value.content || "exception.stacktrace" in value.content)) {
                    // A tool result may itself be serialized JSON. If it is malformed, mask it.
                    JsonPrimitive(runCatching {
                        scrubJson(Json.parseToJsonElement(value.content), depth + 1).toString()
                    }.getOrElse { HIDDEN })
                } else value
            }
        }

        /** Span kind Future AGI should show, or null when the collector already maps the span. */
        internal fun spanKindFor(attributes: Attributes): String? {
            if (attributes.get(FI_SPAN_KIND) != null || attributes.get(GEN_AI_SPAN_KIND) != null) return null
            return when (attributes.get(OPERATION)) {
                "invoke_agent", "create_agent" -> "AGENT"
                null -> if (KOOG_CHAIN_KEYS.any { attributes.get(AttributeKey.stringKey(it)) != null }) "CHAIN" else null
                else -> null
            }
        }

        internal fun transformAttributes(attributes: Attributes, captureContent: Boolean): Attributes {
            val builder = attributes.toBuilder().remove(EXCEPTION_STACKTRACE)
            if (!captureContent) builder.remove(AttributeKey.stringKey("exception.message"))
            attributes.forEach { key, value ->
                if (key.key in CONTENT_KEYS) {
                    val text = value.toString()
                    val exported = when {
                        !captureContent -> if (text.isEmpty()) "" else HIDDEN
                        // If stripping ever fails, mask the value rather than lose the span or leak the trace.
                        "stackTrace" in text || "exception.stacktrace" in text -> stripStackTraces(text)
                        else -> null
                    }
                    if (exported != null) {
                        builder.remove(key)
                        builder.put(AttributeKey.stringKey(key.key), exported)
                    }
                }
            }
            spanKindFor(attributes)?.let { builder.put(FI_SPAN_KIND, it) }
            val conversation = attributes.get(CONVERSATION_ID)
            if (conversation != null && attributes.get(SESSION_ID) == null) builder.put(SESSION_ID, conversation)
            return builder.build()
        }

        internal fun transform(span: SpanData, captureContent: Boolean): SpanData {
            val attributes = transformAttributes(span.attributes, captureContent)
            val status = if (!captureContent && span.status.statusCode == StatusCode.ERROR && span.status.description.isNotEmpty()) {
                StatusData.create(StatusCode.ERROR, "")
            } else {
                span.status
            }
            val events: List<EventData> = if (!captureContent) emptyList() else span.events.map { event ->
                if (event.attributes.get(EXCEPTION_STACKTRACE) == null) event
                else EventData.create(
                    event.epochNanos,
                    event.name,
                    event.attributes.toBuilder().remove(EXCEPTION_STACKTRACE).build(),
                    event.totalAttributeCount,
                )
            }
            return object : DelegatingSpanData(span) {
                override fun getAttributes(): Attributes = attributes
                override fun getTotalAttributeCount(): Int = maxOf(span.totalAttributeCount, attributes.size())
                override fun getStatus(): StatusData = status
                override fun getEvents(): List<EventData> = events
                override fun getTotalRecordedEvents(): Int = if (captureContent) span.totalRecordedEvents else 0
            }
        }
    }
}
