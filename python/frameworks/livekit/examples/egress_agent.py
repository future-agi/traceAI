import logging
import os

from dotenv import load_dotenv
from livekit import api
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
from fi_instrumentation.otel import register, ProjectType, Transport
from traceai_livekit import enable_http_attribute_mapping

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
    logger.info(f"connecting to room {ctx.room.name}")

    # --- 1. Initialize TraceAI ---
    register(
        project_name="LiveKit Agent with Egress",
        project_type=ProjectType.OBSERVE,
        set_global_tracer_provider=True,
    )
    enable_http_attribute_mapping()
    # -----------------------------

    # --- 2. Start Egress (Recording) ---
    try:
        egress_api = api.EgressClient(
            url=os.getenv("LIVEKIT_URL"),
            api_key=os.getenv("LIVEKIT_API_KEY"),
            api_secret=os.getenv("LIVEKIT_API_SECRET")
        )
        
        file_output = api.EncodedFileOutput(
            filepath=f"recordings/{ctx.room.name}-{ctx.job_id}.mp4",
            file_type=api.EncodedFileType.MP4
        )
        
        info = await egress_api.start_room_composite_egress(
            room_name=ctx.room.name,
            file_outputs=[file_output],
            # audio_only=True # Uncomment for audio-only recording
        )
        logger.info(f"Started egress recording: {info.egress_id}")
        
    except Exception as e:
        logger.error(f"Failed to start egress: {e}")
        logger.warning("Continuing without recording...")

    # -----------------------------------

    session = AgentSession(
        stt=openai.STT(),
        llm=openai.LLM(),
        tts=openai.TTS(),
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
