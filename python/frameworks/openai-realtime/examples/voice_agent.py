"""A server-side voice agent on the OpenAI Realtime API, traced to Future AGI.

The agent opens ``client.realtime.connect()``, configures the session (voice,
input transcription, server VAD, one tool), streams microphone audio, plays the
model's audio, runs the tool when the model calls it, and handles barge-in the
way OpenAI's guide describes: on ``input_audio_buffer.speech_started`` it stops
playback and sends ``conversation.item.truncate`` with how much audio was heard.

There is no microphone or speaker here: the "mic" is a synthetic tone followed by
silence, and "playback" is a clock that counts how much of the received audio
would have played. Everything on the wire is the real SDK talking to the real
socket (or to a local stand-in, see README).

    export FI_API_KEY=... FI_SECRET_KEY=...        # Future AGI
    export OPENAI_API_KEY=...                       # OpenAI
    python voice_agent.py --project my-voice-agent

Optional:
    OPENAI_WEBSOCKET_BASE_URL   this example's own setting: passed to AsyncOpenAI(websocket_base_url=...),
                                e.g. ws://127.0.0.1:8765/v1 for the stand-in server
    --metadata-only             trace without transcripts, instructions, tool arguments or results
    --typed "What's the weather in Paris?"
                                type the question, hear the answer: an audio session with a tool call
                                and no microphone (the shortest live check)
    --text                      a text-only session (traced, but not a Voice-tab call)
    --error-case                also send two invalid client events, so the trace shows error spans
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import struct
import sys
import time
from typing import Any, Dict, List, Optional

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from openai import AsyncOpenAI
from traceai_openai_realtime import OpenAIRealtimeInstrumentor

RATE = 24000
CHUNK_MS = 100

TOOLS = [
    {
        "type": "function",
        "name": "get_weather",
        "description": "Current or forecast weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}, "ref": {"type": "string"}},
            "required": ["city"],
        },
    }
]


def get_weather(city: str, marker: str = "", **_: Any) -> str:
    result = {"city": city, "temp_c": 18, "conditions": "sunny"}
    if marker:
        # Content that must never leave the process in metadata-only mode.
        tag = marker.rsplit("-", 1)[-1].lower()
        result["note"] = f"{marker} report filed by ops+{tag}@example.com from /Users/alice-{tag}/weather/cache.json"
    return json.dumps(result)


def pcm(ms: int, loud: bool) -> bytes:
    n = RATE * ms // 1000
    if not loud:
        return b"\x00\x00" * n
    return struct.pack(f"<{n}h", *(int(3000 * math.sin(2 * math.pi * 440 * i / RATE)) for i in range(n)))


class Playback:
    """Counts how much received audio would have reached the listener by now."""

    def __init__(self, speed: float) -> None:
        self.speed = speed
        self.item_id: Optional[str] = None
        self.buffered_ms = 0.0
        self.started: Optional[float] = None

    def add(self, item_id: str, b64: str) -> None:
        if item_id != self.item_id:
            self.item_id, self.buffered_ms, self.started = item_id, 0.0, time.monotonic()
        self.buffered_ms += len(base64.b64decode(b64)) / (RATE * 2 / 1000)

    def played_ms(self) -> int:
        if self.started is None:
            return 0
        return int(min(self.buffered_ms, (time.monotonic() - self.started) * 1000 * self.speed))

    def remaining_s(self) -> float:
        return max(0.0, (self.buffered_ms - self.played_ms()) / 1000 / self.speed)

    def stop(self) -> None:
        self.started = None


async def speak(conn: Any, speech_ms: int, pace: float) -> None:
    """Stream one utterance from the "mic": speech, then enough silence for VAD to end the turn."""
    for loud, total in ((True, speech_ms), (False, 600)):
        for _ in range(total // CHUNK_MS):
            await conn.input_audio_buffer.append(audio=base64.b64encode(pcm(CHUNK_MS, loud)).decode())
            await asyncio.sleep(pace)


async def voice_call(client: AsyncOpenAI, args: argparse.Namespace) -> None:
    instructions = "You are a concise weather assistant. Use get_weather for any weather question."
    if args.marker:
        instructions += f" Session reference {args.marker}."
    playback = Playback(args.playback_speed)
    responses_done = 0
    turn_cue = asyncio.Event()
    mic_tasks: List["asyncio.Task[None]"] = []

    try:
        async with client.realtime.connect(model=args.model) as conn:
            await conn.session.update(
                session={
                    "type": "realtime",
                    "instructions": instructions,
                    "tools": TOOLS,
                    "audio": {
                        "input": {
                            "transcription": {"model": "gpt-4o-mini-transcribe"},
                            "turn_detection": {"type": "server_vad", "silence_duration_ms": 300},
                        },
                        "output": {"voice": "marin"},
                    },
                }
            )
            if args.error_case:
                # Two client mistakes the server answers with an `error` event.
                await conn.response.cancel()
                await conn.conversation.item.delete(item_id=f"item_{args.marker or 'missing'}")

            async def user(utterance_ms: int, wait_for_quiet: bool) -> None:
                if wait_for_quiet:
                    await asyncio.sleep(playback.remaining_s())
                await speak(conn, utterance_ms, args.mic_pace)

            mic_tasks.append(asyncio.create_task(user(1200, False)))
            async for event in conn:
                etype = event.type
                if etype == "response.output_audio.delta":
                    playback.add(event.item_id, event.delta)
                    # Barge in a second into the long spoken answer.
                    if responses_done == 1 and playback.played_ms() >= 1000 and not turn_cue.is_set():
                        turn_cue.set()
                        mic_tasks.append(asyncio.create_task(user(900, False)))
                elif etype == "input_audio_buffer.speech_started" and playback.started and playback.remaining_s() > 0:
                    # The user talked over audio that had not finished playing.
                    heard = playback.played_ms()
                    playback.stop()
                    await conn.conversation.item.truncate(item_id=playback.item_id, content_index=0, audio_end_ms=heard)
                elif etype == "response.function_call_arguments.done":
                    result = get_weather(**json.loads(event.arguments), marker=args.marker)
                    await conn.conversation.item.create(
                        item={"type": "function_call_output", "call_id": event.call_id, "output": result}
                    )
                    await conn.response.create()
                elif etype == "response.done":
                    responses_done += 1
                    status = event.response.status
                    print(f"response {responses_done}: {status}", flush=True)
                    if responses_done == 3:
                        mic_tasks.append(asyncio.create_task(user(800, True)))
                    elif responses_done == 4:
                        await asyncio.sleep(playback.remaining_s())
                        break
                elif etype == "error":
                    print(f"server error: {event.error.type} {event.error.code}", flush=True)
    finally:
        for task in mic_tasks:
            task.cancel()


async def typed_call(client: AsyncOpenAI, args: argparse.Namespace) -> None:
    """Typed question in, spoken answer out, with a tool call: an audio session that needs no microphone.
    This is the shortest real round trip against the live API."""
    instructions = "You are a concise weather assistant. Use get_weather for any weather question."
    if args.marker:
        instructions += f" Session reference {args.marker}."
    async with client.realtime.connect(model=args.model) as conn:
        await conn.session.update(
            session={"type": "realtime", "instructions": instructions, "tools": TOOLS, "audio": {"output": {"voice": "marin"}}}
        )
        await conn.conversation.item.create(
            item={"type": "message", "role": "user", "content": [{"type": "input_text", "text": args.typed}]}
        )
        await conn.response.create()
        async for event in conn:
            if event.type == "response.function_call_arguments.done":
                result = get_weather(**json.loads(event.arguments), marker=args.marker)
                await conn.conversation.item.create(
                    item={"type": "function_call_output", "call_id": event.call_id, "output": result}
                )
                await conn.response.create()
            elif event.type == "response.done":
                print(f"response: {event.response.status}", flush=True)
                if not any(getattr(item, "type", None) == "function_call" for item in event.response.output or []):
                    break
            elif event.type == "error":
                print(f"server error: {event.error.type} {event.error.code}", flush=True)


async def text_session(client: AsyncOpenAI, args: argparse.Namespace) -> None:
    async with client.realtime.connect(model=args.model) as conn:
        await conn.session.update(session={"type": "realtime", "output_modalities": ["text"], "instructions": "Answer in one line."})
        await conn.conversation.item.create(
            item={"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"Say hello. {args.marker}".strip()}]}
        )
        await conn.response.create()
        async for event in conn:
            if event.type == "response.output_text.done":
                print(f"text: {event.text}", flush=True)
            if event.type == "response.done":
                break


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=os.getenv("FI_PROJECT_NAME", "openai-realtime-example"))
    ap.add_argument("--model", default="gpt-realtime-2.1")
    ap.add_argument("--marker", default="")
    ap.add_argument("--metadata-only", action="store_true")
    ap.add_argument("--user-id", default=None)
    ap.add_argument("--text", action="store_true")
    ap.add_argument("--typed", default=None, help="type this question instead of speaking; the answer is spoken")
    ap.add_argument("--error-case", action="store_true")
    ap.add_argument("--playback-speed", type=float, default=1.0)
    ap.add_argument("--mic-pace", type=float, default=0.02, help="seconds between 100 ms mic chunks")
    args = ap.parse_args()

    provider = register(project_type=ProjectType.OBSERVE, project_name=args.project)
    OpenAIRealtimeInstrumentor().instrument(
        tracer_provider=provider, metadata_only=args.metadata_only, user_id=args.user_id
    )

    client_kwargs: Dict[str, Any] = {}
    if os.getenv("OPENAI_WEBSOCKET_BASE_URL"):
        client_kwargs["websocket_base_url"] = os.environ["OPENAI_WEBSOCKET_BASE_URL"]
    client = AsyncOpenAI(**client_kwargs)
    try:
        if args.text:
            asyncio.run(text_session(client, args))
        elif args.typed:
            asyncio.run(typed_call(client, args))
        else:
            asyncio.run(voice_call(client, args))
    except Exception as exc:  # the trace records it; the example just reports it
        print(f"session ended with {type(exc).__name__}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
