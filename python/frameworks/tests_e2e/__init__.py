"""
End-to-End Integration Tests for Vector Database Instrumentation

These tests verify that the instrumentation works correctly with real
database instances. They require actual database connections.

To run these tests:
1. Start the required databases (see docker-compose.yml)
2. Set environment variables
3. Run: pytest tests_e2e/ -v --integration
"""
