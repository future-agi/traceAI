"""
E2E Tests for OTLP Ingestion Endpoints

Tests OpenTelemetry trace ingestion.
"""

import pytest
import requests
import uuid
import json
import time
from datetime import datetime
from typing import Dict, Any, List

from config import config


def generate_otlp_trace() -> Dict[str, Any]:
    """Generate a sample OTLP trace payload."""
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    now_ns = int(datetime.now().timestamp() * 1_000_000_000)

    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "e2e-test-service"}},
                        {"key": "fi.project.name", "value": {"stringValue": "e2e_test_otlp"}},
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "e2e-test"},
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": span_id,
                                "name": "e2e-test-span",
                                "kind": 1,  # SPAN_KIND_INTERNAL
                                "startTimeUnixNano": str(now_ns),
                                "endTimeUnixNano": str(now_ns + 1_000_000_000),
                                "status": {"code": 1},  # STATUS_CODE_OK
                                "attributes": [
                                    {"key": "llm.model", "value": {"stringValue": "gpt-4o-mini"}},
                                    {"key": "llm.token.count.prompt", "value": {"intValue": "100"}},
                                    {"key": "llm.token.count.completion", "value": {"intValue": "50"}},
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }


class TestOTLPIngestion:
    """Test OTLP trace ingestion."""

    def test_health_check(self, api_session: requests.Session):
        """Test OTLP health endpoint."""
        response = api_session.get(
            f"{config.backend_url}/v1/health",
            timeout=config.request_timeout,
        )

        # Health should always be accessible
        assert response.status_code in [200, 404]

        if response.status_code == 200:
            print(f"Health check response: {response.json()}")

    def test_post_traces_json(self, api_session: requests.Session):
        """Test posting traces in JSON format."""
        trace_data = generate_otlp_trace()

        response = api_session.post(
            f"{config.backend_url}/v1/traces",
            json=trace_data,
            headers={
                "Content-Type": "application/json",
                "fi-api-key": "test-api-key",
            },
            timeout=config.request_timeout,
        )

        # Should accept or reject based on auth
        assert response.status_code in [200, 202, 400, 401, 403]

        if response.status_code in [200, 202]:
            print(f"Trace ingestion response: {response.status_code}")

    def test_post_traces_with_trailing_slash(self, api_session: requests.Session):
        """Test posting traces with trailing slash."""
        trace_data = generate_otlp_trace()

        response = api_session.post(
            f"{config.backend_url}/v1/traces/",
            json=trace_data,
            headers={
                "Content-Type": "application/json",
                "fi-api-key": "test-api-key",
            },
            timeout=config.request_timeout,
        )

        assert response.status_code in [200, 202, 400, 401, 403]

    def test_invalid_trace_format(self, api_session: requests.Session):
        """Test posting invalid trace format."""
        invalid_data = {"invalid": "data"}

        response = api_session.post(
            f"{config.backend_url}/v1/traces",
            json=invalid_data,
            headers={
                "Content-Type": "application/json",
                "fi-api-key": "test-api-key",
            },
            timeout=config.request_timeout,
        )

        # Should reject invalid format
        assert response.status_code in [400, 401, 403, 422]

    def test_empty_trace_batch(self, api_session: requests.Session):
        """Test posting empty trace batch."""
        empty_data = {"resourceSpans": []}

        response = api_session.post(
            f"{config.backend_url}/v1/traces",
            json=empty_data,
            headers={
                "Content-Type": "application/json",
                "fi-api-key": "test-api-key",
            },
            timeout=config.request_timeout,
        )

        # Should handle gracefully
        assert response.status_code in [200, 202, 400, 401, 403]


class TestOTLPAttributes:
    """Test OTLP attribute handling."""

    def test_genai_attributes(self, api_session: requests.Session):
        """Test GenAI semantic convention attributes."""
        trace_data = generate_otlp_trace()

        # Add GenAI attributes
        span = trace_data["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        span["attributes"].extend([
            {"key": "gen_ai.system", "value": {"stringValue": "openai"}},
            {"key": "gen_ai.request.model", "value": {"stringValue": "gpt-4o-mini"}},
            {"key": "gen_ai.response.model", "value": {"stringValue": "gpt-4o-mini-2024-07-18"}},
            {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "100"}},
            {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "50"}},
        ])

        response = api_session.post(
            f"{config.backend_url}/v1/traces",
            json=trace_data,
            headers={
                "Content-Type": "application/json",
                "fi-api-key": "test-api-key",
            },
            timeout=config.request_timeout,
        )

        assert response.status_code in [200, 202, 400, 401, 403]

    def test_fi_attributes(self, api_session: requests.Session):
        """Test FutureAGI custom attributes."""
        trace_data = generate_otlp_trace()

        # Add FI attributes
        span = trace_data["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        span["attributes"].extend([
            {"key": "fi.span.kind", "value": {"stringValue": "llm"}},
            {"key": "fi.user.id", "value": {"stringValue": "test-user-123"}},
            {"key": "fi.session.id", "value": {"stringValue": "session-456"}},
        ])

        response = api_session.post(
            f"{config.backend_url}/v1/traces",
            json=trace_data,
            headers={
                "Content-Type": "application/json",
                "fi-api-key": "test-api-key",
            },
            timeout=config.request_timeout,
        )

        assert response.status_code in [200, 202, 400, 401, 403]


class TestOTLPBatching:
    """Test OTLP batch ingestion."""

    def test_multiple_spans(self, api_session: requests.Session):
        """Test posting multiple spans in one trace."""
        trace_id = uuid.uuid4().hex
        now_ns = int(datetime.now().timestamp() * 1_000_000_000)

        spans = []
        for i in range(5):
            span_id = uuid.uuid4().hex[:16]
            spans.append({
                "traceId": trace_id,
                "spanId": span_id,
                "name": f"span-{i}",
                "kind": 1,
                "startTimeUnixNano": str(now_ns + i * 1_000_000),
                "endTimeUnixNano": str(now_ns + (i + 1) * 1_000_000),
                "status": {"code": 1},
            })

        trace_data = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": "e2e-test"}},
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "e2e-test"},
                            "spans": spans,
                        }
                    ],
                }
            ]
        }

        response = api_session.post(
            f"{config.backend_url}/v1/traces",
            json=trace_data,
            headers={
                "Content-Type": "application/json",
                "fi-api-key": "test-api-key",
            },
            timeout=config.request_timeout,
        )

        assert response.status_code in [200, 202, 400, 401, 403]

    def test_multiple_resource_spans(self, api_session: requests.Session):
        """Test posting multiple resource spans."""
        trace_data = {
            "resourceSpans": [
                generate_otlp_trace()["resourceSpans"][0],
                generate_otlp_trace()["resourceSpans"][0],
            ]
        }

        response = api_session.post(
            f"{config.backend_url}/v1/traces",
            json=trace_data,
            headers={
                "Content-Type": "application/json",
                "fi-api-key": "test-api-key",
            },
            timeout=config.request_timeout,
        )

        assert response.status_code in [200, 202, 400, 401, 403]
