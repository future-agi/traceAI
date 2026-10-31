package ai.traceai.koog

import ai.koog.agents.features.opentelemetry.feature.OpenTelemetryConfig
import io.opentelemetry.sdk.common.CompletableResultCode
import io.opentelemetry.sdk.trace.data.SpanData
import io.opentelemetry.sdk.trace.export.SpanExporter
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.yield
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class LifecycleTest {
    private class PendingExporter : SpanExporter {
        val entered = CountDownLatch(1)
        val pending = CompletableResultCode()
        @Volatile var shutdownCalls = 0
        override fun export(spans: Collection<SpanData>): CompletableResultCode {
            entered.countDown()
            return pending
        }
        override fun flush() = pending
        override fun shutdown(): CompletableResultCode {
            shutdownCalls++
            return pending
        }
    }

    @Test
    fun `shutdown cannot overtake an export and closes the delegate exactly once`() {
        val delegate = PendingExporter()
        val wrapper = FutureAgiSpanExporter(delegate, false)
        val pool = Executors.newFixedThreadPool(2)
        try {
            val exported = pool.submit<CompletableResultCode> { wrapper.export(emptyList()) }
            assertTrue(delegate.entered.await(5, TimeUnit.SECONDS))
            val closing = CountDownLatch(1)
            val shutdown = pool.submit<CompletableResultCode> { closing.countDown(); wrapper.shutdown() }
            assertTrue(closing.await(5, TimeUnit.SECONDS))
            assertEquals(0, delegate.shutdownCalls)
            assertFalse(exported.isDone)
            delegate.pending.succeed()
            assertTrue(exported.get(5, TimeUnit.SECONDS).isSuccess)
            assertTrue(shutdown.get(5, TimeUnit.SECONDS).isSuccess)
            assertTrue(wrapper.shutdown().isSuccess)
            assertEquals(1, delegate.shutdownCalls)
            assertFalse(wrapper.export(emptyList()).isSuccess)
        } finally {
            delegate.pending.succeed()
            pool.shutdownNow()
        }
    }

    @Test
    fun `flush and shutdown await asynchronous completion`() {
        for (closing in listOf(false, true)) {
            val delegate = PendingExporter()
            val wrapper = FutureAgiSpanExporter(delegate, false)
            val pool = Executors.newSingleThreadExecutor()
            try {
                val started = CountDownLatch(1)
                val future = pool.submit<CompletableResultCode> {
                    started.countDown()
                    if (closing) wrapper.shutdown() else wrapper.flush()
                }
                assertTrue(started.await(5, TimeUnit.SECONDS))
                assertFalse(future.isDone)
                delegate.pending.succeed()
                assertTrue(future.get(5, TimeUnit.SECONDS).isSuccess)
            } finally {
                delegate.pending.succeed()
                pool.shutdownNow()
            }
        }
    }

    @Test
    fun `export failures timeouts and exceptions never become success`() {
        val pending = PendingExporter()
        val wrapper = FutureAgiSpanExporter(pending, false, exportTimeoutMillis = 10)
        assertFalse(wrapper.export(emptyList()).isSuccess)
        pending.pending.fail()
        assertFalse(wrapper.export(emptyList()).isSuccess)
        assertFalse(wrapper.shutdown().isSuccess)
        assertFalse(wrapper.shutdown().isSuccess)
        assertEquals(1, pending.shutdownCalls)
        val throwing = object : SpanExporter {
            override fun export(spans: Collection<SpanData>): CompletableResultCode = throw IllegalStateException("synthetic")
            override fun flush() = CompletableResultCode.ofFailure()
            override fun shutdown() = CompletableResultCode.ofFailure()
        }
        assertFalse(FutureAgiSpanExporter(throwing, false).export(emptyList()).isSuccess)
        assertFailsWith<IllegalArgumentException> { FutureAgiSpanExporter(pending, false, 0) }
    }

    @Test
    fun `shutdown survives cancellation is idempotent and prevents reuse`() = runBlocking {
        val delegate = PendingExporter().apply { pending.succeed() }
        val telemetry = FutureAgiTelemetry(FutureAgiTelemetry.Config("http://localhost", "k", "s", "p"), delegate)
        val worker = launch {
            try {
                cancel()
                yield()
            } finally {
                telemetry.shutdown()
            }
        }
        worker.join()
        telemetry.shutdown()
        assertEquals(1, delegate.shutdownCalls)
        assertFailsWith<IllegalStateException> { telemetry.configure(OpenTelemetryConfig()) }
        Unit // JUnit requires a void-returning test, not the exception returned by assertFailsWith.
    }

    @Test
    fun `configuration rejects endpoint credentials without printing them`() {
        for (endpoint in listOf("ftp://localhost", "https://user:secret@example.com", "https://example.com?token=secret", "not a url")) {
            val error = assertFailsWith<IllegalArgumentException> {
                FutureAgiTelemetry.Config(endpoint, "k", "s", "p")
            }
            assertFalse(error.message.orEmpty().contains("secret"))
        }
    }
}
