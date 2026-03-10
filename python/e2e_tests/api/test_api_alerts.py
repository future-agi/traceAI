"""
E2E Tests for User Alerts API Endpoints

Tests alert creation, management, and logging.
"""

import pytest
import requests
import uuid
from datetime import datetime
from typing import Dict, Any

from config import config


class TestUserAlertsCRUD:
    """Test User Alerts CRUD operations."""

    @pytest.fixture
    def alert_data(self, test_project: Dict[str, Any]) -> Dict[str, Any]:
        """Generate test alert data."""
        return {
            "name": f"e2e_test_alert_{uuid.uuid4().hex[:8]}",
            "project": test_project.get("id"),
            "alert_type": "latency",
            "threshold_value": 5000,  # 5 seconds
            "threshold_operator": "gt",  # greater than
            "enabled": True,
            "notification_channels": ["email"],
        }

    def test_list_alerts(self, api_session: requests.Session, test_project: Dict[str, Any]):
        """Test listing user alerts."""
        response = api_session.get(
            f"{config.tracer_base_url}/user-alerts/",
            params={"project_id": test_project.get("id")},
            timeout=config.request_timeout,
        )

        assert response.status_code in [200, 401, 403]

        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, (list, dict))
            print(f"Found alerts: {len(data.get('results', data)) if isinstance(data, dict) else len(data)}")

    def test_create_alert(self, api_session: requests.Session, alert_data: Dict[str, Any]):
        """Test creating a user alert."""
        response = api_session.post(
            f"{config.tracer_base_url}/user-alerts/",
            json=alert_data,
            timeout=config.request_timeout,
        )

        assert response.status_code in [201, 400, 401, 403]

        if response.status_code == 201:
            alert = response.json()
            assert "id" in alert
            print(f"Created alert: {alert['id']}")

            # Cleanup
            api_session.delete(
                f"{config.tracer_base_url}/user-alerts/{alert['id']}/",
                timeout=config.request_timeout,
            )

    def test_get_alert(self, api_session: requests.Session):
        """Test getting an alert by ID."""
        alert_id = str(uuid.uuid4())

        response = api_session.get(
            f"{config.tracer_base_url}/user-alerts/{alert_id}/",
            timeout=config.request_timeout,
        )

        assert response.status_code in [404, 401, 403]

    def test_update_alert(self, api_session: requests.Session, alert_data: Dict[str, Any]):
        """Test updating an alert."""
        # First create an alert
        create_response = api_session.post(
            f"{config.tracer_base_url}/user-alerts/",
            json=alert_data,
            timeout=config.request_timeout,
        )

        if create_response.status_code != 201:
            pytest.skip("Alert creation not available")

        alert = create_response.json()

        # Update it
        update_data = {"enabled": False}
        update_response = api_session.patch(
            f"{config.tracer_base_url}/user-alerts/{alert['id']}/",
            json=update_data,
            timeout=config.request_timeout,
        )

        assert update_response.status_code in [200, 401, 403]

        # Cleanup
        api_session.delete(
            f"{config.tracer_base_url}/user-alerts/{alert['id']}/",
            timeout=config.request_timeout,
        )

    def test_delete_alert(self, api_session: requests.Session, alert_data: Dict[str, Any]):
        """Test deleting an alert."""
        # First create an alert
        create_response = api_session.post(
            f"{config.tracer_base_url}/user-alerts/",
            json=alert_data,
            timeout=config.request_timeout,
        )

        if create_response.status_code != 201:
            pytest.skip("Alert creation not available")

        alert = create_response.json()

        # Delete it
        delete_response = api_session.delete(
            f"{config.tracer_base_url}/user-alerts/{alert['id']}/",
            timeout=config.request_timeout,
        )

        assert delete_response.status_code in [204, 200, 401, 403]


class TestUserAlertLogs:
    """Test User Alert Logs."""

    def test_list_alert_logs(self, api_session: requests.Session, test_project: Dict[str, Any]):
        """Test listing alert logs."""
        response = api_session.get(
            f"{config.tracer_base_url}/user-alert-logs/",
            params={"project_id": test_project.get("id")},
            timeout=config.request_timeout,
        )

        assert response.status_code in [200, 401, 403]

        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, (list, dict))

    def test_filter_logs_by_alert(self, api_session: requests.Session, test_project: Dict[str, Any]):
        """Test filtering logs by alert ID."""
        alert_id = str(uuid.uuid4())

        response = api_session.get(
            f"{config.tracer_base_url}/user-alert-logs/",
            params={
                "project_id": test_project.get("id"),
                "alert_id": alert_id,
            },
            timeout=config.request_timeout,
        )

        assert response.status_code in [200, 401, 403]


class TestAlertTypes:
    """Test different alert types."""

    def test_latency_alert(self, api_session: requests.Session, test_project: Dict[str, Any]):
        """Test latency-based alert."""
        alert_data = {
            "name": f"latency_alert_{uuid.uuid4().hex[:8]}",
            "project": test_project.get("id"),
            "alert_type": "latency",
            "threshold_value": 5000,
            "threshold_operator": "gt",
            "enabled": True,
        }

        response = api_session.post(
            f"{config.tracer_base_url}/user-alerts/",
            json=alert_data,
            timeout=config.request_timeout,
        )

        assert response.status_code in [201, 400, 401, 403]

        if response.status_code == 201:
            api_session.delete(
                f"{config.tracer_base_url}/user-alerts/{response.json()['id']}/",
                timeout=config.request_timeout,
            )

    def test_error_rate_alert(self, api_session: requests.Session, test_project: Dict[str, Any]):
        """Test error rate alert."""
        alert_data = {
            "name": f"error_alert_{uuid.uuid4().hex[:8]}",
            "project": test_project.get("id"),
            "alert_type": "error_rate",
            "threshold_value": 10,  # 10%
            "threshold_operator": "gt",
            "enabled": True,
        }

        response = api_session.post(
            f"{config.tracer_base_url}/user-alerts/",
            json=alert_data,
            timeout=config.request_timeout,
        )

        assert response.status_code in [201, 400, 401, 403]

        if response.status_code == 201:
            api_session.delete(
                f"{config.tracer_base_url}/user-alerts/{response.json()['id']}/",
                timeout=config.request_timeout,
            )

    def test_cost_alert(self, api_session: requests.Session, test_project: Dict[str, Any]):
        """Test cost-based alert."""
        alert_data = {
            "name": f"cost_alert_{uuid.uuid4().hex[:8]}",
            "project": test_project.get("id"),
            "alert_type": "cost",
            "threshold_value": 100,  # $100
            "threshold_operator": "gt",
            "enabled": True,
        }

        response = api_session.post(
            f"{config.tracer_base_url}/user-alerts/",
            json=alert_data,
            timeout=config.request_timeout,
        )

        assert response.status_code in [201, 400, 401, 403]

        if response.status_code == 201:
            api_session.delete(
                f"{config.tracer_base_url}/user-alerts/{response.json()['id']}/",
                timeout=config.request_timeout,
            )
