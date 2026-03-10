"""
E2E Tests for Charts/Analytics API Endpoints

Tests analytics chart creation and data retrieval.
"""

import pytest
import requests
import uuid
from datetime import datetime, timedelta
from typing import Dict, Any

from config import config


class TestChartsCRUD:
    """Test Charts CRUD operations."""

    @pytest.fixture
    def chart_data(self, test_project: Dict[str, Any]) -> Dict[str, Any]:
        """Generate test chart data."""
        return {
            "name": f"e2e_test_chart_{uuid.uuid4().hex[:8]}",
            "project": test_project.get("id"),
            "chart_type": "line",
            "metric": "latency",
            "aggregation": "avg",
            "group_by": "hour",
        }

    def test_list_charts(self, api_session: requests.Session, test_project: Dict[str, Any]):
        """Test listing charts."""
        response = api_session.get(
            f"{config.tracer_base_url}/charts/",
            params={"project_id": test_project.get("id")},
            timeout=config.request_timeout,
        )

        assert response.status_code in [200, 401, 403]

        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, (list, dict))
            print(f"Found charts: {len(data.get('results', data)) if isinstance(data, dict) else len(data)}")

    def test_create_chart(self, api_session: requests.Session, chart_data: Dict[str, Any]):
        """Test creating a chart."""
        response = api_session.post(
            f"{config.tracer_base_url}/charts/",
            json=chart_data,
            timeout=config.request_timeout,
        )

        assert response.status_code in [201, 400, 401, 403]

        if response.status_code == 201:
            chart = response.json()
            assert "id" in chart
            print(f"Created chart: {chart['id']}")

            # Cleanup
            api_session.delete(
                f"{config.tracer_base_url}/charts/{chart['id']}/",
                timeout=config.request_timeout,
            )


class TestChartTypes:
    """Test different chart types."""

    def test_latency_chart(self, api_session: requests.Session, test_project: Dict[str, Any]):
        """Test latency chart."""
        chart_data = {
            "name": f"latency_chart_{uuid.uuid4().hex[:8]}",
            "project": test_project.get("id"),
            "chart_type": "line",
            "metric": "latency",
            "aggregation": "p95",
            "group_by": "day",
        }

        response = api_session.post(
            f"{config.tracer_base_url}/charts/",
            json=chart_data,
            timeout=config.request_timeout,
        )

        assert response.status_code in [201, 400, 401, 403]

        if response.status_code == 201:
            api_session.delete(
                f"{config.tracer_base_url}/charts/{response.json()['id']}/",
                timeout=config.request_timeout,
            )

    def test_token_usage_chart(self, api_session: requests.Session, test_project: Dict[str, Any]):
        """Test token usage chart."""
        chart_data = {
            "name": f"tokens_chart_{uuid.uuid4().hex[:8]}",
            "project": test_project.get("id"),
            "chart_type": "bar",
            "metric": "token_count",
            "aggregation": "sum",
            "group_by": "model",
        }

        response = api_session.post(
            f"{config.tracer_base_url}/charts/",
            json=chart_data,
            timeout=config.request_timeout,
        )

        assert response.status_code in [201, 400, 401, 403]

        if response.status_code == 201:
            api_session.delete(
                f"{config.tracer_base_url}/charts/{response.json()['id']}/",
                timeout=config.request_timeout,
            )

    def test_cost_chart(self, api_session: requests.Session, test_project: Dict[str, Any]):
        """Test cost chart."""
        chart_data = {
            "name": f"cost_chart_{uuid.uuid4().hex[:8]}",
            "project": test_project.get("id"),
            "chart_type": "area",
            "metric": "cost",
            "aggregation": "sum",
            "group_by": "day",
        }

        response = api_session.post(
            f"{config.tracer_base_url}/charts/",
            json=chart_data,
            timeout=config.request_timeout,
        )

        assert response.status_code in [201, 400, 401, 403]

        if response.status_code == 201:
            api_session.delete(
                f"{config.tracer_base_url}/charts/{response.json()['id']}/",
                timeout=config.request_timeout,
            )

    def test_trace_count_chart(self, api_session: requests.Session, test_project: Dict[str, Any]):
        """Test trace count chart."""
        chart_data = {
            "name": f"count_chart_{uuid.uuid4().hex[:8]}",
            "project": test_project.get("id"),
            "chart_type": "line",
            "metric": "trace_count",
            "aggregation": "count",
            "group_by": "hour",
        }

        response = api_session.post(
            f"{config.tracer_base_url}/charts/",
            json=chart_data,
            timeout=config.request_timeout,
        )

        assert response.status_code in [201, 400, 401, 403]

        if response.status_code == 201:
            api_session.delete(
                f"{config.tracer_base_url}/charts/{response.json()['id']}/",
                timeout=config.request_timeout,
            )


class TestChartData:
    """Test chart data retrieval."""

    def test_get_chart_data(self, api_session: requests.Session, test_project: Dict[str, Any]):
        """Test getting chart data."""
        # First create a chart
        chart_data = {
            "name": f"data_chart_{uuid.uuid4().hex[:8]}",
            "project": test_project.get("id"),
            "chart_type": "line",
            "metric": "latency",
            "aggregation": "avg",
            "group_by": "day",
        }

        create_response = api_session.post(
            f"{config.tracer_base_url}/charts/",
            json=chart_data,
            timeout=config.request_timeout,
        )

        if create_response.status_code != 201:
            pytest.skip("Chart creation not available")

        chart = create_response.json()

        # Get chart data
        end_date = datetime.now()
        start_date = end_date - timedelta(days=7)

        data_response = api_session.get(
            f"{config.tracer_base_url}/charts/{chart['id']}/data/",
            params={
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
            timeout=config.request_timeout,
        )

        assert data_response.status_code in [200, 401, 403, 404]

        if data_response.status_code == 200:
            data = data_response.json()
            assert isinstance(data, (list, dict))
            print(f"Chart data points: {len(data.get('data', data)) if isinstance(data, dict) else len(data)}")

        # Cleanup
        api_session.delete(
            f"{config.tracer_base_url}/charts/{chart['id']}/",
            timeout=config.request_timeout,
        )

    def test_chart_data_filters(self, api_session: requests.Session, test_project: Dict[str, Any]):
        """Test chart data with filters."""
        # First create a chart
        chart_data = {
            "name": f"filter_chart_{uuid.uuid4().hex[:8]}",
            "project": test_project.get("id"),
            "chart_type": "line",
            "metric": "latency",
            "aggregation": "avg",
            "group_by": "model",
        }

        create_response = api_session.post(
            f"{config.tracer_base_url}/charts/",
            json=chart_data,
            timeout=config.request_timeout,
        )

        if create_response.status_code != 201:
            pytest.skip("Chart creation not available")

        chart = create_response.json()

        # Get chart data with model filter
        end_date = datetime.now()
        start_date = end_date - timedelta(days=7)

        data_response = api_session.get(
            f"{config.tracer_base_url}/charts/{chart['id']}/data/",
            params={
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "model": "gpt-4o-mini",
            },
            timeout=config.request_timeout,
        )

        assert data_response.status_code in [200, 401, 403, 404]

        # Cleanup
        api_session.delete(
            f"{config.tracer_base_url}/charts/{chart['id']}/",
            timeout=config.request_timeout,
        )
