"""Required SDK-artifact compatibility checks; no SDK calls or network.

Run in the explicitly provisioned compatibility CI job. Missing dependencies fail
that job, rather than masquerading as a feature pass. These checks establish shape,
not timestamp units, RPC behavior, native parentage or server finalization.
"""

import importlib
import inspect
from importlib.metadata import version

import pytest

import traceai_livekit


REQUESTS = (
    ("start_room_composite_egress", "RoomCompositeEgressRequest"),
    ("start_web_egress", "WebEgressRequest"),
    ("start_participant_egress", "ParticipantEgressRequest"),
    ("start_track_composite_egress", "TrackCompositeEgressRequest"),
    ("start_track_egress", "TrackEgressRequest"),
    ("stop_egress", "StopEgressRequest"),
    ("update_stream", "UpdateStreamRequest"),
    ("update_layout", "UpdateLayoutRequest"),
)


@pytest.mark.parametrize("method,request_name", REQUESTS)
def test_sdk_service_accepts_one_positional_request_and_is_async(method, request_name, record_property):
    assert callable(getattr(traceai_livekit, "wrap_egress_client", None))
    api = importlib.import_module("livekit.api")
    service = importlib.import_module("livekit.api.egress_service").EgressService
    for distribution in ("livekit-api", "livekit-protocol", "fi-instrumentation-otel"):
        record_property(distribution, version(distribution))
    request = getattr(api, request_name)()
    fn = getattr(service, method)
    assert inspect.iscoroutinefunction(fn)
    inspect.signature(fn).bind(None, request)
    assert isinstance(inspect.getattr_static(api.LiveKitAPI, "egress"), property)
    assert inspect.iscoroutinefunction(api.LiveKitAPI.aclose)


def test_sdk_modern_request_and_response_field_contract(record_property):
    assert callable(getattr(traceai_livekit, "record_egress", None))
    api = importlib.import_module("livekit.api")
    record_property("livekit-api", version("livekit-api"))
    record_property("livekit-protocol", version("livekit-protocol"))
    start = api.RoomCompositeEgressRequest(
        room_name="room-synthetic",
        file_outputs=[api.EncodedFileOutput(
            filepath="recordings/call.mp4", file_type=api.EncodedFileType.MP4,
        )],
    )
    assert start.room_name == "room-synthetic"
    assert start.file_outputs[0].filepath == "recordings/call.mp4"
    stop = api.StopEgressRequest(egress_id="egress-synthetic")
    assert stop.egress_id == "egress-synthetic"
    info = api.EgressInfo(
        egress_id="egress-synthetic", room_name="room-synthetic", status=0,
        started_at=1700000000000000001, ended_at=1700000001000000001,
    )
    info.file_results.add(location="s3://bucket/call.mp4", filename="call.mp4")
    info.stream_results.add(url="rtmps://stream.example/key")
    assert info.egress_id == "egress-synthetic" and info.status == 0
    assert type(info.started_at) is int and type(info.ended_at) is int
    assert info.file_results[0].location == "s3://bucket/call.mp4"
    assert info.stream_results[0].url == "rtmps://stream.example/key"
    # A protobuf int64 shape alone does not establish its semantic time unit.
    # The verifier must separately inspect the exact artifact's published schema
    # comments/source and retain evidence of Unix nanoseconds before SDK support.
