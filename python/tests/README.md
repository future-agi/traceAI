# FI-Instrumentation Test Suite

This directory contains comprehensive tests for the `fi-instrumentation` OpenTelemetry SDK. The test suite covers all major functionality including settings, configuration, context attributes, tracers, and OpenTelemetry integration.

### Configuration

- **`pytest.ini`** - Test configuration with coverage settings, markers, and warning filters

## Running Tests

### Install Dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e . #installing the package in editable mode
```

### Run All Tests

```bash
pytest tests/
```

### Run with Coverage

```bash
pytest tests/ --cov=fi_instrumentation --cov-report=html
```

### Run Specific Test Files

```bash
# Settings tests
pytest tests/test_settings.py -v

# Tracer tests
pytest tests/test_tracers.py -v

# OpenTelemetry integration tests
pytest tests/test_otel.py -v
```

