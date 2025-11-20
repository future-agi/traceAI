import logging
import os

from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    cli,
    inference,
    room_io,
)
from livekit.plugins import openai, silero

# TraceAI Imports
from fi_instrumentation import FITracer
from fi_instrumentation.otel import register, ProjectType, Transport
from traceai_livekit import enable_http_attribute_mapping

load_dotenv()

logger = logging.getLogger("traceai-example")

class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions="""You are a voice assistant created by Future AGI. Your interface with users will be voice.
            You should provide short and concise answers to user queries.
            """,
        )

server = AgentServer()


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()

server.setup_fnc = prewarm

@server.rtc_session()
async def entrypoint(ctx: JobContext):
    logger.info(f"connecting to room {ctx.room.name}")
    
    # Initialize TraceAI INSIDE the process to avoid multiprocessing pickling errors
    provider = register(
        project_name="LiveKit Agent Example",
        project_type=ProjectType.OBSERVE,
        set_global_tracer_provider=True,
    )
    enable_http_attribute_mapping()
    
    # Create the tracer helper
    tracer = FITracer(provider.get_tracer(__name__))
    
    # Use context manager for parent span instead of decorator
    # This ensures the span starts when this process is actually running
    with tracer.start_as_current_span("LiveKit Agent Session", fi_span_kind="agent") as parent_span:
        parent_span.set_input(f"Room: {ctx.room.name}")
    
        # Modern AgentSession setup
        session = AgentSession(
            stt=openai.STT(), # Requires OPENAI_API_KEY
            llm=openai.LLM(),   # Requires OPENAI_API_KEY
            tts=openai.TTS(),   # Requires OPENAI_API_KEY
            vad=ctx.proc.userdata["vad"],
            preemptive_generation=True,
        )

        await session.start(
            agent=Assistant(),
            room=ctx.room,
            room_options=room_io.RoomOptions(
                audio_input=room_io.AudioInputOptions(),
            ),
        )
        
        await ctx.connect()


if __name__ == "__main__":
    cli.run_app(server)
