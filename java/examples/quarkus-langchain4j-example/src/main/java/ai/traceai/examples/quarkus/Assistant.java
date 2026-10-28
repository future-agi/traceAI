package ai.traceai.examples.quarkus;

import dev.langchain4j.service.SystemMessage;
import dev.langchain4j.service.UserMessage;
import io.quarkiverse.langchain4j.RegisterAiService;

/**
 * The AI service. Quarkus LangChain4j generates the implementation at build time and, with quarkus-opentelemetry on
 * the classpath, opens a span named {@code langchain4j.aiservices.Assistant.chat} for every call.
 */
@RegisterAiService(tools = WeatherTools.class)
public interface Assistant {

    @SystemMessage("You are a weather assistant. Always call the currentWeather tool for the city in the question, then answer in one short sentence.")
    String chat(@UserMessage String question);
}
