package ai.traceai.koog

import io.opentelemetry.api.common.AttributeKey.stringKey
import io.opentelemetry.api.common.Attributes
import io.opentelemetry.api.trace.SpanKind
import io.opentelemetry.api.trace.StatusCode
import io.opentelemetry.sdk.common.CompletableResultCode
import io.opentelemetry.sdk.testing.trace.TestSpanData
import io.opentelemetry.sdk.trace.data.EventData
import io.opentelemetry.sdk.trace.data.SpanData
import io.opentelemetry.sdk.trace.data.StatusData
import io.opentelemetry.sdk.trace.export.SpanExporter
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

class FutureAgiSpanExporterTest {
    private val marker = "UNITMARK42"

    private fun span(attributes: Attributes, status: StatusData = StatusData.ok(), events: List<EventData> = emptyList()): SpanData =
        TestSpanData.builder()
            .setName("execute_tool getWeather")
            .setKind(SpanKind.INTERNAL)
            .setStartEpochNanos(1)
            .setEndEpochNanos(2)
            .setHasEnded(true)
            .setStatus(status)
            .setAttributes(attributes)
            .setEvents(events)
            .setTotalRecordedEvents(events.size)
            .build()

    private val toolAttrs = Attributes.builder()
        .put("gen_ai.operation.name", "execute_tool")
        .put("gen_ai.tool.name", "getWeather")
        .put("gen_ai.tool.call.arguments", """{"city":"$marker"}""")
        .put("gen_ai.tool.call.result", "sunny $marker")
        .put("system_instructions", "be nice $marker")
        .put("koog.node.input", "")
        .build()

    @Test
    fun `content off masks content keys even when Koog verbose mode leaked them`() {
        val out = FutureAgiSpanExporter.transform(span(toolAttrs), captureContent = false)
        assertEquals(FutureAgiSpanExporter.HIDDEN, out.attributes.get(stringKey("gen_ai.tool.call.arguments")))
        assertEquals(FutureAgiSpanExporter.HIDDEN, out.attributes.get(stringKey("gen_ai.tool.call.result")))
        assertEquals(FutureAgiSpanExporter.HIDDEN, out.attributes.get(stringKey("system_instructions")))
        assertEquals("", out.attributes.get(stringKey("koog.node.input")), "empty stays empty, like Koog")
        assertEquals("getWeather", out.attributes.get(stringKey("gen_ai.tool.name")))
        assertFalse(out.toString().contains(marker))
    }

    @Test
    fun `content on keeps content`() {
        val out = FutureAgiSpanExporter.transform(span(toolAttrs), captureContent = true)
        assertEquals("sunny $marker", out.attributes.get(stringKey("gen_ai.tool.call.result")))
    }

    @Test
    fun `content off blanks raw error text and drops events but keeps ERROR`() {
        val event = EventData.create(3, "exception", Attributes.of(stringKey("exception.message"), "boom $marker"))
        val failed = span(toolAttrs, StatusData.create(StatusCode.ERROR, "boom $marker at /Users/someone/app"), listOf(event))
        val off = FutureAgiSpanExporter.transform(failed, captureContent = false)
        assertEquals(StatusCode.ERROR, off.status.statusCode)
        assertEquals("", off.status.description)
        assertTrue(off.events.isEmpty())
        assertEquals(0, off.totalRecordedEvents)
        val on = FutureAgiSpanExporter.transform(failed, captureContent = true)
        assertTrue(on.status.description.contains(marker))
        assertEquals(1, on.events.size)
    }

    @Test
    fun `stack traces are never exported, even with content on`() {
        val nodeOutput = """{"toolResults":[{"tool":"getWeather","output":"failed: boom $marker","resultKind":{"error":{"message":"boom $marker","stackTrace":"java.lang.IllegalStateException: boom\n\tat ai.example.Tools.getWeather(Tools.kt:12)\n\tat java.base/java.lang.Thread.run(Thread.java:1583)"}}}]}"""
        val attrs = Attributes.builder().put("koog.node.id", "nodeExecuteTool").put("koog.node.output", nodeOutput).build()
        val event = EventData.create(
            3, "exception",
            Attributes.builder().put("exception.message", "boom $marker").put("exception.stacktrace", "at ai.example.Tools.getWeather(Tools.kt:12)").build(),
        )
        val on = FutureAgiSpanExporter.transform(span(attrs, events = listOf(event)), captureContent = true)
        val output = on.attributes.get(stringKey("koog.node.output")).orEmpty()
        assertFalse("\\tat " in output, output)
        assertTrue(""""stackTrace":""""" in output)
        assertTrue("boom $marker" in output, "the error message itself is content and stays with the opt-in")
        assertNull(on.events.single().attributes.get(stringKey("exception.stacktrace")))
        assertEquals("boom $marker", on.events.single().attributes.get(stringKey("exception.message")))
        val off = FutureAgiSpanExporter.transform(span(attrs, events = listOf(event)), captureContent = false)
        assertEquals(FutureAgiSpanExporter.HIDDEN, off.attributes.get(stringKey("koog.node.output")))
    }

    @Test
    fun `span kind is added only where Future AGI would show unknown`() {
        fun kind(vararg kv: Pair<String, String>): String? {
            val b = Attributes.builder(); kv.forEach { (k, v) -> b.put(k, v) }
            return FutureAgiSpanExporter.spanKindFor(b.build())
        }
        assertEquals("AGENT", kind("gen_ai.operation.name" to "invoke_agent"))
        assertEquals("AGENT", kind("gen_ai.operation.name" to "create_agent"))
        assertEquals("CHAIN", kind("koog.strategy.name" to "single_run"))
        assertEquals("CHAIN", kind("koog.node.id" to "nodeCallLLM"))
        assertEquals("CHAIN", kind("koog.subgraph.id" to "sub"))
        assertNull(kind("gen_ai.operation.name" to "chat"))
        assertNull(kind("gen_ai.operation.name" to "execute_tool"))
        assertNull(kind("gen_ai.operation.name" to "invoke_agent", "fi.span.kind" to "LLM"))
        assertNull(kind("mcp.method.name" to "tools/call"))
    }

    @Test
    fun `conversation id becomes session id unless the app set one`() {
        val a = FutureAgiSpanExporter.transformAttributes(Attributes.of(stringKey("gen_ai.conversation.id"), "s-1"), false)
        assertEquals("s-1", a.get(stringKey("session.id")))
        val b = FutureAgiSpanExporter.transformAttributes(
            Attributes.builder().put("gen_ai.conversation.id", "s-1").put("session.id", "mine").build(), false,
        )
        assertEquals("mine", b.get(stringKey("session.id")))
    }

    @Test
    fun `export waits for the delegate and reports its result`() {
        val pending = CompletableResultCode()
        val delegate = object : SpanExporter {
            override fun export(spans: Collection<SpanData>) = pending
            override fun flush() = CompletableResultCode.ofSuccess()
            override fun shutdown() = CompletableResultCode.ofSuccess()
        }
        val exporter = FutureAgiSpanExporter(delegate, captureContent = false)
        val pool = Executors.newSingleThreadExecutor()
        val started = CountDownLatch(1)
        val future = pool.submit<CompletableResultCode> { started.countDown(); exporter.export(listOf(span(toolAttrs))) }
        started.await()
        Thread.sleep(100)
        assertFalse(future.isDone, "export must block until the HTTP request finished")
        pending.succeed()
        assertTrue(future.get(5, TimeUnit.SECONDS).isSuccess)
        pool.shutdown()
    }

    @Test
    fun `shutdown waits for in-flight exports and then rejects new ones`() {
        val inner = CollectingExporter()
        val exporter = FutureAgiSpanExporter(inner, captureContent = false)
        assertTrue(exporter.export(listOf(span(toolAttrs))).isSuccess)
        assertTrue(exporter.shutdown().isSuccess)
        assertFalse(exporter.export(listOf(span(toolAttrs))).isSuccess)
        assertEquals(1, inner.spans.size)
    }
}
