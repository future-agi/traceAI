package ai.futureagi.examples.embabel;

import com.embabel.agent.api.annotation.AchievesGoal;
import com.embabel.agent.api.annotation.Action;
import com.embabel.agent.api.annotation.Agent;
import com.embabel.agent.api.common.OperationContext;

/**
 * One Embabel agent with one action that calls one tool. Nothing in here knows about tracing:
 * the observability starter instruments the run, and application.yml sends it to Future AGI.
 */
@Agent(description = "Answers a weather question for one weather station using a weather tool")
public class WeatherAgent {

    /** What the caller asks. */
    public record WeatherQuestion(String stationId, String text) {
    }

    /** What the agent returns. */
    public record WeatherReport(String stationId, String summary) {
    }

    @AchievesGoal(description = "A one-sentence weather report was written for the user")
    @Action
    public WeatherReport report(WeatherQuestion question, OperationContext context) {
        return context.ai()
                .withDefaultLlm()
                .withToolObject(new WeatherTools())
                .createObject("""
                        Call the currentWeather tool exactly once with stationId "%s".
                        Then answer this question in one sentence using the tool result: %s
                        """.formatted(question.stationId(), question.text()),
                        WeatherReport.class);
    }
}
