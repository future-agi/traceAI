package ai.traceai.koog

import ai.koog.prompt.executor.clients.openai.OpenAIClientSettings
import ai.koog.prompt.executor.clients.openai.OpenAILLMClient
import ai.koog.prompt.executor.llms.MultiLLMPromptExecutor
import ai.koog.prompt.llm.LLMProvider
import ai.koog.utils.io.use
import io.opentelemetry.api.common.AttributeKey.stringKey
import io.opentelemetry.api.trace.StatusCode
import io.opentelemetry.sdk.trace.data.SpanData
import kotlinx.coroutines.runBlocking
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlin.test.assertTrue

/**
 * Runs a real Koog 1.3.0 agent (real OpenTelemetry feature, real OpenAI client) against a local fake
 * model, with an in-memory exporter behind the recipe's wrapper. No network.
 */
class AgentTraceTest {
    private val marker = "KOOGTESTMARK7"
    private val session = "session-test-1"

    private fun run(captureContent: Boolean, failTool: Boolean = false): List<SpanData> {
        val memory = CollectingExporter()
        FakeOpenAI(marker).use { fake ->
            val config = FutureAgiTelemetry.Config("http://unused", "k", "s", "koog-test", captureContent = captureContent)
            val telemetry = FutureAgiTelemetry(config, delegate = memory)
            val client = OpenAILLMClient(apiKey = "test-key", settings = OpenAIClientSettings(baseUrl = fake.baseUrl))
            val tools = WeatherTools(marker, failWith = if (failTool) "backend down $marker" else null)
            runBlocking {
                try {
                    weatherAgent(MultiLLMPromptExecutor(LLMProvider.OpenAI to client), customModel("fake-model"), tools, telemetry)
                        .use { it.run("Weather in Paris? ref $marker", session) }
                } finally {
                    telemetry.shutdown()
                }
            }
        }
        return memory.spans.toList()
    }

    @Test
    fun `closing one agent does not close shared telemetry and repeated configure does not duplicate spans`() {
        val memory = CollectingExporter()
        val telemetry = FutureAgiTelemetry(FutureAgiTelemetry.Config("http://localhost", "k", "s", "shared"), memory)
        FakeOpenAI(marker).use { fake ->
            runBlocking {
                try {
                    repeat(2) { index ->
                        val client = OpenAILLMClient(apiKey = "test-key", settings = OpenAIClientSettings(baseUrl = fake.baseUrl))
                        weatherAgent(MultiLLMPromptExecutor(LLMProvider.OpenAI to client), customModel("fake-model"), WeatherTools(marker), telemetry)
                            .use { it.run("Weather?", "session-$index") }
                    }
                } finally {
                    telemetry.shutdown()
                }
            }
        }
        assertEquals(2, memory.spans.named("create_agent").size)
        assertEquals(2, memory.spans.named("invoke_agent").size)
        assertEquals(4, memory.spans.named("chat ").size)
        assertEquals(memory.spans.size, memory.spans.map { it.spanId }.toSet().size)
        assertEquals(setOf("session-0", "session-1"), memory.spans.named("invoke_agent").map { it.str("session.id") }.toSet())
    }

    private fun List<SpanData>.named(prefix: String) = filter { it.name.startsWith(prefix) }
    private fun List<SpanData>.parentOf(s: SpanData) = single { it.spanId == s.parentSpanId }
    private fun SpanData.str(key: String) = attributes.get(stringKey(key))

    @Test
    fun `full span tree is exported after shutdown, one trace, Koog parent links`() {
        val spans = run(captureContent = false)
        assertEquals(1, spans.map { it.traceId }.distinct().size)
        val createAgent = spans.named("create_agent").single()
        val invoke = spans.named("invoke_agent").single()
        assertEquals(2, spans.named("chat ").size)
        assertEquals(createAgent.spanId, invoke.parentSpanId)
        // execute_tool -> node nodeExecuteTool -> strategy single_run -> invoke_agent
        val tool = spans.named("execute_tool getWeather").single()
        val node = spans.parentOf(tool)
        assertEquals("node nodeExecuteTool", node.name)
        val strategy = spans.parentOf(node)
        assertEquals("strategy single_run", strategy.name)
        assertEquals(invoke.spanId, strategy.parentSpanId)
        assertEquals("koog-test", createAgent.resource.attributes.get(stringKey("project_name")))
        assertEquals("observe", createAgent.resource.attributes.get(stringKey("project_type")))
    }

    @Test
    fun `model, tokens, session and span kinds`() {
        val spans = run(captureContent = false)
        val firstChat = spans.named("chat ").minBy { it.startEpochNanos }
        assertEquals("fake-model", firstChat.str("gen_ai.request.model"))
        // opentelemetry-kotlin 0.3.0 turns every attribute into a string when it hands spans to a Java exporter.
        assertEquals("101", firstChat.str("gen_ai.usage.input_tokens"))
        assertEquals("11", firstChat.str("gen_ai.usage.output_tokens"))
        assertEquals(session, firstChat.str("gen_ai.conversation.id"))
        assertEquals(session, firstChat.str("session.id"))
        assertEquals("AGENT", spans.named("invoke_agent").single().str("fi.span.kind"))
        assertEquals("CHAIN", spans.named("node nodeCallLLM").single().str("fi.span.kind"))
        assertEquals(null, firstChat.str("fi.span.kind"), "chat spans are left for the collector to map")
    }

    @Test
    fun `content off by default - the marker is nowhere in the export`() {
        val spans = run(captureContent = false)
        spans.forEach { s ->
            assertFalse(marker in s.attributes.toString(), "attributes of ${s.name}")
            assertFalse(marker in s.resource.attributes.toString())
            assertFalse(marker in s.status.description)
            assertTrue(s.events.isEmpty())
            assertFalse("test-key" in s.attributes.toString(), "model API key must never be exported")
        }
        assertEquals(FutureAgiSpanExporter.HIDDEN, spans.named("execute_tool").single().str("gen_ai.tool.call.arguments"))
    }

    @Test
    fun `opt-in exports content`() {
        val spans = run(captureContent = true)
        val tool = spans.named("execute_tool").single()
        assertTrue(marker in tool.str("gen_ai.tool.call.arguments").orEmpty())
        assertTrue(marker in tool.str("gen_ai.tool.call.result").orEmpty())
        assertTrue(spans.any { marker in it.str("gen_ai.output.messages").orEmpty() })
    }

    @Test
    fun `tool failure is ERROR with error type, raw text only with opt-in`() {
        val off = run(captureContent = false, failTool = true).named("execute_tool").single()
        assertEquals(StatusCode.ERROR, off.status.statusCode)
        assertEquals("", off.status.description)
        assertNotNull(off.str("error.type"))
        val onSpans = run(captureContent = true, failTool = true)
        val on = onSpans.named("execute_tool").single()
        assertEquals(StatusCode.ERROR, on.status.statusCode)
        assertTrue(marker in on.status.description)
        // Koog serializes the exception's stack trace into the node output; it is emptied even with the opt-in.
        assertTrue(onSpans.any { "stackTrace" in it.attributes.toString() }, "Koog 1.3.0 shape changed")
        onSpans.forEach { s ->
            assertFalse("\\tat " in s.attributes.toString(), "stack trace in ${s.name}")
            assertFalse(s.events.any { it.attributes.get(stringKey("exception.stacktrace")) != null })
        }
    }
}
