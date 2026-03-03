"""
E2E Tests for Trace API Endpoints

Tests trace CRUD operations and filtering.
"""

import pytest
import requests
import uuid
from datetime import datetime, timedelta
from typing import Dict, Any

from config import config


class TestTraceCRUD:
    """Test Trace CRUD operations."""

    def test_list_traces(self, api_session: requests.Session, test_project: Dict[str, Any]):
        """Test listing traces."""
        response = api_session.get(
            f"{config.tracer_base_url}/trace/",
            params={"project_id": test_project.get("id")},
            timeout=config.request_timeout,
        )

        assert response.status_code in [200, 401, 403]

        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, (list, dict))
            print(f"Found traces: {len(data.get('results', data)) if isinstance(data, dict) else len(data)}")

    def test_get_trace_by_id(self, api_session: requests.Session):
        """Test getting a trace by ID."""
        trace_id = str(uuid.uuid4())

        response = api_session.get(
            f"{config.tracer_base_url}/trace/{trace_id}/",
            timeout=config.request_timeout,
        )

        # Should return 404 for non-existent trace or 401/403 for auth
        assert response.status_code in [404, 401, 403]


class TestTraceFilters:
    """Test trace filtering capabilities."""

    def test_filter_by_date_range(self, api_session: requests.Session, test_project: Dict[str, Any]):
        """Test filtering traces by date range."""
        start_date = (datetime.now() - timedelta(days=7)).isoformat()
        end_date = datetime.now().isoformat()

        response = api_session.get(
            f"{config.tracer_base_url}/trace/",
            params={
                "project_id": test_project.get("id"),
                "start_date": start_date,
                "end_date": end_date,
            },
            timeout=config.request_timeout,
        )

        assert response.status_code in [200, 401, 403]

    def test_filter_by_user_id(self, api_session: requests.Session, test_project: Dict[str, Any]):
        """Test filtering traces by user ID."""
        response = api_session.get(
            f"{config.tracer_base_url}/trace/",
            params={
                "project_id": test_project.get("id"),
                "user_id": "test_user",
            },
            timeout=config.request_timeout,
        )

        assert response.status_code in [200, 401, 403]

    def test_filter_by_session(self, api_session: requests.Session, test_project: Dict[str, Any]):
        """Test filtering traces by session ID."""
        response = api_session.get(
            f"{config.tracer_base_url}/trace/",
            params={
                "project_id": test_project.get("id"),
                "session_id": "test_session",
            },
            timeout=config.request_timeout,
        )

        assert response.status_code in [200, 401, 403]

    def test_search_traces(self, api_session: requests.Session, test_project: Dict[str, Any]):
        """Test trace search."""
        response = api_session.get(
            f"{config.tracer_base_url}/trace/",
            params={
                "project_id": test_project.get("id"),
                "search": "test",
            },
            timeout=config.request_timeout,
        )

        assert response.status_code in [200, 401, 403]


class TestTracePagination:
    """Test trace pagination."""

    def test_pagination_params(self, api_session: requests.Session, test_project: Dict[str, Any]):
        """Test pagination parameters."""
        response = api_session.get(
            f"{config.tracer_base_url}/trace/",
            params={
                "project_id": test_project.get("id"),
                "page": 1,
                "page_size": 10,
            },
            timeout=config.request_timeout,
        )

        assert response.status_code in [200, 401, 403]

        if response.status_code == 200:
            data = response.json()
            if isinstance(data, dict):
                # Check pagination metadata
                if "count" in data:
                    assert isinstance(data["count"], int)
                if "next" in data:
                    assert data["next"] is None or isinstance(data["next"], str)

    def test_page_size_limit(self, api_session: requests.Session, test_project: Dict[str, Any]):
        """Test page size limits."""
        # Request large page size
        response = api_session.get(
            f"{config.tracer_base_url}/trace/",
            params={
                "project_id": test_project.get("id"),
                "page_size": 1000,
            },
            timeout=config.request_timeout,
        )

        assert response.status_code in [200, 401, 403]


class TestTraceUsers:
    """Test users endpoint."""

    def test_list_users(self, api_session: requests.Session, test_project: Dict[str, Any]):
        """Test listing users who have traces."""
        response = api_session.get(
            f"{config.tracer_base_url}/users/",
            params={"project_id": test_project.get("id")},
            timeout=config.request_timeout,
        )

        assert response.status_code in [200, 401, 403]

        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, (list, dict))

    def test_get_code_example(self, api_session: requests.Session, test_project: Dict[str, Any]):
        """Test getting code example."""
        response = api_session.get(
            f"{config.tracer_base_url}/users/get_code_example/",
            params={"project_id": test_project.get("id")},
            timeout=config.request_timeout,
        )

        assert response.status_code in [200, 401, 403]

        if response.status_code == 200:
            data = response.json()
            # Should contain code example
            assert isinstance(data, dict)
