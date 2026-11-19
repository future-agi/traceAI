"""Collector-side wire decoder: no adapter output rewriting."""
import sys
from google.protobuf.json_format import MessageToJson
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
request = ExportTraceServiceRequest()
request.ParseFromString(sys.stdin.buffer.read())
print(MessageToJson(request))
