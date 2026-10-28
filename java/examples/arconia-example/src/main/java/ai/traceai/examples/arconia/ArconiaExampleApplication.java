package ai.traceai.examples.arconia;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * A plain Spring Boot web app. There is no Future AGI code in it: tracing comes from
 * arconia-opentelemetry-spring-boot-starter plus the arconia.otel.* properties in application.yaml.
 */
@SpringBootApplication
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
