"""Trace one Gemini Live voice session with a tool call in Future AGI.

    pip install traceAI-google-genai-live "google-genai>=2.28,<3"
    export GEMINI_API_KEY=... FI_API_KEY=... FI_SECRET_KEY=...
    python voice_tool.py                      # typed question, spoken answer
    python voice_tool.py --pcm question.pcm   # spoken question (16 kHz, 16-bit mono PCM)
    FI_HIDE_INPUTS=true FI_HIDE_OUTPUTS=true python voice_tool.py   # metadata only, no content

Options used by the package's end-to-end run:
    --marker TEXT       put TEXT in the system instruction, the question and the tool result
    --resume            drop the socket after the first answer and resume with the stored handle
    --model NAME        Live model (default gemini-3.8-live; the list is preview, re-read it)
    --modality TEXT     ask for TEXT responses (native-audio models refuse this)
    --user-id ID        attach your own user id to the call (the wire carries none)
"""

import argparse
import asyncio
import os

from fi_instrumentation import register, using_user
from fi_instrumentation.fi_types import ProjectType
from google import genai
from google.genai import errors, types

from traceai_google_genai_live import instrument

parser = argparse.ArgumentParser()
parser.add_argument("--project", default=os.getenv("FI_PROJECT_NAME", "gemini-live-demo"))
parser.add_argument("--model", default="gemini-3.8-live")
parser.add_argument("--pcm")
parser.add_argument("--marker", default="")
parser.add_argument("--resume", action="store_true")
parser.add_argument("--modality", default="AUDIO")
parser.add_argument("--user-id")
args = parser.parse_args()

# 1. Future AGI first, then the instrumentor. Content capture follows the standard
#    FI_HIDE_INPUTS / FI_HIDE_OUTPUTS environment variables (or pass config=TraceConfig(...)).
trace_provider = register(project_type=ProjectType.OBSERVE, project_name=args.project)
instrument(tracer_provider=trace_provider)

WEATHER = {
    "name": "get_weather",
    "description": "Current weather for a city.",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
}


def get_weather(city: str) -> dict:
    return {"city": city, "forecast": "18C and sunny", "note": args.marker}


def live_config(handle=None) -> dict:
    return {
        "response_modalities": [args.modality],
        "input_audio_transcription": {},
        "output_audio_transcription": {},
        "system_instruction": "You are a terse voice assistant. Always use get_weather for weather. "
                              + args.marker,
        "tools": [{"function_declarations": [WEATHER]}],
        "session_resumption": {"handle": handle} if handle else {},
    }


async def stream_pcm(session, path: str) -> None:
    pcm = open(path, "rb").read()
    chunk = 3200  # 100 ms of 16 kHz 16-bit mono
    for i in range(0, len(pcm), chunk):
        await session.send_realtime_input(audio=types.Blob(data=pcm[i:i + chunk], mime_type="audio/pcm;rate=16000"))
        await asyncio.sleep(0.1)
    for _ in range(15):  # trailing silence so server VAD ends the user turn
        await session.send_realtime_input(audio=types.Blob(data=b"\x00" * chunk, mime_type="audio/pcm;rate=16000"))
        await asyncio.sleep(0.1)
    await session.send_realtime_input(audio_stream_end=True)


async def answer_turns(session, state: dict) -> None:
    """Receive until the model has spoken an answer (a turn with audio), answering tool calls."""
    while True:
        spoke = False
        async for msg in session.receive():
            if msg.session_resumption_update and msg.session_resumption_update.new_handle:
                state["handle"] = msg.session_resumption_update.new_handle
            if msg.data:
                spoke = True
            if msg.server_content and msg.server_content.output_transcription:
                print("model:", msg.server_content.output_transcription.text, flush=True)
            if msg.tool_call:
                await session.send_tool_response(function_responses=[
                    types.FunctionResponse(id=fc.id, name=fc.name, response=get_weather(**(fc.args or {})))
                    for fc in msg.tool_call.function_calls])
        if spoke:
            return


async def main() -> None:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    question = "What is the weather in Paris right now? " + args.marker
    state: dict = {}
    async with client.aio.live.connect(model=args.model, config=live_config()) as session:
        if args.pcm:
            sender = asyncio.create_task(stream_pcm(session, args.pcm))
        else:
            await session.send_client_content(turns={"role": "user", "parts": [{"text": question}]})
            sender = None
        await asyncio.wait_for(answer_turns(session, state), 60)
        if sender:
            await sender
        if args.resume:
            # Simulate a dropped connection: the next receive fails, as it would on a network drop.
            await session.close()
            try:
                async for _ in session.receive():
                    pass
            except errors.APIError as exc:
                print("connection dropped:", exc.code, flush=True)
    if args.resume and state.get("handle"):
        async with client.aio.live.connect(model=args.model, config=live_config(state["handle"])) as session:
            await session.send_client_content(turns={"role": "user", "parts": [{"text": "Which city did I ask about?"}]})
            await asyncio.wait_for(answer_turns(session, state), 60)


try:
    if args.user_id:
        with using_user(args.user_id):
            asyncio.run(main())
    else:
        asyncio.run(main())
finally:
    trace_provider.force_flush()
