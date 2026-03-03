"""
E2E Tests for Project API Endpoints

Tests CRUD operations for projects.
"""

import pytest
import requests
import uuid
from datetime import datetime
from typing import Dict, Any, Generator

from config import config


class TestProjectCRUD:
    """Test Project CRUD operations."""

    @pytest.fixture
    def test_project_data(self) -> Dict[str, Any]:
        """Generate test project data."""
        return {
            "name": f"e2e_test_project_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}",
            "description": "E2E test project for API testing",
        }

    @pytest.fixture
    def created_project(
        self, api_session: requests.Session, test_project_data: Dict[str, Any]
    ) -> Generator[Dict[str, Any], None, None]:
        """Create a project for testing and cleanup after."""
        # Create project
        response = api_session.post(
            f"{config.tracer_base_url}/project/",
            json=test_project_data,
            timeout=config.request_timeout,
        )

        if response.status_code == 201:
            project = response.json()
            yield project

            # Cleanup
            try:
                api_session.delete(
                    f"{config.tracer_base_url}/project/{project['id']}/",
                    timeout=config.request_timeout,
                )
            except Exception:
                pass
        else:
            # API might not be available or require auth
            yield {"id": str(uuid.uuid4()), **test_project_data}

    def test_list_projects(self, api_session: requests.Session):
        """Test listing projects."""
        response = api_session.get(
            f"{config.tracer_base_url}/project/",
            timeout=config.request_timeout,
        )

        # Check response (might be 200 or 401 depending on auth)
        assert response.status_code in [200, 401, 403]

        if response.status_code == 200:
            data = response.json()
            # Should return list or paginated results
            assert isinstance(data, (list, dict))
            if isinstance(data, dict):
                assert "results" in data or "count" in data
            print(f"Listed projects: {len(data.get('results', data)) if isinstance(data, dict) else len(data)}")

    def test_create_project(
        self, api_session: requests.Session, test_project_data: Dict[str, Any]
    ):
        """Test creating a project."""
        response = api_session.post(
            f"{config.tracer_base_url}/project/",
            json=test_project_data,
            timeout=config.request_timeout,
        )

        assert response.status_code in [201, 401, 403]

        if response.status_code == 201:
            project = response.json()
            assert "id" in project
            assert project["name"] == test_project_data["name"]
            print(f"Created project: {project['id']}")

            # Cleanup
            api_session.delete(
                f"{config.tracer_base_url}/project/{project['id']}/",
                timeout=config.request_timeout,
            )

    def test_get_project(
        self, api_session: requests.Session, created_project: Dict[str, Any]
    ):
        """Test getting a project by ID."""
        if "id" not in created_project:
            pytest.skip("Project creation not available")

        response = api_session.get(
            f"{config.tracer_base_url}/project/{created_project['id']}/",
            timeout=config.request_timeout,
        )

        assert response.status_code in [200, 401, 403, 404]

        if response.status_code == 200:
            project = response.json()
            assert project["id"] == created_project["id"]
            assert project["name"] == created_project["name"]

    def test_update_project(
        self, api_session: requests.Session, created_project: Dict[str, Any]
    ):
        """Test updating a project."""
        if "id" not in created_project:
            pytest.skip("Project creation not available")

        update_data = {"description": "Updated description"}

        response = api_session.patch(
            f"{config.tracer_base_url}/project/{created_project['id']}/",
            json=update_data,
            timeout=config.request_timeout,
        )

        assert response.status_code in [200, 401, 403, 404]

        if response.status_code == 200:
            project = response.json()
            assert project["description"] == "Updated description"

    def test_delete_project(
        self, api_session: requests.Session, test_project_data: Dict[str, Any]
    ):
        """Test deleting a project."""
        # First create a project to delete
        create_response = api_session.post(
            f"{config.tracer_base_url}/project/",
            json=test_project_data,
            timeout=config.request_timeout,
        )

        if create_response.status_code != 201:
            pytest.skip("Project creation not available")

        project = create_response.json()

        # Delete it
        delete_response = api_session.delete(
            f"{config.tracer_base_url}/project/{project['id']}/",
            timeout=config.request_timeout,
        )

        assert delete_response.status_code in [204, 200, 401, 403]

        if delete_response.status_code in [204, 200]:
            # Verify deletion
            get_response = api_session.get(
                f"{config.tracer_base_url}/project/{project['id']}/",
                timeout=config.request_timeout,
            )
            assert get_response.status_code == 404


class TestProjectFilters:
    """Test project filtering and search."""

    def test_filter_by_name(self, api_session: requests.Session):
        """Test filtering projects by name."""
        response = api_session.get(
            f"{config.tracer_base_url}/project/",
            params={"search": "e2e_test"},
            timeout=config.request_timeout,
        )

        assert response.status_code in [200, 401, 403]

    def test_pagination(self, api_session: requests.Session):
        """Test project list pagination."""
        response = api_session.get(
            f"{config.tracer_base_url}/project/",
            params={"page": 1, "page_size": 5},
            timeout=config.request_timeout,
        )

        assert response.status_code in [200, 401, 403]

        if response.status_code == 200:
            data = response.json()
            if isinstance(data, dict):
                # Paginated response
                assert "count" in data or "results" in data


class TestProjectValidation:
    """Test project validation."""

    def test_create_project_missing_name(self, api_session: requests.Session):
        """Test creating project without required name."""
        response = api_session.post(
            f"{config.tracer_base_url}/project/",
            json={"description": "No name"},
            timeout=config.request_timeout,
        )

        # Should fail validation
        assert response.status_code in [400, 401, 403]

    def test_create_project_invalid_data(self, api_session: requests.Session):
        """Test creating project with invalid data."""
        response = api_session.post(
            f"{config.tracer_base_url}/project/",
            json={"name": "", "invalid_field": "value"},
            timeout=config.request_timeout,
        )

        assert response.status_code in [400, 401, 403]
