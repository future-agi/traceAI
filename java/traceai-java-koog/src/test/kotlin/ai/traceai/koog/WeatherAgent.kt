package ai.traceai.koog

import ai.koog.agents.core.agent.AIAgent
import ai.koog.agents.core.agent.GraphAIAgent
import ai.koog.agents.core.tools.ToolRegistry
import ai.koog.agents.core.tools.annotations.LLMDescription
import ai.koog.agents.core.tools.annotations.Tool
import ai.koog.agents.core.tools.reflect.ToolSet
import ai.koog.agents.features.opentelemetry.feature.OpenTelemetry
import ai.koog.prompt.executor.model.PromptExecutor
import ai.koog.prompt.llm.LLModel

/** One tool the model can call. [failWith] makes it throw, for the failure case. */
@LLMDescription("Weather tools")
class WeatherTools(private val reference: String, private val failWith: String? = null) : ToolSet {
    @Tool
    @LLMDescription("Returns the current weather for a city")
    fun getWeather(@LLMDescription("City name") city: String): String {
        failWith?.let { throw IllegalStateException(it) }
        return "Sunny, 21 C in $city (station $reference)"
    }
}

/** A plain Koog agent with one tool and the OpenTelemetry feature wired to Future AGI. */
fun weatherAgent(
    executor: PromptExecutor,
    model: LLModel,
    tools: WeatherTools,
    telemetry: FutureAgiTelemetry,
    systemPrompt: String = "You are a weather assistant. Always call getWeather before answering.",
): GraphAIAgent<String, String> = AIAgent(
    // A fixed id gives readable span names (create_agent weather-agent). Koog uses a random UUID otherwise.
    id = "weather-agent",
    promptExecutor = executor,
    llmModel = model,
    systemPrompt = systemPrompt,
    toolRegistry = ToolRegistry { tools(tools) },
    maxIterations = 10,
) {
    install(OpenTelemetry) {
        telemetry.configure(this)
    }
}

fun customModel(id: String): ai.koog.prompt.llm.LLModel = ai.koog.prompt.llm.LLModel(
    provider = ai.koog.prompt.llm.LLMProvider.OpenAI,
    id = id,
    capabilities = listOf(
        ai.koog.prompt.llm.LLMCapability.Completion,
        ai.koog.prompt.llm.LLMCapability.Tools,
        ai.koog.prompt.llm.LLMCapability.ToolChoice,
        ai.koog.prompt.llm.LLMCapability.OpenAIEndpoint.Completions,
    ),
    contextLength = 128_000,
)
