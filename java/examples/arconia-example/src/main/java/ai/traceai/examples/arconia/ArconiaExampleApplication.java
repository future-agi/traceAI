package ai.traceai.examples.arconia;

import ai.traceai.privacy.spring.ErrorDetailRedactionConfiguration;
import org.springframework.context.annotation.Import;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * Native Arconia tracing, with the shared Java core privacy policy imported before startup.
 * Arconia owns the provider, exporter and batching; do not initialize a second SDK.
 */
@SpringBootApplication
@Import(ErrorDetailRedactionConfiguration.class)
@RestController
public class ArconiaExampleApplication {

    public static void main(String[] args) {
        SpringApplication.run(ArconiaExampleApplication.class, args);
    }

    @GetMapping("/hello")
    public String hello(@RequestParam(defaultValue = "world") String name) {
        return "hello " + name;
    }

    /** Throws, so the trace contains an ERROR server span. */
    @GetMapping("/fail")
    public String fail(@RequestParam(defaultValue = "boom") String reason) {
        throw new IllegalStateException("failed: " + reason);
    }
}
