"""Pytest configuration and common fixtures for fi_instrumentation tests."""

import os
import pytest
from typing import Dict, Any, Generator
from unittest.mock import patch, MagicMock

@pytest.fixture
def clean_env():
    """Fixture to clean environment variables before each test."""
    # Store original env vars
    original_env = dict(os.environ)
    
    # Clear relevant environment variables
    env_vars_to_clear = [
        "FI_BASE_URL",
        "FI_GRPC_URL", 
        "FI_PROJECT_NAME",
        "FI_PROJECT_VERSION_NAME",
        "FI_API_KEY",
        "FI_SECRET_KEY",
        "FI_HIDE_LLM_INVOCATION_PARAMETERS",
        "FI_HIDE_INPUTS",
        "FI_HIDE_OUTPUTS",
        "FI_HIDE_INPUT_MESSAGES",
        "FI_HIDE_OUTPUT_MESSAGES",
        "FI_HIDE_INPUT_IMAGES",
        "FI_HIDE_INPUT_TEXT",
        "FI_HIDE_OUTPUT_TEXT",
        "FI_HIDE_EMBEDDING_VECTORS",
        "FI_BASE64_IMAGE_MAX_LENGTH",
    ]
    
    for var in env_vars_to_clear:
        os.environ.pop(var, None)
    
    yield
    
    # Restore original environment
    os.environ.clear()
    os.environ.update(original_env)


@pytest.fixture
def mock_requests():
    """Mock requests module for HTTP calls."""
    with patch('requests.post') as mock_post, \
         patch('requests.get') as mock_get:
        mock_response = MagicMock()
        mock_response.json.return_value = {"result": {"isUserEvalTemplate": False, "evalTemplate": {}}}
        mock_response.raise_for_status.return_value = None
        mock_response.status_code = 200
        mock_post.return_value = mock_response
        mock_get.return_value = mock_response
        yield {
            'post': mock_post,
            'get': mock_get,
            'response': mock_response
        }


@pytest.fixture
def sample_env_vars():
    """Sample environment variables for testing."""
    return {
        "FI_BASE_URL": "https://test-api.example.com",
        "FI_GRPC_URL": "https://test-grpc.example.com:50051",
        "FI_PROJECT_NAME": "test_project",
        "FI_PROJECT_VERSION_NAME": "test_version",
        "FI_API_KEY": "test_api_key",
        "FI_SECRET_KEY": "test_secret_key",
    }


@pytest.fixture
def set_env_vars(sample_env_vars):
    """Set environment variables for testing."""
    for key, value in sample_env_vars.items():
        os.environ[key] = value
    yield sample_env_vars
    # Cleanup handled by clean_env fixture


@pytest.fixture(autouse=True)
def mock_eval_config_check():
    """Auto-mock eval config checks to avoid HTTP calls."""
    with patch('fi_instrumentation.otel.check_custom_eval_config_exists') as mock_check, \
         patch('fi_instrumentation.settings.get_custom_eval_template') as mock_template:
        mock_check.return_value = False
        mock_template.return_value = {"isUserEvalTemplate": False, "evalTemplate": {}}
        yield {
            'check': mock_check,
            'template': mock_template
        }


@pytest.fixture(autouse=True)
def mock_all_http_requests():
    """Mock all HTTP requests to prevent network calls during tests."""
    import requests
    
    with patch.object(requests, 'post') as mock_post, \
         patch.object(requests, 'get') as mock_get, \
         patch.object(requests, 'put') as mock_put, \
         patch.object(requests, 'delete') as mock_delete:
        
        # Configure all HTTP method mocks
        mock_response = MagicMock()
        mock_response.json.return_value = {"result": {"isUserEvalTemplate": False, "evalTemplate": {}}}
        mock_response.raise_for_status.return_value = None
        mock_response.status_code = 200
        mock_response.text = '{"result": {"isUserEvalTemplate": false}}'
        
        mock_post.return_value = mock_response
        mock_get.return_value = mock_response
        mock_put.return_value = mock_response
        mock_delete.return_value = mock_response
        
        yield {
            'post': mock_post,
            'get': mock_get,
            'put': mock_put,
            'delete': mock_delete,
            'response': mock_response
        } 