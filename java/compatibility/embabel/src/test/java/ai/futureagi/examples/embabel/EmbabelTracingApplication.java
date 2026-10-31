package ai.futureagi.examples.embabel;

import ai.futureagi.examples.embabel.WeatherAgent.WeatherQuestion;
import ai.futureagi.examples.embabel.WeatherAgent.WeatherReport;
import com.embabel.agent.api.invocation.AgentInvocation;
import com.embabel.agent.core.AgentPlatform;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.CommandLineRunner;
import org.springframework.boot.ExitCodeGenerator;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.annotation.Bean;

/**
 * Runs the weather agent once and exits. Closing the Spring context flushes the batch span
 * processor, so every span of the run is exported before the JVM stops.
 */
@SpringBootApplication
@org.springframework.context.annotation.Import(ai.traceai.nativeotel.spring.EmbabelTracingConfiguration.class)
public class EmbabelTracingApplication {

    private static final Logger log = LoggerFactory.getLogger(EmbabelTracingApplication.class);

    private static int exitCode = 0;

    public static void main(String[] args) {
        // SpringApplication.exit closes the context (and so flushes spans) before we exit.
        System.exit(SpringApplication.exit(SpringApplication.run(EmbabelTracingApplication.class, args)));
    }

    @Bean
    CommandLineRunner runWeatherAgentOnce(
            AgentPlatform agentPlatform,
            @Value("${demo.station-id:LIS-01}") String stationId,
            @Value("${demo.question:Is it a good day for a walk?}") String question) {
        return args -> {
            exitCode = 0;
            try {
                WeatherReport report = AgentInvocation.create(agentPlatform, WeatherReport.class)
                        .invoke(new WeatherQuestion(stationId, question));
                log.info("Agent finished: {}", report.summary());
            } catch (Exception e) {
                exitCode = 1;
                log.error("Agent run failed: {}", e.getClass().getSimpleName());
            }
        };
    }

    @Bean
    ExitCodeGenerator agentRunExitCode() {
        return () -> exitCode;
    }
}
