"""A local stand-in for the OpenAI Realtime WebSocket, for running without an OpenAI key.

STAND-IN. This is not OpenAI. It speaks the documented GA event vocabulary of the
classic socket (``/v1/realtime``) closely enough for the official ``openai`` SDK
to hold a full session against it, so the instrumentor can be exercised end to
end with no spend. Event names and shapes follow
https://developers.openai.com/api/reference/resources/realtime/server-events
(read 2026-10-15). Transcripts, tool calls, token counts and the voice-activity
detection are scripted, not produced by a model.

What it does:

* sends ``session.created`` on connect and ``session.updated`` for every ``session.update``;
* a toy server VAD: PCM16 frames with signal are speech, zero frames are silence
  (``speech_started`` / ``speech_stopped`` / ``committed``), plus push-to-talk via
  ``input_audio_buffer.commit``;
* input transcription when the session enables it (scripted user lines);
* responses from a script: a function call, then spoken answers streamed as
  ``response.output_audio.delta`` (silent PCM) with transcript deltas, or text
  when the session or the response asks for text only;
* barge-in: speech during a response cancels it (``response.done`` with
  ``status: cancelled``, ``reason: turn_detected``), and ``conversation.item.truncate``
  is answered with ``conversation.item.truncated``;
* errors: ``response.cancel`` with nothing in progress, and ``conversation.item.delete``
  of an unknown item, answer with an ``error`` event;
* ``--drop-after-deltas N``: closes the socket with code 1011 in the middle of the
  second response, after N audio deltas (the socket-drop case);
* rejects the WebSocket upgrade with HTTP 401 when the bearer key starts with ``sk-bad``.

If the client's instructions contain a marker of the form ``FI-MARK-<letters/digits>``,
the scripted transcripts, tool arguments and error messages repeat it, the way a
model repeats what it was told. That is how the privacy check finds content.

Run:  python realtime_stub_server.py --port 8765
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import secrets
import struct
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, Dict, List, Optional

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

MARK = re.compile(r"FI-MARK-[A-Za-z0-9]+")
BYTES_PER_MS = 48  # audio/pcm: 24 kHz, 16-bit mono
CHUNK_MS = 100

USER_LINES = [
    "What's the weather in Paris right now?",
    "Actually, what about tomorrow?",
    "Thanks, that's all.",
]
# One step per response, in order.
ASSISTANT_STEPS: List[Dict[str, Any]] = [
    {"tool": "get_weather", "arguments": {"city": "Paris"}},
    {
        "say": "It is eighteen degrees and sunny in Paris right now, with a light breeze from the west, "
        "which makes it a good afternoon for a walk along the river."
    },
    {"say": "Tomorrow looks cloudy in Paris, around fifteen degrees."},
    {"say": "You're welcome. Goodbye!"},
]


@dataclass
class Config:
    pace: float = 0.02  # seconds of wall time per 100 ms audio chunk (5x faster than real time)
    silence_ms: int = 300
    drop_after_deltas: Optional[int] = None


@dataclass
class Session:
    model: str
    counter: int = 0
    marker: str = ""
    config: Dict[str, Any] = field(default_factory=dict)
    position_ms: float = 0.0
    speaking: bool = False
    speech_start_ms: float = 0.0
    silence_run_ms: float = 0.0
    buffer_bytes: int = 0
    item_id: Optional[str] = None
    user_index: int = 0
    step_index: int = 0
    response_task: Optional["asyncio.Task[None]"] = None
    response_id: Optional[str] = None
    response_cancel: Optional[asyncio.Event] = None
    items: Dict[str, str] = field(default_factory=dict)
    responses_started: int = 0

    def next_id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}_{self.counter:04d}"

    @property
    def text_only(self) -> bool:
        return self.config.get("output_modalities") == ["text"]

    @property
    def transcription_on(self) -> bool:
        audio = self.config.get("audio") or {}
        return bool((audio.get("input") or {}).get("transcription"))

    @property
    def turn_detection(self) -> Optional[Dict[str, Any]]:
        audio = self.config.get("audio") or {}
        inp = audio.get("input") or {}
        return inp.get("turn_detection", {"type": "server_vad"})


class Handler:
    def __init__(self, ws: ServerConnection, cfg: Config) -> None:
        self.ws = ws
        self.cfg = cfg
        path = ws.request.path if ws.request else ""
        model = re.search(r"[?&]model=([^&]+)", path)
        self.s = Session(model=model.group(1) if model else "gpt-realtime-2.1")
        self.s.config = {
            "type": "realtime",
            "object": "realtime.session",
            "id": f"sess_{secrets.token_hex(8)}",
            "model": self.s.model,
            "output_modalities": ["audio"],
            "instructions": "You are a helpful voice assistant.",
            "tools": [],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": None,
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.5,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": cfg.silence_ms,
                        "create_response": True,
                        "interrupt_response": True,
                    },
                },
                "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": "marin", "speed": 1.0},
            },
        }

    def mark(self, text: str) -> str:
        return f"{text} ({self.s.marker})" if self.s.marker else text

    async def emit(self, event: Dict[str, Any]) -> None:
        event = {"event_id": self.s.next_id("event"), **event}
        await self.ws.send(json.dumps(event))

    async def run(self) -> None:
        await self.emit({"type": "session.created", "session": self.s.config})
        try:
            async for raw in self.ws:
                await self.on_client(json.loads(raw))
        except ConnectionClosed:
            pass
        finally:
            if self.s.response_task:
                self.s.response_task.cancel()

    # ---------------------------------------------------------------- client events

    async def on_client(self, ev: Dict[str, Any]) -> None:
        t = ev.get("type")
        if t == "session.update":
            await self.session_update(ev.get("session") or {})
        elif t == "input_audio_buffer.append":
            await self.audio(ev.get("audio") or "")
        elif t == "input_audio_buffer.commit":
            await self.commit(manual=True)
        elif t == "input_audio_buffer.clear":
            self.s.buffer_bytes = 0
            await self.emit({"type": "input_audio_buffer.cleared"})
        elif t == "conversation.item.create":
            item = dict(ev.get("item") or {})
            item.setdefault("id", self.s.next_id("item"))
            self.s.items[item["id"]] = item.get("type", "message")
            await self.emit({"type": "conversation.item.added", "previous_item_id": None, "item": item})
            await self.emit({"type": "conversation.item.done", "previous_item_id": None, "item": item})
        elif t == "conversation.item.truncate":
            await self.emit(
                {
                    "type": "conversation.item.truncated",
                    "item_id": ev.get("item_id"),
                    "content_index": ev.get("content_index", 0),
                    "audio_end_ms": ev.get("audio_end_ms", 0),
                }
            )
        elif t == "conversation.item.delete":
            item_id = ev.get("item_id")
            if item_id in self.s.items:
                del self.s.items[item_id]
                await self.emit({"type": "conversation.item.deleted", "item_id": item_id})
            else:
                await self.error(
                    "invalid_event",
                    f"Error deleting item: the item with id '{item_id}' does not exist.",
                    ev,
                    param="item_id",
                )
        elif t == "response.create":
            await self.start_response(ev.get("response") or {})
        elif t == "response.cancel":
            if self.s.response_task and not self.s.response_task.done():
                self.s.response_cancel.set()
            else:
                await self.error("response_cancel_not_active", "Cancellation failed: no active response found.", ev)

    async def error(self, code: str, message: str, ev: Dict[str, Any], param: Optional[str] = None) -> None:
        await self.emit(
            {
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "code": code,
                    "message": message,
                    "param": param,
                    "event_id": ev.get("event_id"),
                },
            }
        )

    async def session_update(self, update: Dict[str, Any]) -> None:
        instructions = update.get("instructions")
        if isinstance(instructions, str):
            found = MARK.search(instructions)
            self.s.marker = found.group(0) if found else ""
        merged = dict(self.s.config)
        for key, value in update.items():
            if key == "audio" and isinstance(value, dict):
                audio = {k: dict(v) for k, v in (merged.get("audio") or {}).items()}
                for side, conf in value.items():
                    audio.setdefault(side, {}).update(conf or {})
                merged["audio"] = audio
            else:
                merged[key] = value
        self.s.config = merged
        await self.emit({"type": "session.updated", "session": merged})

    # ---------------------------------------------------------------- audio and turns

    async def audio(self, b64: str) -> None:
        data = base64.b64decode(b64) if b64 else b""
        ms = len(data) / BYTES_PER_MS
        self.s.buffer_bytes += len(data)
        detection = self.s.turn_detection
        if detection is None:  # push-to-talk: the client commits
            self.s.position_ms += ms
            return
        samples = struct.unpack(f"<{len(data) // 2}h", data[: len(data) // 2 * 2]) if data else ()
        loud = any(abs(x) > 500 for x in samples)
        if loud and not self.s.speaking:
            self.s.speaking = True
            self.s.speech_start_ms = self.s.position_ms
            self.s.silence_run_ms = 0
            self.s.item_id = self.s.next_id("item")
            await self.emit(
                {
                    "type": "input_audio_buffer.speech_started",
                    "audio_start_ms": int(self.s.speech_start_ms),
                    "item_id": self.s.item_id,
                }
            )
            if self.s.response_task and not self.s.response_task.done():
                self.s.response_cancel.set()  # barge-in: interrupt_response
        elif self.s.speaking and not loud:
            self.s.silence_run_ms += ms
            if self.s.silence_run_ms >= self.cfg.silence_ms:
                self.s.speaking = False
                end = self.s.position_ms + ms - self.s.silence_run_ms
                await self.emit(
                    {"type": "input_audio_buffer.speech_stopped", "audio_end_ms": int(end), "item_id": self.s.item_id}
                )
                self.s.position_ms += ms
                await self.commit(manual=False)
                return
        elif loud:
            self.s.silence_run_ms = 0
        self.s.position_ms += ms

    async def commit(self, *, manual: bool) -> None:
        item_id = self.s.item_id if not manual and self.s.item_id else self.s.next_id("item")
        self.s.item_id = None
        self.s.buffer_bytes = 0
        self.s.items[item_id] = "message"
        await self.emit({"type": "input_audio_buffer.committed", "previous_item_id": None, "item_id": item_id})
        line = USER_LINES[self.s.user_index % len(USER_LINES)]
        self.s.user_index += 1
        if self.s.transcription_on:
            await self.emit(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": item_id,
                    "content_index": 0,
                    "transcript": self.mark(line),
                    "usage": {"type": "duration", "seconds": 2.0},
                }
            )
        detection = self.s.turn_detection or {}
        if not manual and detection.get("create_response", True):
            await self.start_response({})

    # ---------------------------------------------------------------- responses

    async def start_response(self, overrides: Dict[str, Any]) -> None:
        if self.s.response_task and not self.s.response_task.done():
            await self.s.response_task  # one response at a time, as the server does
        self.s.response_cancel = asyncio.Event()
        rid = self.s.next_id("resp")
        self.s.response_id = rid
        self.s.responses_started += 1
        text_only = overrides.get("output_modalities") == ["text"] or self.s.text_only
        step = ASSISTANT_STEPS[self.s.step_index % len(ASSISTANT_STEPS)]
        self.s.step_index += 1
        if "tool" in step and not self.s.config.get("tools"):
            # A model only calls tools the session declared.
            step = ASSISTANT_STEPS[self.s.step_index % len(ASSISTANT_STEPS)]
            self.s.step_index += 1
        self.s.response_task = asyncio.create_task(self.respond(rid, step, text_only))

    def usage(self, out_audio: int, out_text: int) -> Dict[str, Any]:
        inp = 180 + 40 * self.s.responses_started
        return {
            "total_tokens": inp + out_audio + out_text,
            "input_tokens": inp,
            "output_tokens": out_audio + out_text,
            "input_token_details": {"text_tokens": inp - 60, "audio_tokens": 60, "cached_tokens": 0, "image_tokens": 0},
            "output_token_details": {"text_tokens": out_text, "audio_tokens": out_audio},
        }

    async def respond(self, rid: str, step: Dict[str, Any], text_only: bool) -> None:
        modalities = ["text"] if text_only else ["audio"]
        base = {"object": "realtime.response", "id": rid, "conversation_id": "conv_0001", "output_modalities": modalities}
        await self.emit({"type": "response.created", "response": {**base, "status": "in_progress", "output": []}})
        item_id = self.s.next_id("item")
        self.s.items[item_id] = "message"
        if "tool" in step:
            args = dict(step["arguments"])
            if self.s.marker:
                args["ref"] = self.s.marker
            call = {
                "id": item_id,
                "type": "function_call",
                "status": "completed",
                "name": step["tool"],
                "call_id": self.s.next_id("call"),
                "arguments": json.dumps(args),
            }
            await self.emit({"type": "response.output_item.added", "response_id": rid, "output_index": 0, "item": {**call, "status": "in_progress", "arguments": ""}})
            await self.emit({"type": "response.function_call_arguments.delta", "response_id": rid, "item_id": item_id, "output_index": 0, "call_id": call["call_id"], "delta": call["arguments"]})
            await self.emit({"type": "response.function_call_arguments.done", "response_id": rid, "item_id": item_id, "output_index": 0, "call_id": call["call_id"], "name": call["name"], "arguments": call["arguments"]})
            await self.emit({"type": "response.output_item.done", "response_id": rid, "output_index": 0, "item": call})
            await self.emit({"type": "response.done", "response": {**base, "status": "completed", "status_details": None, "output": [call], "usage": self.usage(0, 22)}})
            return
        words = self.mark(step["say"]).split(" ")
        part_type = "output_text" if text_only else "output_audio"
        item = {"id": item_id, "object": "realtime.item", "type": "message", "status": "in_progress", "role": "assistant", "content": []}
        await self.emit({"type": "response.output_item.added", "response_id": rid, "output_index": 0, "item": item})
        await self.emit({"type": "response.content_part.added", "response_id": rid, "item_id": item_id, "output_index": 0, "content_index": 0, "part": {"type": part_type}})
        said: List[str] = []
        cancelled = False
        chunk = base64.b64encode(b"\x00" * (BYTES_PER_MS * CHUNK_MS)).decode()
        deltas = 0
        # About 2.5 words per second of speech, so each 100 ms chunk carries a quarter word.
        chunks = max(1, len(words) * 4)
        for i in range(chunks):
            if self.s.response_cancel.is_set():
                cancelled = True
                break
            if i % 4 == 0 and i // 4 < len(words):
                word = words[i // 4] + (" " if i // 4 < len(words) - 1 else "")
                said.append(word)
                kind = "response.output_text.delta" if text_only else "response.output_audio_transcript.delta"
                await self.emit({"type": kind, "response_id": rid, "item_id": item_id, "output_index": 0, "content_index": 0, "delta": word})
            if not text_only:
                await self.emit({"type": "response.output_audio.delta", "response_id": rid, "item_id": item_id, "output_index": 0, "content_index": 0, "delta": chunk})
                deltas += 1
                if self.cfg.drop_after_deltas and self.s.responses_started >= 2 and deltas >= self.cfg.drop_after_deltas:
                    await self.ws.close(1011, f"upstream inference error {self.s.marker}".strip())
                    return
            await asyncio.sleep(self.cfg.pace)
        text = "".join(said)
        if text_only:
            await self.emit({"type": "response.output_text.done", "response_id": rid, "item_id": item_id, "output_index": 0, "content_index": 0, "text": text})
            content = [{"type": "output_text", "text": text}]
        else:
            await self.emit({"type": "response.output_audio.done", "response_id": rid, "item_id": item_id, "output_index": 0, "content_index": 0})
            await self.emit({"type": "response.output_audio_transcript.done", "response_id": rid, "item_id": item_id, "output_index": 0, "content_index": 0, "transcript": text})
            content = [{"type": "output_audio", "transcript": text}]
        await self.emit({"type": "response.content_part.done", "response_id": rid, "item_id": item_id, "output_index": 0, "content_index": 0, "part": content[0]})
        status = "incomplete" if cancelled else "completed"
        done_item = {**item, "status": status, "content": content}
        await self.emit({"type": "response.output_item.done", "response_id": rid, "output_index": 0, "item": done_item})
        details = {"type": "cancelled", "reason": "turn_detected"} if cancelled else None
        await self.emit(
            {
                "type": "response.done",
                "response": {
                    **base,
                    "status": "cancelled" if cancelled else "completed",
                    "status_details": details,
                    "output": [done_item],
                    "usage": self.usage(0 if text_only else deltas * 2, len(said) * 2),
                },
            }
        )


def make_server(cfg: Config, host: str, port: int):
    async def handler(ws: ServerConnection) -> None:
        await Handler(ws, cfg).run()

    def check_auth(connection: ServerConnection, request: Any):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:].startswith("sk-bad"):
            return connection.respond(HTTPStatus.UNAUTHORIZED, "Incorrect API key provided.\n")
        return None

    return serve(handler, host, port, process_request=check_auth, max_size=None)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--pace", type=float, default=0.02)
    ap.add_argument("--drop-after-deltas", type=int, default=None)
    a = ap.parse_args()
    cfg = Config(pace=a.pace, drop_after_deltas=a.drop_after_deltas)
    async with make_server(cfg, a.host, a.port) as server:
        port = server.sockets[0].getsockname()[1]
        print(f"STAND-IN Realtime server on ws://{a.host}:{port}/v1/realtime", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
