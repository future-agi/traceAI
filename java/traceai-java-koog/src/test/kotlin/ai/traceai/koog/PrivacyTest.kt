package ai.traceai.koog

import io.opentelemetry.api.common.AttributeKey.stringKey
import io.opentelemetry.api.common.Attributes
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class PrivacyTest {
    @Test
    fun `every native content field is masked despite verbose overrides`() {
        val builder = Attributes.builder()
        FutureAgiSpanExporter.CONTENT_KEYS.forEach { builder.put(it, "private-marker") }
        val transformed = FutureAgiSpanExporter.transformAttributes(builder.build(), false)
        FutureAgiSpanExporter.CONTENT_KEYS.forEach {
            assertEquals(FutureAgiSpanExporter.HIDDEN, transformed.get(stringKey(it)))
        }
        assertFalse(transformed.toString().contains("private-marker"))
    }

    @Test
    fun `escaped nested serialized stacks are stripped and valid content remains`() {
        val inner = JsonObject(mapOf("message" to JsonPrimitive("tool failed"), "stackTrace" to JsonPrimitive("private-stack")))
        val outer = JsonObject(mapOf("toolResult" to JsonPrimitive(inner.toString())))
        val result = FutureAgiSpanExporter.stripStackTraces(outer.toString())
        assertFalse("private-stack" in result)
        assertTrue("tool failed" in result)
        Json.parseToJsonElement(result)
    }

    @Test
    fun `malformed and deeply nested serialized error content fails closed`() {
        assertEquals(FutureAgiSpanExporter.HIDDEN, FutureAgiSpanExporter.stripStackTraces("{\"stackTrace\":\"private"))
        val deep = "[".repeat(70) + "{\"stackTrace\":\"private\"}" + "]".repeat(70)
        assertEquals(FutureAgiSpanExporter.HIDDEN, FutureAgiSpanExporter.stripStackTraces(deep))
    }

    @Test
    fun `raw exception attributes follow privacy without dropping error type`() {
        val attrs = Attributes.builder().put("exception.stacktrace", "private-stack")
            .put("exception.message", "private-message").put("error.type", "IllegalStateException").build()
        for (capture in listOf(false, true)) {
            val result = FutureAgiSpanExporter.transformAttributes(attrs, capture)
            assertEquals(null, result.get(stringKey("exception.stacktrace")))
            assertEquals(if (capture) "private-message" else null, result.get(stringKey("exception.message")))
            assertEquals("IllegalStateException", result.get(stringKey("error.type")))
        }
    }
}
