package ai.traceai.koog

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class ConfigTest {
    private fun env(vararg kv: Pair<String, String>): (String) -> String? = mapOf(*kv)::get

    @Test
    fun `reads env, defaults to the cloud endpoint, content off`() {
        val c = FutureAgiTelemetry.Config.fromEnv(env("FI_API_KEY" to "k", "FI_SECRET_KEY" to "s", "FI_PROJECT_NAME" to "p"))
        assertEquals("https://api.futureagi.com/tracer/v1/traces", c.tracesEndpoint)
        assertFalse(c.captureContent)
        assertEquals("koog-agent", c.serviceName)
    }

    @Test
    fun `self-hosted base url and opt-in`() {
        val c = FutureAgiTelemetry.Config.fromEnv(
            env("FI_BASE_URL" to "http://localhost:14338/", "FI_API_KEY" to "k", "FI_SECRET_KEY" to "s",
                "FI_PROJECT_NAME" to "p", "FI_KOOG_CAPTURE_CONTENT" to "TRUE"),
        )
        assertEquals("http://localhost:14338/tracer/v1/traces", c.tracesEndpoint)
        assertTrue(c.captureContent)
    }

    @Test
    fun `both keys and the project are required`() {
        assertFailsWith<IllegalArgumentException> { FutureAgiTelemetry.Config.fromEnv(env("FI_API_KEY" to "k", "FI_PROJECT_NAME" to "p")) }
        assertFailsWith<IllegalArgumentException> { FutureAgiTelemetry.Config.fromEnv(env("FI_SECRET_KEY" to "s", "FI_PROJECT_NAME" to "p")) }
        assertFailsWith<IllegalArgumentException> { FutureAgiTelemetry.Config.fromEnv(env("FI_API_KEY" to "k", "FI_SECRET_KEY" to "s")) }
    }

    @Test
    fun `toString never prints the keys`() {
        val c = FutureAgiTelemetry.Config("http://x", "API-KEY-VALUE", "SECRET-VALUE", "p")
        assertFalse("API-KEY-VALUE" in c.toString() || "SECRET-VALUE" in c.toString())
    }
}
