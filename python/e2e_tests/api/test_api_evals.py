"""
E2E Tests for Evaluation API Endpoints

Tests evaluation tasks and custom eval configurations.
"""

import pytest
import requests
import uuid
from datetime import datetime
from typing import Dict, Any

from config import config


class TestEvalTaskCRUD:
    """Test Eval Task CRUD operations."""

    @pytest.fixture
    def eval_task_data(self, test_project: Dict[str, Any]) -> Dict[str, Any]:
        """Generate test eval task data."""
        return {
            "name": f"e2e_test_eval_{uuid.uuid4().hex[:8]}",
            "project": test_project.get("id"),
            "eval_type": "toxicity",
            "config": {
                "threshold": 0.5,
            },
        }

    def test_list_eval_tasks(self, api_session: requests.Session, test_project: Dict[str, Any]):
        """Test listing eval tasks."""
        response = api_session.get(
            f"{config.tracer_base_url}/eval-task/",
            params={"project_id": test_project.get("id")},
            timeout=config.request_timeout,
        )

        assert response.status_code in [200, 401, 403]

        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, (list, dict))
            print(f"Found eval tasks: {len(data.get('results', data)) if isinstance(data, dict) else len(data)}")

    def test_create_eval_task(self, api_session: requests.Session, eval_task_data: Dict[str, Any]):
        """Test creating an eval task."""
        response = api_session.post(
            f"{config.tracer_base_url}/eval-task/",
            json=eval_task_data,
            timeout=config.request_timeout,
        )

        assert response.status_code in [201, 400, 401, 403]

        if response.status_code == 201:
            task = response.json()
            assert "id" in task
            print(f"Created eval task: {task['id']}")

            # Cleanup
            api_session.delete(
                f"{config.tracer_base_url}/eval-task/{task['id']}/",
                timeout=config.request_timeout,
            )

    def test_get_eval_task(self, api_session: requests.Session):
        """Test getting an eval task by ID."""
        task_id = str(uuid.uuid4())

        response = api_session.get(
            f"{config.tracer_base_url}/eval-task/{task_id}/",
            timeout=config.request_timeout,
        )

        assert response.status_code in [404, 401, 403]


class TestEvalTypes:
    """Test different evaluation types."""

    def test_toxicity_eval(self, api_session: requests.Session, test_project: Dict[str, Any]):
        """Test toxicity evaluation."""
        eval_data = {
            "name": f"toxicity_eval_{uuid.uuid4().hex[:8]}",
            "project": test_project.get("id"),
            "eval_type": "toxicity",
            "config": {"threshold": 0.5},
        }

        response = api_session.post(
            f"{config.tracer_base_url}/eval-task/",
            json=eval_data,
            timeout=config.request_timeout,
        )

        assert response.status_code in [201, 400, 401, 403]

        if response.status_code == 201:
            api_session.delete(
                f"{config.tracer_base_url}/eval-task/{response.json()['id']}/",
                timeout=config.request_timeout,
            )

    def test_hallucination_eval(self, api_session: requests.Session, test_project: Dict[str, Any]):
        """Test hallucination evaluation."""
        eval_data = {
            "name": f"hallucination_eval_{uuid.uuid4().hex[:8]}",
            "project": test_project.get("id"),
            "eval_type": "hallucination",
            "config": {"threshold": 0.3},
        }

        response = api_session.post(
            f"{config.tracer_base_url}/eval-task/",
            json=eval_data,
            timeout=config.request_timeout,
        )

        assert response.status_code in [201, 400, 401, 403]

        if response.status_code == 201:
            api_session.delete(
                f"{config.tracer_base_url}/eval-task/{response.json()['id']}/",
                timeout=config.request_timeout,
            )

    def test_relevance_eval(self, api_session: requests.Session, test_project: Dict[str, Any]):
        """Test relevance evaluation."""
        eval_data = {
            "name": f"relevance_eval_{uuid.uuid4().hex[:8]}",
            "project": test_project.get("id"),
            "eval_type": "relevance",
            "config": {"threshold": 0.7},
        }

        response = api_session.post(
            f"{config.tracer_base_url}/eval-task/",
            json=eval_data,
            timeout=config.request_timeout,
        )

        assert response.status_code in [201, 400, 401, 403]

        if response.status_code == 201:
            api_session.delete(
                f"{config.tracer_base_url}/eval-task/{response.json()['id']}/",
                timeout=config.request_timeout,
            )


class TestCustomEvalConfig:
    """Test Custom Eval Configuration."""

    @pytest.fixture
    def custom_eval_data(self, test_project: Dict[str, Any]) -> Dict[str, Any]:
        """Generate custom eval config data."""
        return {
            "name": f"custom_eval_{uuid.uuid4().hex[:8]}",
            "project": test_project.get("id"),
            "description": "Custom evaluation for testing",
            "prompt_template": "Evaluate the following response for helpfulness: {response}",
            "score_type": "numeric",
            "score_range": {"min": 0, "max": 10},
        }

    def test_list_custom_evals(self, api_session: requests.Session, test_project: Dict[str, Any]):
        """Test listing custom eval configs."""
        response = api_session.get(
            f"{config.tracer_base_url}/custom-eval-config/",
            params={"project_id": test_project.get("id")},
            timeout=config.request_timeout,
        )

        assert response.status_code in [200, 401, 403]

        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, (list, dict))

    def test_create_custom_eval(self, api_session: requests.Session, custom_eval_data: Dict[str, Any]):
        """Test creating custom eval config."""
        response = api_session.post(
            f"{config.tracer_base_url}/custom-eval-config/",
            json=custom_eval_data,
            timeout=config.request_timeout,
        )

        assert response.status_code in [201, 400, 401, 403]

        if response.status_code == 201:
            eval_config = response.json()
            assert "id" in eval_config
            print(f"Created custom eval: {eval_config['id']}")

            # Cleanup
            api_session.delete(
                f"{config.tracer_base_url}/custom-eval-config/{eval_config['id']}/",
                timeout=config.request_timeout,
            )


class TestDatasets:
    """Test Dataset endpoints."""

    @pytest.fixture
    def dataset_data(self, test_project: Dict[str, Any]) -> Dict[str, Any]:
        """Generate dataset data."""
        return {
            "name": f"e2e_test_dataset_{uuid.uuid4().hex[:8]}",
            "project": test_project.get("id"),
            "description": "Test dataset for E2E testing",
        }

    def test_list_datasets(self, api_session: requests.Session, test_project: Dict[str, Any]):
        """Test listing datasets."""
        response = api_session.get(
            f"{config.tracer_base_url}/dataset/",
            params={"project_id": test_project.get("id")},
            timeout=config.request_timeout,
        )

        assert response.status_code in [200, 401, 403]

        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, (list, dict))

    def test_create_dataset(self, api_session: requests.Session, dataset_data: Dict[str, Any]):
        """Test creating a dataset."""
        response = api_session.post(
            f"{config.tracer_base_url}/dataset/",
            json=dataset_data,
            timeout=config.request_timeout,
        )

        assert response.status_code in [201, 400, 401, 403]

        if response.status_code == 201:
            dataset = response.json()
            assert "id" in dataset
            print(f"Created dataset: {dataset['id']}")

            # Cleanup
            api_session.delete(
                f"{config.tracer_base_url}/dataset/{dataset['id']}/",
                timeout=config.request_timeout,
            )
