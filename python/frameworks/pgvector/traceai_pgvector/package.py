# SQL-level pgvector use does not require the Python type-adapter package.
# Each maintained instrumentor checks its own installed driver distribution.
_instruments = (
    "opentelemetry-instrumentation-psycopg2 >= 0.59b0, < 1",
    "opentelemetry-instrumentation-psycopg >= 0.59b0, < 1",
)
