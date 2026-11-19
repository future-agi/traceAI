"""Recording metadata on an explicitly owned session-envelope trace.

Uses the modern LiveKitAPI/service/request shape. Validate against your installed
livekit-api before deployment; the recording adapter does not promise arbitrary
SDK-version compatibility or discovery of LiveKit Cloud's native session root.
"""

import asyncio
import logging

from dotenv import load_dotenv
from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.otel import ProjectType, register
from livekit import api
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    cli,
    room_io,
)
from livekit.plugins import openai, silero
from opentelemetry import trace

from traceai_livekit import enable_http_attribute_mapping, record_egress, wrap_egress_client

load_dotenv()
logger = logging.getLogger("traceai-example-egress")


class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions="""You are a voice assistant demonstrating call recording.
            Inform the user that this call is being recorded for quality assurance.""",
        )


server = AgentServer()


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


server.setup_fnc = prewarm


@server.rtc_session()
async def entrypoint(ctx: JobContext):
    logger.info("connecting to recording-enabled room")
    # Obtain required recording consent before enabling egress in your deployment.
    # Set up inside the worker process, before creating the envelope or egress.
    provider = register(
        project_name="LiveKit Agent with Egress",
        project_type=ProjectType.OBSERVE,
        set_global_tracer_provider=True,
    )
    enable_http_attribute_mapping()
    policy = TraceConfig()
    tracer = provider.get_tracer(__name__)
    fi_tracer = FITracer(tracer, config=policy)
    envelope = fi_tracer.start_span("LiveKit Agent Session", fi_span_kind="agent")
    saved_parent = trace.NonRecordingSpan(envelope.get_span_context())
    api_owner = None
    egress = None
    egress_id = None
    cleanup_started = False

    async def _stop_recording_and_close(reason=None):
        # LiveKit supplies a reason. Do not log it or pass it to a flush timeout.
        nonlocal cleanup_started
        if cleanup_started:
            return
        cleanup_started = True
        try:
            if egress is not None and egress_id:
                try:
                    # This example alone bounds waiting; the library never changes
                    # delegate timeouts. ENDING is not a final-file guarantee.
                    await asyncio.wait_for(
                        egress.stop_egress(api.StopEgressRequest(egress_id=egress_id)),
                        timeout=2.0,
                    )
                except Exception as exc:
                    logger.warning("recording stop unavailable (%s)", type(exc).__name__)
        finally:
            try:
                if api_owner is not None:
                    try:
                        await asyncio.wait_for(api_owner.aclose(), timeout=1.0)
                    except Exception as exc:
                        logger.warning("recording API close unavailable (%s)", type(exc).__name__)
            finally:
                envelope.end()
                # Existing process-exit flush covers snapshots/envelope ended after
                # the earlier job-shutdown flush. No extra flush worker is needed.

    def on_final_egress_info(final_info):
        # Optional integration point for an application-delivered final response.
        # Register with YOUR existing callback source if needed. No polling,
        # webhook server, media read/upload or implicit remote completion capture.
        return record_egress(
            final_info, session_span=saved_parent, room_name=ctx.room.name,
            tracer=tracer, trace_config=policy,
        )

    try:
        ctx.add_shutdown_callback(_stop_recording_and_close)
        # The envelope remains open when this context/entrypoint returns. Native
        # session startup inherits this context where the LiveKit mode supports it.
        with trace.use_span(
            envelope, end_on_exit=False, record_exception=False,
            set_status_on_exception=False,
        ):
            try:
                api_owner = api.LiveKitAPI()
                egress = wrap_egress_client(
                    api_owner.egress, session_span=saved_parent,
                    room_name=ctx.room.name, tracer=tracer, trace_config=policy,
                )
                info = await egress.start_room_composite_egress(
                    api.RoomCompositeEgressRequest(
                        room_name=ctx.room.name,
                        file_outputs=[api.EncodedFileOutput(
                            filepath="recordings/{room_name}-{time}.mp4",
                            file_type=api.EncodedFileType.MP4,
                        )],
                    )
                )
                egress_id = info.egress_id
                logger.info("recording start response received")
            except Exception as exc:
                logger.warning("recording start unavailable (%s)", type(exc).__name__)
                logger.info("continuing without a confirmed recording")

            session = AgentSession(
                stt=openai.STT(), llm=openai.LLM(), tts=openai.TTS(),
                vad=ctx.proc.userdata["vad"], preemptive_generation=True,
            )
            await session.start(
                agent=Assistant(), room=ctx.room,
                room_options=room_io.RoomOptions(audio_input=room_io.AudioInputOptions()),
            )
            await ctx.connect()
    except BaseException:
        # Cleanup also runs on startup failure/cancellation; do not turn either into
        # a telemetry error or swallow it. Ordinary cleanup failures are contained.
        await _stop_recording_and_close()
        raise


if __name__ == "__main__":
    cli.run_app(server)
