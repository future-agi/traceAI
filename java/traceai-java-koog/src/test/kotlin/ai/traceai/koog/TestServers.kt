package ai.traceai.koog

import com.sun.net.httpserver.HttpServer
import java.net.InetSocketAddress
import java.util.concurrent.CopyOnWriteArrayList

/**
 * Test stand-in for an OpenAI-compatible chat-completions endpoint (tests only, no network).
 * The first request gets a tool call to getWeather; any request that already carries a tool
 * result gets a final text answer that repeats [reference].
 */
class FakeOpenAI(private val reference: String) : AutoCloseable {
    private val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
    val requests = CopyOnWriteArrayList<String>()
    val baseUrl: String get() = "http://127.0.0.1:${server.address.port}"

    init {
        server.createContext("/v1/chat/completions") { ex ->
            val body = ex.requestBody.readBytes().decodeToString()
            requests += body
            val reply = if ("\"role\":\"tool\"" in body.replace(" ", "")) finalAnswer() else toolCall()
            val bytes = reply.encodeToByteArray()
            ex.responseHeaders.add("Content-Type", "application/json")
            ex.sendResponseHeaders(200, bytes.size.toLong())
            ex.responseBody.use { it.write(bytes) }
        }
        server.start()
    }

    private fun toolCall() = """
        {"id":"chatcmpl-1","object":"chat.completion","created":1,"model":"fake-model",
         "choices":[{"index":0,"message":{"role":"assistant","content":null,
           "tool_calls":[{"id":"call_1","type":"function","function":{"name":"getWeather","arguments":"{\"city\":\"Paris $reference\"}"}}]},
           "finish_reason":"tool_calls"}],
         "usage":{"prompt_tokens":101,"completion_tokens":11,"total_tokens":112}}
    """.trimIndent()

    private fun finalAnswer() = """
        {"id":"chatcmpl-2","object":"chat.completion","created":2,"model":"fake-model",
         "choices":[{"index":0,"message":{"role":"assistant","content":"Sunny in Paris, reference $reference."},"finish_reason":"stop"}],
         "usage":{"prompt_tokens":150,"completion_tokens":9,"total_tokens":159}}
    """.trimIndent()

    override fun close() = server.stop(0)
}

/** Test stand-in for an OTLP/HTTP receiver: keeps path, headers and raw body of every request. */
class RecordingOtlpReceiver : AutoCloseable {
    data class Request(val path: String, val headers: Map<String, String>, val body: ByteArray)

    private val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
    val requests = CopyOnWriteArrayList<Request>()
    val baseUrl: String get() = "http://127.0.0.1:${server.address.port}"

    init {
        server.createContext("/") { ex ->
            val headers = ex.requestHeaders.entries.associate { (k, v) -> k.lowercase() to v.joinToString(",") }
            requests += Request(ex.requestURI.path, headers, ex.requestBody.readBytes())
            ex.sendResponseHeaders(200, -1)
            ex.close()
        }
        server.start()
    }

    override fun close() = server.stop(0)
}

/** In-memory exporter that keeps its spans after shutdown (the SDK's InMemorySpanExporter clears them). */
class CollectingExporter : io.opentelemetry.sdk.trace.export.SpanExporter {
    val spans = CopyOnWriteArrayList<io.opentelemetry.sdk.trace.data.SpanData>()
    override fun export(spans: Collection<io.opentelemetry.sdk.trace.data.SpanData>): io.opentelemetry.sdk.common.CompletableResultCode {
        this.spans += spans
        return io.opentelemetry.sdk.common.CompletableResultCode.ofSuccess()
    }
    override fun flush() = io.opentelemetry.sdk.common.CompletableResultCode.ofSuccess()
    override fun shutdown() = io.opentelemetry.sdk.common.CompletableResultCode.ofSuccess()
}
