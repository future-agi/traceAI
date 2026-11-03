"""Allowlisted metadata hooks for the maintained PyMongo command listener."""

from collections.abc import Mapping
from typing import Any

from opentelemetry.trace import Span, Status, StatusCode
from pymongo import monitoring


def _count(span: Span, name: str, value: Any) -> None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        span.set_attribute(name, value)


def request_hook(span: Span, event: monitoring.CommandStartedEvent) -> None:
    if not span.is_recording():
        return
    command = event.command
    span.set_attribute("db.operation.name", event.command_name)
    collection = command.get("collection") if event.command_name == "getMore" else command.get(event.command_name)
    if isinstance(collection, str):
        span.set_attribute("db.vector.collection.name", collection)
    if event.command_name != "aggregate":
        return
    pipeline = command.get("pipeline")
    if not isinstance(pipeline, list):
        return
    span.set_attribute("db.mongodb.pipeline_stages", len(pipeline))
    for stage in pipeline:
        if not isinstance(stage, Mapping):
            continue
        vector = stage.get("$vectorSearch")
        if not isinstance(vector, Mapping):
            continue
        span.set_attribute("db.vector.query.type", "vector_search")
        for key, attribute in (("index", "db.vector.index.name"), ("path", "db.vector.query.field")):
            value = vector.get(key)
            if isinstance(value, str):
                span.set_attribute(attribute, value)
        _count(span, "db.vector.query.top_k", vector.get("limit"))
        _count(span, "db.vector.query.num_candidates", vector.get("numCandidates"))
        query_vector = vector.get("queryVector")
        if isinstance(query_vector, (list, tuple)):
            span.set_attribute("db.vector.query.vector_dimensions", len(query_vector))
        break


def _error(span: Span, details: Any) -> None:
    # Server messages can echo documents, filters or keys. Export a stable status
    # and numeric code only, including when upstream has already set a message.
    span.set_status(Status(StatusCode.ERROR, "MongoDB command failed"))
    span.set_attribute("error.type", "MongoDBCommandError")
    if isinstance(details, Mapping):
        _count(span, "db.response.status_code", details.get("code"))


def response_hook(span: Span, event: monitoring.CommandSucceededEvent) -> None:
    reply = event.reply
    errors = reply.get("writeErrors")
    concern = reply.get("writeConcernError")
    if isinstance(errors, list) and errors:
        _error(span, errors[0])
    elif concern:
        _error(span, concern)

    cursor = reply.get("cursor")
    if isinstance(cursor, Mapping):
        batch = cursor.get("firstBatch", cursor.get("nextBatch"))
        if isinstance(batch, list):
            span.set_attribute("db.vector.results.count", len(batch))
    if event.command_name == "insert":
        _count(span, "db.vector.upsert.count", reply.get("n"))
    elif event.command_name == "update":
        # MongoDB's n includes upserts, which must not be reported as matches.
        upserted = reply.get("upserted", [])
        n = reply.get("n")
        if isinstance(n, int) and not isinstance(n, bool) and isinstance(upserted, list):
            _count(span, "db.vector.update.matched_count", n - len(upserted))
        _count(span, "db.vector.update.modified_count", reply.get("nModified"))
    elif event.command_name == "delete":
        _count(span, "db.vector.delete.count", reply.get("n"))


def failed_hook(span: Span, event: monitoring.CommandFailedEvent) -> None:
    _error(span, event.failure)
