"""
E2E Test: Structured Data Extraction Use Case

Simulates extracting structured data from unstructured text.
"""

import pytest
import os
import time
import json
from typing import Dict, Any, Optional
from dataclasses import dataclass

from config import config, skip_if_no_openai


@pytest.fixture(scope="module")
def setup_extraction():
    """Set up OpenAI with instrumentation for extraction."""
    if not config.has_openai():
        pytest.skip("OpenAI API key required")

    os.environ["OPENAI_API_KEY"] = config.openai_api_key

    from fi_instrumentation import register
    from traceai_openai import OpenAIInstrumentor

    tracer_provider = register(
        project_name="e2e_extraction_usecase",
        project_version_name="1.0.0",
        verbose=False,
    )

    OpenAIInstrumentor().instrument(tracer_provider=tracer_provider)

    from openai import OpenAI
    client = OpenAI()

    yield client

    OpenAIInstrumentor().uninstrument()


@skip_if_no_openai
class TestEntityExtraction:
    """Test entity extraction use cases."""

    def test_extract_person_info(self, setup_extraction):
        """Extract person information from text."""
        text = """
        John Smith is a 35-year-old software engineer from San Francisco.
        He can be reached at john.smith@email.com or (555) 123-4567.
        He has been working at TechCorp for 5 years.
        """

        response = setup_extraction.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "Extract structured information from text. Return valid JSON."
                },
                {
                    "role": "user",
                    "content": f"""Extract the following information from this text:
                    - Name
                    - Age
                    - Occupation
                    - Location
                    - Email
                    - Phone
                    - Company
                    - Years at company

                    Text: {text}

                    Return as JSON."""
                }
            ],
            max_tokens=300,
            response_format={"type": "json_object"},
        )

        result = response.choices[0].message.content
        print(f"Extracted: {result}")

        # Verify it's valid JSON
        data = json.loads(result)
        assert "name" in data.get("Name", data).lower() or "john" in str(data).lower()

        time.sleep(2)

    def test_extract_product_info(self, setup_extraction):
        """Extract product information from description."""
        text = """
        The new iPhone 15 Pro Max features a 6.7-inch Super Retina XDR display,
        A17 Pro chip, and starts at $1,199. It comes in Natural Titanium,
        Blue Titanium, White Titanium, and Black Titanium colors.
        Storage options include 256GB, 512GB, and 1TB.
        """

        response = setup_extraction.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "Extract product details. Return valid JSON."
                },
                {
                    "role": "user",
                    "content": f"""Extract product details:
                    - Product name
                    - Screen size
                    - Processor
                    - Starting price
                    - Available colors
                    - Storage options

                    Text: {text}

                    Return as JSON."""
                }
            ],
            max_tokens=300,
            response_format={"type": "json_object"},
        )

        result = response.choices[0].message.content
        data = json.loads(result)
        print(f"Product data: {json.dumps(data, indent=2)}")

        time.sleep(2)

    def test_extract_events(self, setup_extraction):
        """Extract event information from calendar text."""
        text = """
        Team meeting scheduled for Monday, January 15th at 2:00 PM in Conference Room A.
        The meeting will last approximately 1 hour and will cover Q1 planning.
        Attendees: Alice, Bob, and Charlie.
        Please bring your project updates.
        """

        response = setup_extraction.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": f"""Extract event details as JSON:
                    - Event type
                    - Date
                    - Time
                    - Duration
                    - Location
                    - Topic
                    - Attendees

                    Text: {text}"""
                }
            ],
            max_tokens=300,
            response_format={"type": "json_object"},
        )

        result = response.choices[0].message.content
        data = json.loads(result)
        print(f"Event data: {json.dumps(data, indent=2)}")

        time.sleep(2)


@skip_if_no_openai
class TestSentimentExtraction:
    """Test sentiment and classification extraction."""

    def test_sentiment_analysis(self, setup_extraction):
        """Extract sentiment from customer reviews."""
        reviews = [
            "This product is amazing! Best purchase I've ever made.",
            "Terrible quality. Broke after one week. Would not recommend.",
            "It's okay. Does what it's supposed to do, nothing special.",
        ]

        results = []
        for review in reviews:
            response = setup_extraction.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": "Analyze sentiment. Return JSON with sentiment (positive/negative/neutral) and confidence (0-1)."
                    },
                    {
                        "role": "user",
                        "content": f"Analyze: {review}"
                    }
                ],
                max_tokens=100,
                response_format={"type": "json_object"},
            )

            data = json.loads(response.choices[0].message.content)
            results.append(data)
            print(f"Review: {review[:50]}... -> {data}")

        assert len(results) == 3
        time.sleep(2)

    def test_intent_classification(self, setup_extraction):
        """Classify user intent from messages."""
        messages = [
            "I want to cancel my subscription",
            "How do I change my password?",
            "Your service is great, thanks!",
            "I'm having trouble logging in",
        ]

        for message in messages:
            response = setup_extraction.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": """Classify intent. Return JSON with:
                        - intent: one of [billing, technical_support, feedback, account_access, other]
                        - confidence: 0-1"""
                    },
                    {
                        "role": "user",
                        "content": message
                    }
                ],
                max_tokens=100,
                response_format={"type": "json_object"},
            )

            data = json.loads(response.choices[0].message.content)
            print(f"Message: {message} -> Intent: {data}")

        time.sleep(2)


@skip_if_no_openai
class TestBatchExtraction:
    """Test batch extraction scenarios."""

    def test_extract_multiple_emails(self, setup_extraction):
        """Extract information from multiple emails."""
        emails = """
        From: alice@company.com
        Subject: Project Update
        Date: Jan 10, 2025
        Hi team, the project is on track for Q1 delivery.

        ---

        From: bob@vendor.com
        Subject: Invoice #12345
        Date: Jan 11, 2025
        Please find attached invoice for $5,000 due Feb 1.

        ---

        From: hr@company.com
        Subject: Holiday Schedule
        Date: Jan 12, 2025
        Office closed Jan 20 for MLK Day.
        """

        response = setup_extraction.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": f"""Extract details from each email as JSON array:
                    - sender
                    - subject
                    - date
                    - summary (one sentence)
                    - category (project/billing/hr/other)

                    Emails:
                    {emails}"""
                }
            ],
            max_tokens=500,
            response_format={"type": "json_object"},
        )

        data = json.loads(response.choices[0].message.content)
        print(f"Extracted {len(data.get('emails', data))} emails")

        time.sleep(2)
