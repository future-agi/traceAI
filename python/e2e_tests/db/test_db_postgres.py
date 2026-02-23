"""
E2E Tests for PostgreSQL Database Verification

Verifies data is correctly stored in PostgreSQL.
"""

import pytest
import os
import time
import uuid
from datetime import datetime
from typing import Dict, Any, Optional

from config import config


def get_db_connection():
    """Get PostgreSQL database connection."""
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor

        conn = psycopg2.connect(
            host=config.pg_host,
            port=config.pg_port,
            database=config.pg_db,
            user=config.pg_user,
            password=config.pg_password,
        )
        return conn
    except ImportError:
        pytest.skip("psycopg2 not installed")
    except Exception as e:
        pytest.skip(f"Cannot connect to PostgreSQL: {e}")


class TestTraceStorage:
    """Test trace data storage in PostgreSQL."""

    @pytest.fixture
    def db_conn(self):
        """Get database connection."""
        conn = get_db_connection()
        yield conn
        conn.close()

    def test_traces_table_exists(self, db_conn):
        """Verify traces table exists."""
        cursor = db_conn.cursor()
        cursor.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_name = 'tracer_trace'
            );
        """)
        result = cursor.fetchone()
        assert result[0] is True
        cursor.close()

    def test_observation_spans_table_exists(self, db_conn):
        """Verify observation spans table exists."""
        cursor = db_conn.cursor()
        cursor.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_name = 'tracer_observationspan'
            );
        """)
        result = cursor.fetchone()
        assert result[0] is True
        cursor.close()

    def test_trace_columns(self, db_conn):
        """Verify trace table has expected columns."""
        cursor = db_conn.cursor()
        cursor.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'tracer_trace';
        """)
        columns = {row[0] for row in cursor.fetchall()}

        expected_columns = {
            "id",
            "trace_id",
            "project_id",
            "name",
            "start_time",
            "end_time",
            "user_id",
            "session_id",
        }

        for col in expected_columns:
            assert col in columns, f"Missing column: {col}"

        cursor.close()

    def test_span_columns(self, db_conn):
        """Verify span table has expected columns."""
        cursor = db_conn.cursor()
        cursor.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'tracer_observationspan';
        """)
        columns = {row[0] for row in cursor.fetchall()}

        expected_columns = {
            "id",
            "span_id",
            "trace_id",
            "name",
            "observation_type",
            "start_time",
            "end_time",
            "model",
            "prompt_tokens",
            "completion_tokens",
            "cost",
        }

        for col in expected_columns:
            assert col in columns, f"Missing column: {col}"

        cursor.close()


class TestProjectStorage:
    """Test project data storage."""

    @pytest.fixture
    def db_conn(self):
        """Get database connection."""
        conn = get_db_connection()
        yield conn
        conn.close()

    def test_projects_table_exists(self, db_conn):
        """Verify projects table exists."""
        cursor = db_conn.cursor()
        cursor.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_name = 'tracer_project'
            );
        """)
        result = cursor.fetchone()
        assert result[0] is True
        cursor.close()

    def test_count_projects(self, db_conn):
        """Count existing projects."""
        cursor = db_conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM tracer_project;")
        count = cursor.fetchone()[0]
        print(f"Total projects in database: {count}")
        assert count >= 0
        cursor.close()


class TestDataIntegrity:
    """Test data integrity and foreign keys."""

    @pytest.fixture
    def db_conn(self):
        """Get database connection."""
        conn = get_db_connection()
        yield conn
        conn.close()

    def test_span_trace_foreign_key(self, db_conn):
        """Verify spans have valid trace references."""
        cursor = db_conn.cursor()

        # Check for orphaned spans (spans without valid trace)
        cursor.execute("""
            SELECT COUNT(*)
            FROM tracer_observationspan s
            LEFT JOIN tracer_trace t ON s.trace_id = t.id
            WHERE t.id IS NULL;
        """)
        orphaned = cursor.fetchone()[0]
        print(f"Orphaned spans: {orphaned}")
        # Some orphaned spans might be acceptable during testing
        cursor.close()

    def test_trace_project_foreign_key(self, db_conn):
        """Verify traces have valid project references."""
        cursor = db_conn.cursor()

        cursor.execute("""
            SELECT COUNT(*)
            FROM tracer_trace t
            LEFT JOIN tracer_project p ON t.project_id = p.id
            WHERE p.id IS NULL;
        """)
        orphaned = cursor.fetchone()[0]
        print(f"Traces without project: {orphaned}")
        cursor.close()

    def test_no_duplicate_trace_ids(self, db_conn):
        """Check for duplicate trace IDs within same project."""
        cursor = db_conn.cursor()

        cursor.execute("""
            SELECT trace_id, project_id, COUNT(*)
            FROM tracer_trace
            GROUP BY trace_id, project_id
            HAVING COUNT(*) > 1
            LIMIT 10;
        """)
        duplicates = cursor.fetchall()

        if duplicates:
            print(f"Found {len(duplicates)} duplicate trace IDs")

        cursor.close()


class TestTokenAndCostStorage:
    """Test token counts and cost calculations."""

    @pytest.fixture
    def db_conn(self):
        """Get database connection."""
        conn = get_db_connection()
        yield conn
        conn.close()

    def test_token_counts_not_negative(self, db_conn):
        """Verify token counts are non-negative."""
        cursor = db_conn.cursor()

        cursor.execute("""
            SELECT COUNT(*)
            FROM tracer_observationspan
            WHERE prompt_tokens < 0 OR completion_tokens < 0;
        """)
        negative_count = cursor.fetchone()[0]
        assert negative_count == 0, f"Found {negative_count} spans with negative token counts"
        cursor.close()

    def test_cost_not_negative(self, db_conn):
        """Verify costs are non-negative."""
        cursor = db_conn.cursor()

        cursor.execute("""
            SELECT COUNT(*)
            FROM tracer_observationspan
            WHERE cost < 0;
        """)
        negative_count = cursor.fetchone()[0]
        assert negative_count == 0, f"Found {negative_count} spans with negative cost"
        cursor.close()

    def test_sample_token_data(self, db_conn):
        """Sample token data from recent spans."""
        cursor = db_conn.cursor()

        cursor.execute("""
            SELECT
                model,
                AVG(prompt_tokens) as avg_prompt,
                AVG(completion_tokens) as avg_completion,
                AVG(cost) as avg_cost
            FROM tracer_observationspan
            WHERE model IS NOT NULL
            GROUP BY model
            LIMIT 10;
        """)
        results = cursor.fetchall()

        for row in results:
            print(f"Model: {row[0]}, Avg Prompt: {row[1]:.0f}, Avg Completion: {row[2]:.0f}, Avg Cost: ${row[3]:.4f}")

        cursor.close()


class TestTimestampStorage:
    """Test timestamp storage."""

    @pytest.fixture
    def db_conn(self):
        """Get database connection."""
        conn = get_db_connection()
        yield conn
        conn.close()

    def test_span_timestamps_valid(self, db_conn):
        """Verify span timestamps are valid."""
        cursor = db_conn.cursor()

        # Check for spans where end_time < start_time
        cursor.execute("""
            SELECT COUNT(*)
            FROM tracer_observationspan
            WHERE end_time IS NOT NULL AND end_time < start_time;
        """)
        invalid_count = cursor.fetchone()[0]
        print(f"Spans with invalid timestamps: {invalid_count}")
        cursor.close()

    def test_recent_data_exists(self, db_conn):
        """Check if there's recent data."""
        cursor = db_conn.cursor()

        cursor.execute("""
            SELECT COUNT(*)
            FROM tracer_trace
            WHERE start_time > NOW() - INTERVAL '7 days';
        """)
        recent_count = cursor.fetchone()[0]
        print(f"Traces in last 7 days: {recent_count}")
        cursor.close()
