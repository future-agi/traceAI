package ai.traceai.examples.quarkus;

import dev.langchain4j.agent.tool.Tool;
import jakarta.enterprise.context.ApplicationScoped;

/** One deterministic tool, so a single question produces a model call, a tool call and a second model call. */
@ApplicationScoped
public class WeatherTools {

    @Tool("Returns the current weather for a city")
    public String currentWeather(String city) {
        if (city == null || city.isBlank() || city.startsWith("Atlantis")) {
            // The message carries user input on purpose: the tests check it never leaves the JVM by default.
            throw new IllegalArgumentException("No weather station for " + city);
        }
        return "Sunny, 21 C in " + city;
    }
}
