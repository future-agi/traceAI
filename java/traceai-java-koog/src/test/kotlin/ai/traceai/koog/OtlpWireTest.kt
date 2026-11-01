package ai.traceai.koog

import ai.koog.prompt.executor.clients.openai.OpenAIClientSettings
import ai.koog.prompt.executor.clients.openai.OpenAILLMClient
import ai.koog.prompt.executor.llms.MultiLLMPromptExecutor
import ai.koog.prompt.llm.LLMProvider
import ai.koog.utils.io.use
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest
import io.opentelemetry.proto.trace.v1.Status.StatusCode
import kotlinx.coroutines.runBlocking
import org.junit.jupiter.params.ParameterizedTest
import org.junit.jupiter.params.provider.CsvSource
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

/** Real Koog -> maintained adapter -> real Java exporter -> localhost protobuf receiver. */
class OtlpWireTest {
    private val marker = "WIREMARK93"

    @ParameterizedTest
    @CsvSource("false,false", "true,false", "false,true", "true,true")
    fun `native span tree privacy and failure status survive the OTLP wire`(capture: Boolean, failTool: Boolean) {
        RecordingOtlpReceiver().use { receiver ->
            FakeOpenAI(marker).use { fake ->
                val config = FutureAgiTelemetry.Config(receiver.baseUrl, "fi-api-key-123", "fi-secret-456", "wire-project", captureContent = capture)
                val telemetry = FutureAgiTelemetry(config)
                val client = OpenAILLMClient(apiKey = "model-key-789", settings = OpenAIClientSettings(baseUrl = fake.baseUrl))
                val tools = WeatherTools(marker, if (failTool) "tool failed $marker" else null)
                runBlocking {
                    try {
                        weatherAgent(MultiLLMPromptExecutor(LLMProvider.OpenAI to client), customModel("fake-model"), tools, telemetry)
                            .use { it.run("Weather in Paris? ref $marker", "wire-session") }
                    } finally {
                        telemetry.shutdown()
                    }
                }
            }
            assertTrue(receiver.requests.isNotEmpty(), "nothing was exported")
            receiver.requests.forEach { r ->
                assertEquals("/tracer/v1/traces", r.path)
                assertEquals("fi-api-key-123", r.headers["x-api-key"])
                assertEquals("fi-secret-456", r.headers["x-secret-key"])
                assertEquals("application/x-protobuf", r.headers["content-type"])
                assertFalse(r.headers["content-encoding"].orEmpty().contains("gzip"))
            }
            val resources = receiver.requests.flatMap { ExportTraceServiceRequest.parseFrom(it.body).resourceSpansList }
            resources.forEach { resource ->
                val attrs = resource.resource.attributesList.associate { it.key to it.value.stringValue }
                assertEquals("wire-project", attrs["project_name"])
                assertEquals("observe", attrs["project_type"])
                assertEquals("koog-agent", attrs["service.name"])
                assertFalse(attrs.keys.any { it.startsWith("host.") || it.startsWith("process.") })
            }
            val spans = resources.flatMap { it.scopeSpansList }.flatMap { it.spansList }
            assertEquals(spans.size, spans.map { it.spanId }.toSet().size, "no duplicate exports or spans")
            assertEquals(1, spans.map { it.traceId }.toSet().size)
            val create = spans.single { it.name == "create_agent weather-agent" }
            val invoke = spans.single { it.name == "invoke_agent weather-agent" }
            val tool = spans.single { it.name == "execute_tool getWeather" }
            val chats = spans.filter { it.name == "chat fake-model" }
            assertEquals(2, chats.size, "adapter must not add model spans")
            assertEquals(create.spanId, invoke.parentSpanId)
            val node = spans.single { it.spanId == tool.parentSpanId }
            val strategy = spans.single { it.spanId == node.parentSpanId }
            assertEquals("node nodeExecuteTool", node.name)
            assertEquals(invoke.spanId, strategy.parentSpanId)
            assertEquals("AGENT", invoke.attributesList.single { it.key == "fi.span.kind" }.value.stringValue)
            assertEquals("CHAIN", node.attributesList.single { it.key == "fi.span.kind" }.value.stringValue)
            chats.forEach { chat ->
                val attrs = chat.attributesList.associate { it.key to it.value.stringValue }
                assertEquals("wire-session", attrs["session.id"])
                assertEquals("fake-model", attrs["gen_ai.request.model"])
                assertTrue(attrs.getValue("gen_ai.usage.input_tokens").toLong() > 0)
            }
            if (failTool) {
                assertEquals(StatusCode.STATUS_CODE_ERROR, tool.status.code)
                assertTrue(tool.attributesList.any { it.key == "error.type" })
                assertEquals(capture, tool.status.message.contains(marker))
            }
            if (!capture) spans.forEach {
                assertEquals(0, it.eventsCount)
                assertEquals("", it.status.message)
            }
            val body = receiver.requests.joinToString("") { it.body.decodeToString() }
            assertEquals(capture, marker in body, "content is strictly opt-in")
            assertFalse("\\tat " in body, "serialized stacktrace leaked")
            assertFalse("exception.stacktrace" in body, "exception stacktrace leaked")
            listOf("fi-api-key-123", "fi-secret-456", "model-key-789").forEach { assertFalse(it in body, "credential in payload") }
        }
    }
}
