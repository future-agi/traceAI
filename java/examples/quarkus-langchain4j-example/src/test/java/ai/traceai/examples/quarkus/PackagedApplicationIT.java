package ai.traceai.examples.quarkus;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;

/** Runs the production JVM artifact against localhost, including its normal SDK shutdown. */
class PackagedApplicationIT {

    @Test
    void commandFlushesNativeSpansOnNormalExit() throws Exception {
        StandIns resources = new StandIns();
        Process process = null;
        Path output = Path.of("target", "packaged-application.log");
        try {
            ProcessBuilder builder = new ProcessBuilder(
                    Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                    "-Djava.net.preferIPv4Stack=true", "-jar", "target/quarkus-app/quarkus-run.jar",
                    "What is the weather in PRIVATE-CLI-MARKER?");
            builder.environment().putAll(resources.start());
            // A batch should remain pending until normal shutdown, not the periodic export timer.
            builder.environment().put("QUARKUS_OTEL_BSP_SCHEDULE_DELAY", "1h");
            builder.redirectErrorStream(true).redirectOutput(output.toFile());
            process = builder.start();
            assertTrue(process.waitFor(45, TimeUnit.SECONDS), "command must exit normally");
            assertEquals(0, process.exitValue(), () -> {
                try { return Files.readString(output); } catch (Exception e) { return e.toString(); }
            });
            var spans = Exported.awaitTrace();
            assertEquals(6, spans.size(), "one AI service, two model, one tool and two HTTP spans");
            assertTrue(spans.stream().allMatch(s -> StandIns.PROJECT.equals(s.resource().get("project_name"))));
            assertTrue(spans.stream().noneMatch(s -> s.resource().containsKey("host.name")));
            String bytes = Exported.everythingReceived();
            assertFalse(bytes.contains("PRIVATE-CLI-MARKER"));
            assertFalse(bytes.contains(StandIns.PROVIDER_KEY));
            assertTrue(StandIns.collector.posts().stream().allMatch(p -> p.path().equals("/tracer/v1/traces")));
        } finally {
            if (process != null && process.isAlive()) {
                process.destroyForcibly();
                process.waitFor(10, TimeUnit.SECONDS);
            }
            resources.stop();
        }
    }
}
