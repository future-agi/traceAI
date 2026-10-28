"""Scenario constants and plugins shared by the contract app and the tests."""

from __future__ import annotations

from typing import Annotated

SECRET_PROMPT = "SECRET-PROMPT-7f3a: what is the weather in the capital?"
TOOL_SECRET_CITY = "SECRET-CITY-91c2"
PROJECT_NAME = "semantic-kernel-contract"
SESSION = "contract-session-1"
API_KEY = "placeholder-api-key"
SECRET_KEY = "placeholder-secret-key"


def weather_plugin():
    from semantic_kernel.functions import kernel_function

    class Weather:
        @kernel_function(name="get_weather", description="Weather for a city")
        def get_weather(self, city: Annotated[str, "City name"]) -> str:
            return "sunny in " + city

    return Weather()


def broken_plugin():
    from semantic_kernel.functions import kernel_function

    class Broken:
        @kernel_function(name="explode", description="Always fails")
        def explode(self, city: Annotated[str, "City name"]) -> str:
            raise RuntimeError("tool failed on purpose")

    return Broken()
