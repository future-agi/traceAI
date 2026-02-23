"""
E2E Tests for Instructor SDK Instrumentation

Tests Instructor instrumentation using Google's OpenAI-compatible endpoint.
Instructor wraps OpenAI client for structured extraction.
"""

import pytest
import time

from config import config, skip_if_no_google


@pytest.fixture(scope="module")
def instructor_client():
    """Create an instrumented Instructor client."""
    if not config.has_google():
        pytest.skip("GOOGLE_API_KEY not set")

    from fi_instrumentation import register
    try:
        from traceai_instructor import InstructorInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_instructor not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    InstructorInstrumentor().instrument(tracer_provider=tracer_provider)

    from openai import OpenAI
    import instructor

    openai_client = OpenAI(
        base_url=config.google_openai_base_url,
        api_key=config.google_api_key,
    )

    client = instructor.from_openai(openai_client)

    yield client

    InstructorInstrumentor().uninstrument()


@skip_if_no_google
class TestInstructorExtraction:
    """Test Instructor structured extraction."""

    def test_simple_extraction(self, instructor_client):
        """Test simple structured extraction."""
        from pydantic import BaseModel

        class UserInfo(BaseModel):
            name: str
            age: int

        response = instructor_client.chat.completions.create(
            model=config.google_model,
            messages=[
                {"role": "user", "content": "My name is John and I am 30 years old."}
            ],
            response_model=UserInfo,
        )

        assert isinstance(response, UserInfo)
        assert response.name == "John"
        assert response.age == 30

        time.sleep(2)
        print(f"Extracted: {response}")

    def test_list_extraction(self, instructor_client):
        """Test extraction of list of items."""
        from pydantic import BaseModel
        from typing import List

        class City(BaseModel):
            name: str
            country: str

        class CityList(BaseModel):
            cities: List[City]

        response = instructor_client.chat.completions.create(
            model=config.google_model,
            messages=[
                {"role": "user", "content": "Name 3 European capital cities with their countries."}
            ],
            response_model=CityList,
        )

        assert isinstance(response, CityList)
        assert len(response.cities) >= 3
        print(f"Cities: {[c.name for c in response.cities]}")

    def test_nested_extraction(self, instructor_client):
        """Test nested structured extraction."""
        from pydantic import BaseModel
        from typing import Optional

        class Address(BaseModel):
            city: str
            country: str

        class Person(BaseModel):
            name: str
            address: Optional[Address] = None

        response = instructor_client.chat.completions.create(
            model=config.google_model,
            messages=[
                {"role": "user", "content": "Alice lives in Paris, France."}
            ],
            response_model=Person,
        )

        assert isinstance(response, Person)
        assert response.name == "Alice"
        assert response.address is not None
        assert "Paris" in response.address.city
        print(f"Person: {response}")

    def test_enum_extraction(self, instructor_client):
        """Test extraction with enum field."""
        from pydantic import BaseModel
        from enum import Enum

        class Sentiment(str, Enum):
            POSITIVE = "positive"
            NEGATIVE = "negative"
            NEUTRAL = "neutral"

        class SentimentResult(BaseModel):
            text: str
            sentiment: Sentiment

        response = instructor_client.chat.completions.create(
            model=config.google_model,
            messages=[
                {"role": "user", "content": "I love this product! It's amazing!"}
            ],
            response_model=SentimentResult,
        )

        assert isinstance(response, SentimentResult)
        assert response.sentiment == Sentiment.POSITIVE
        print(f"Sentiment: {response.sentiment}")


@skip_if_no_google
class TestInstructorAsync:
    """Test async Instructor operations."""

    @pytest.mark.asyncio
    async def test_async_extraction(self):
        """Test async structured extraction."""
        if not config.has_google():
            pytest.skip("GOOGLE_API_KEY not set")

        from pydantic import BaseModel
        from openai import AsyncOpenAI
        import instructor

        async_openai = AsyncOpenAI(
            base_url=config.google_openai_base_url,
            api_key=config.google_api_key,
        )

        client = instructor.from_openai(async_openai)

        class CityInfo(BaseModel):
            name: str
            country: str

        response = await client.chat.completions.create(
            model=config.google_model,
            messages=[
                {"role": "user", "content": "What is the capital of Japan?"}
            ],
            response_model=CityInfo,
        )

        assert isinstance(response, CityInfo)
        assert "Tokyo" in response.name
