package ai.futureagi.examples.embabel;

import com.embabel.agent.api.annotation.LlmTool;

/**
 * A deterministic in-process tool. A station id starting with "offline-" makes the tool throw,
 * which is the failure case the example uses to show an ERROR span.
 */
public class WeatherTools {

    @LlmTool(description = "Current weather at a weather station, by station id")
    public String currentWeather(String stationId) {
        if (stationId.startsWith("offline-")) {
            throw new IllegalStateException("weather station " + stationId + " is offline");
        }
        return "Station " + stationId + ": 21 C, clear sky, wind 8 km/h";
    }
}
