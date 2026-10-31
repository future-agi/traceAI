"""HTTP client hooks for the wire layer: httpx and httpx2 (sync and async), requests and aiohttp.

Each hook only copies bytes the application is already receiving. Bodies the application
reads are passed through unchanged, in the same chunks, and the capture finishes when the
stream is exhausted or closed.
"""

import io
import json
import logging
import os
import stat
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import wrapt
from opentelemetry import context as context_api

from fi_instrumentation.auto.wire import _core
from fi_instrumentation.auto.wire._capture import Capture, Recorder, mark_active, _skip_current_context

logger = logging.getLogger(__name__)
_AIOHTTP_CAPTURE_KEY = context_api.create_key("fi-wire-aiohttp-capture")
_HTTPX_CAPTURE_KEY = context_api.create_key("fi-wire-httpx-capture")
_REQUESTS_REDIRECT_KEY = context_api.create_key("fi-wire-requests-redirect")
_REQUESTS_CAPTURE_KEY = context_api.create_key("fi-wire-requests-eager-capture")


def _guard(fn: Callable[..., Any], *args: Any) -> Any:
    try:
        return fn(*args)
    except Exception:
        logger.debug("traceAI wire layer: %s failed", getattr(fn, "__name__", fn), exc_info=True)
        return None


class _RecorderGate:
    """A removed patch stays inert when an application's outer wrapper retains it."""

    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder
        self.enabled = True

    def begin(self, *args: Any, **kwargs: Any) -> Optional[Capture]:
        return self.recorder.begin(*args, **kwargs) if self.enabled else None

    def observe_credentials(self, headers: Any, url: str = "") -> None:
        if self.enabled:
            self.recorder.observe_credentials(headers, url)


_PATCHES: Dict[Any, Any] = {}


def _install(module: Any, owner: Any, name: str, wrapper: Any) -> None:
    patched = wrapt.FunctionWrapper(getattr(owner, name), wrapper)
    setattr(owner, name, patched)
    _PATCHES[module][1].append((owner, name, patched))


def _new_gate(module: Any, recorder: Recorder) -> _RecorderGate:
    gate = _RecorderGate(recorder)
    _PATCHES[module] = (gate, [])
    return gate


def _unpatch(module: Any) -> None:
    gate, patches = _PATCHES.pop(module, (None, []))
    if gate is not None:
        gate.enabled = False
    for owner, name, patched in reversed(patches):
        # Never peel off an app's wrapper. Arbitrary wrappers may close over the
        # old function, so changing __wrapped__ is not sufficient or safe either.
        if vars(owner).get(name) is patched:
            setattr(owner, name, patched.__wrapped__)


# ---------------------------------------------------------------------------
# httpx
# ---------------------------------------------------------------------------


def _file_size(value: Any) -> Optional[int]:
    """Count in-memory and regular files without reading or seeking their contents."""
    if isinstance(value, (bytes, bytearray, str)):
        return len(value.encode() if isinstance(value, str) else value)
    if isinstance(value, io.BytesIO):
        return value.getbuffer().nbytes - value.tell()
    if isinstance(value, (io.FileIO, io.BufferedReader)):
        try:
            metadata = os.fstat(value.fileno())
            if stat.S_ISREG(metadata.st_mode):
                return max(0, metadata.st_size - value.tell())
        except (OSError, ValueError):
            pass
    return None


def _httpx_request_parts(request: Any) -> Tuple[Optional[bytes], Optional[Dict[str, str]], List[_core.FilePart], Optional[int]]:
    try:
        return request.content, None, [], None
    except Exception:  # httpx.RequestNotRead: streaming or multipart body
        pass
    stream = getattr(request, "stream", None)
    fields = getattr(stream, "fields", None)
    if fields is None:
        return None, None, [], None
    data: Dict[str, str] = {}
    files: List[_core.FilePart] = []
    for index, item in enumerate(fields):
        if index >= 128:
            break
        if hasattr(item, "filename"):
            headers = getattr(item, "headers", {}) or {}
            ctype = next((v for k, v in headers.items() if k.lower() == "content-type"), None)
            size = _file_size(getattr(item, "file", None))
            files.append(_core.FilePart(str(item.name)[:256], str(item.filename)[:1024], ctype[:256] if ctype else None, size))
        else:
            value = item.value
            data[str(item.name)[:256]] = value[:65536].decode("utf-8", "replace") if isinstance(value, bytes) else str(value)[:65536]
    return None, data, files, None


def _httpx_begin(recorder: Recorder, request: Any, client: Any = None,
                 options: Optional[Dict[str, Any]] = None) -> Optional[Capture]:
    if _skip_current_context():
        return None
    body, fields, files, size = _httpx_request_parts(request)
    capture = recorder.begin(request.method, str(request.url), request.headers, body, fields, files, size)
    if capture is not None:
        follow = (options or {}).get("follow_redirects")
        # HTTPX's UseClientDefault sentinel is not a boolean override.
        capture._follow_redirects = follow if isinstance(follow, bool) else getattr(client, "follow_redirects", False)
    return capture


def _httpx_response(capture: Capture, response: Any, httpx: Any, is_async: bool) -> None:
    # Only bodies the client will follow are intermediate. Default HTTPX sends
    # return redirects to the application, which must receive the usual observation.
    if capture._follow_redirects and response.has_redirect_location:
        return
    capture.on_headers(response.status_code, response.headers, decoded=hasattr(response, "_content"))
    if hasattr(response, "_content"):
        capture.on_chunk(response._content)
        capture.finish()
        return
    tee = _async_tee_class(httpx) if is_async else _sync_tee_class(httpx)
    raw_capture = _DecodingCapture(capture)
    response.stream = tee(response.stream, raw_capture)
    original_bytes = response.aiter_bytes if is_async else response.iter_bytes

    def iter_bytes(*args: Any, **kwargs: Any) -> Any:
        iterator = iter(original_bytes(*args, **kwargs))
        try:
            while True:
                raw_capture._self_decoding += 1
                try:
                    chunk = next(iterator)
                except StopIteration:
                    capture.finish()
                    return
                except Exception as error:
                    capture.fail(error)
                    raise
                finally:
                    raw_capture._self_decoding -= 1
                # Decoding is suspended here. A consumer close must finalize now,
                # even if this generator will not be finalized until a later GC.
                yield chunk
        finally:
            capture.finish(incomplete=True)

    async def aiter_bytes(*args: Any, **kwargs: Any) -> Any:
        iterator = original_bytes(*args, **kwargs).__aiter__()
        try:
            while True:
                raw_capture._self_decoding += 1
                try:
                    chunk = await iterator.__anext__()
                except StopAsyncIteration:
                    capture.finish()
                    return
                except Exception as error:
                    capture.fail(error)
                    raise
                finally:
                    raw_capture._self_decoding -= 1
                yield chunk
        finally:
            capture.finish(incomplete=True)

    if is_async:
        response.aiter_bytes = aiter_bytes
    else:
        response.iter_bytes = iter_bytes


class _DecodingCapture(wrapt.ObjectProxy):
    """Raw EOF is not decoded EOF: the HTTPX decoder can still fail on flush."""

    def __init__(self, capture: Capture) -> None:
        super().__init__(capture)
        self._self_decoding = 0

    def finish(self, incomplete: bool = False) -> None:
        if not self._self_decoding:
            self.__wrapped__.finish(incomplete=incomplete)


_TEE_CLASSES: Dict[str, Any] = {}


def _tee_iterator(chunks: Any, capture: Capture, record: bool = True) -> Any:
    iterator = iter(chunks)
    try:
        while True:
            try:
                chunk = next(iterator)
            except StopIteration:
                capture.finish()
                return
            except Exception as error:
                capture.fail(error)
                raise
            if record:
                capture.on_chunk(chunk)
            # Consumer exceptions injected at yield are not transport failures.
            yield chunk
    finally:
        capture.finish(incomplete=True)


async def _tee_async_iterator(chunks: Any, capture: Capture, record: bool = True) -> Any:
    iterator = chunks.__aiter__()
    try:
        while True:
            try:
                chunk = await iterator.__anext__()
            except StopAsyncIteration:
                capture.finish()
                return
            except Exception as error:
                capture.fail(error)
                raise
            if record:
                capture.on_chunk(chunk)
            yield chunk
    finally:
        capture.finish(incomplete=True)


def _sync_tee_class(httpx: Any) -> Any:
    key = f"{httpx.__name__}.sync"
    if key not in _TEE_CLASSES:

        class _SyncTee(httpx.SyncByteStream):  # type: ignore[misc, name-defined]
            def __init__(self, inner: Any, capture: Capture) -> None:
                self._inner = inner
                self._capture = capture

            def __iter__(self) -> Any:
                return _tee_iterator(self._inner, self._capture)

            def close(self) -> None:
                try:
                    self._inner.close()
                except Exception as error:
                    self._capture.fail(error)
                    raise
                finally:
                    self._capture.finish(incomplete=True)

        _TEE_CLASSES[key] = _SyncTee
    return _TEE_CLASSES[key]


def _async_tee_class(httpx: Any) -> Any:
    key = f"{httpx.__name__}.async"
    if key not in _TEE_CLASSES:

        class _AsyncTee(httpx.AsyncByteStream):  # type: ignore[misc, name-defined]
            def __init__(self, inner: Any, capture: Capture) -> None:
                self._inner = inner
                self._capture = capture

            def __aiter__(self) -> Any:
                return _tee_async_iterator(self._inner, self._capture)

            async def aclose(self) -> None:
                try:
                    await self._inner.aclose()
                except Exception as error:
                    self._capture.fail(error)
                    raise
                finally:
                    self._capture.finish(incomplete=True)

        _TEE_CLASSES[key] = _AsyncTee
    return _TEE_CLASSES[key]


def _httpx_dispatch_credentials(recorder: Recorder, request: Any) -> None:
    # HTTPX auth flows and request hooks run after Client.send starts. Observe the
    # actual request immediately before each dispatch, including retries/redirects
    # and calls whose model span suppresses a separate wire capture.
    recorder.observe_credentials(request.headers, str(request.url))


def patch_httpx(recorder: Recorder, httpx: Any) -> None:
    recorder = _new_gate(httpx, recorder)

    def single(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        request = args[0] if args else kwargs.get("request")
        if request is not None:
            _guard(_httpx_dispatch_credentials, recorder, request)
        response = wrapped(*args, **kwargs)
        capture = context_api.get_value(_HTTPX_CAPTURE_KEY)
        if capture is not None:
            _guard(_httpx_response, capture, response, httpx, False)
        return response

    async def async_single(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        request = args[0] if args else kwargs.get("request")
        if request is not None:
            _guard(_httpx_dispatch_credentials, recorder, request)
        response = await wrapped(*args, **kwargs)
        capture = context_api.get_value(_HTTPX_CAPTURE_KEY)
        if capture is not None:
            _guard(_httpx_response, capture, response, httpx, True)
        return response

    def send(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        started = (time.time_ns(), time.perf_counter())
        request = args[0] if args else kwargs.get("request")
        capture = _guard(_httpx_begin, recorder, request, instance, kwargs) if request is not None else None
        if capture is not None:
            capture._start_ns, capture._start_perf = started
        token = mark_active() if capture is not None else None
        pending = context_api.attach(context_api.set_value(_HTTPX_CAPTURE_KEY, capture))
        try:
            # Leave send arguments, eager reads, response hooks and cleanup to HTTPX.
            return wrapped(*args, **kwargs)
        except BaseException as error:
            # A request hook/auth flow can add a secret then raise before dispatch.
            # Observe those headers before the wire/native exception is exported.
            if request is not None:
                _guard(_httpx_dispatch_credentials, recorder, request)
            if capture is not None:
                capture.fail(error)
            raise
        finally:
            context_api.detach(pending)
            if token is not None:
                context_api.detach(token)

    async def async_send(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        started = (time.time_ns(), time.perf_counter())
        request = args[0] if args else kwargs.get("request")
        capture = _guard(_httpx_begin, recorder, request, instance, kwargs) if request is not None else None
        if capture is not None:
            capture._start_ns, capture._start_perf = started
        token = mark_active() if capture is not None else None
        pending = context_api.attach(context_api.set_value(_HTTPX_CAPTURE_KEY, capture))
        try:
            return await wrapped(*args, **kwargs)
        except BaseException as error:
            if request is not None:
                _guard(_httpx_dispatch_credentials, recorder, request)
            if capture is not None:
                capture.fail(error)
            raise
        finally:
            context_api.detach(pending)
            if token is not None:
                context_api.detach(token)

    _install(httpx, httpx.Client, "send", send)
    _install(httpx, httpx.AsyncClient, "send", async_send)
    _install(httpx, httpx.Client, "_send_single_request", single)
    _install(httpx, httpx.AsyncClient, "_send_single_request", async_single)


def unpatch_httpx(httpx: Any) -> None:
    _unpatch(httpx)


# ---------------------------------------------------------------------------
# requests
# ---------------------------------------------------------------------------


def _requests_begin(recorder: Recorder, request: Any) -> Optional[Capture]:
    if _skip_current_context():
        return None
    body = request.body
    if isinstance(body, str):
        body = body.encode("utf-8")
    elif not isinstance(body, (bytes, bytearray, type(None))):
        body = None  # generators and file objects are left alone
    return recorder.begin(request.method or "GET", request.url, request.headers, body)


class _RawTee(wrapt.ObjectProxy):  # type: ignore[misc]
    """Observe urllib3 raw reads as well as Requests' decoded iter_content path."""

    def __init__(self, inner: Any, capture: Capture) -> None:
        super().__init__(inner)
        self._self_capture = capture

    def _self_record(self, data: bytes, amount: Any, decoded: Any) -> bytes:
        try:
            self._self_capture._response.decoded = bool(
                getattr(self.__wrapped__, "decode_content", False) if decoded is None else decoded
            )
            self._self_capture.on_chunk(data)
            if amount != 0:
                closed = getattr(self.__wrapped__, "isclosed", lambda: False)()
                buffered = len(getattr(self.__wrapped__, "_decoded_buffer", b""))
                decoder = getattr(self.__wrapped__, "_decoder", None)
                pending = getattr(decoder, "has_unconsumed_tail", False)
                if not data or amount is None or amount == -1 or closed and not buffered and not pending:
                    self._self_capture.finish()
        except Exception:
            logger.debug("traceAI wire layer: raw read inspection failed", exc_info=True)
        return data

    def _self_read(self, name: str, *args: Any, **kwargs: Any) -> bytes:
        try:
            data = getattr(self.__wrapped__, name)(*args, **kwargs)
        except Exception as error:
            self._self_capture.fail(error)
            raise
        amount = args[0] if args else kwargs.get("amt")
        decoded = args[1] if len(args) > 1 else kwargs.get("decode_content")
        return self._self_record(data, amount, decoded)

    def read(self, *args: Any, **kwargs: Any) -> bytes:
        return self._self_read("read", *args, **kwargs)

    def _self_partial_read(self, name: str, *args: Any, **kwargs: Any) -> bytes:
        try:
            data = getattr(self.__wrapped__, name)(*args, **kwargs)
        except Exception as error:
            self._self_capture.fail(error)
            raise
        amount = args[0] if args else kwargs.get("amt", kwargs.get("size", -1))
        decoded = args[1] if len(args) > 1 else kwargs.get("decode_content")
        return self._self_record(data, 1 if amount in (None, -1) else amount, decoded)

    def read1(self, *args: Any, **kwargs: Any) -> bytes:
        # read1(-1) can return only one buffered chunk, unlike read(-1).
        return self._self_partial_read("read1", *args, **kwargs)

    def readline(self, *args: Any, **kwargs: Any) -> bytes:
        return self._self_partial_read("readline", *args, **kwargs)

    def readall(self) -> bytes:
        return self.read()

    def __iter__(self) -> Any:
        while True:
            line = self.readline()
            if not line:
                return
            yield line

    @property
    def data(self) -> bytes:
        try:
            data = self.__wrapped__.data
        except Exception as error:
            self._self_capture.fail(error)
            raise
        return self._self_record(data, None, None)

    def readinto(self, buffer: Any) -> int:
        try:
            size = self.__wrapped__.readinto(buffer)
        except Exception as error:
            self._self_capture.fail(error)
            raise
        self._self_record(bytes(memoryview(buffer)[:size]), len(buffer), None)
        return size

    def stream(self, amt: Any = 65536, decode_content: Any = None) -> Any:
        self._self_capture._response.decoded = bool(
            getattr(self.__wrapped__, "decode_content", False) if decode_content is None else decode_content
        )
        # Observe raw EOF/decoder buffers too: the final decoded chunk can be
        # complete before a consumer asks the generator for its next item.
        chunks = (self._self_record(chunk, 1, decode_content)
                  for chunk in self.__wrapped__.stream(amt, decode_content=decode_content))
        yield from _tee_iterator(chunks, self._self_capture, record=False)

    def close(self) -> None:
        try:
            self.__wrapped__.close()
        except Exception as error:
            self._self_capture.fail(error)
            raise
        finally:
            self._self_capture.finish(incomplete=True)


def _requests_response(capture: Capture, response: Any, streamed: bool) -> None:
    capture.on_headers(response.status_code, response.headers, decoded=True)
    if not streamed or response._content is not False:
        capture.on_chunk(response.content)
        capture.finish()
        return
    original_iter = response.iter_content
    original_close = response.close
    raw = getattr(response, "raw", None)
    if raw is not None:
        response.raw = _RawTee(raw, capture)

    def iter_content(chunk_size: int = 1, decode_unicode: bool = False) -> Any:
        # With urllib3, the raw tee has already copied these bytes.
        yield from _tee_iterator(original_iter(chunk_size, decode_unicode), capture, record=raw is None)

    def close() -> None:
        try:
            original_close()
        except Exception as error:
            capture.fail(error)
            raise
        finally:
            capture.finish(incomplete=True)

    response.iter_content = iter_content
    response.close = close


def patch_requests(recorder: Recorder, requests: Any) -> None:
    recorder = _new_gate(requests, recorder)

    def send(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        started = (time.time_ns(), time.perf_counter())
        request = args[0] if args else kwargs.get("request")
        capture = _guard(_requests_begin, recorder, request) if request is not None else None
        if capture is None:
            parent = context_api.get_value(_REQUESTS_CAPTURE_KEY)
            if parent is not None and context_api.get_value(_REQUESTS_REDIRECT_KEY):
                parent._requests_request = request
                return wrapped(*args, **kwargs)
            # A response hook may make its own request. Its body is not the parent's.
            token = context_api.attach(context_api.set_value(_REQUESTS_CAPTURE_KEY, None))
            try:
                return wrapped(*args, **kwargs)
            finally:
                context_api.detach(token)
        capture._start_ns, capture._start_perf = started
        token = mark_active()
        capture._requests_request = request
        eager_token = context_api.attach(context_api.set_value(_REQUESTS_CAPTURE_KEY, capture))
        try:
            response = wrapped(*args, **kwargs)
        except BaseException as error:
            capture.fail(error)
            raise
        finally:
            context_api.detach(eager_token)
            context_api.detach(token)
        _guard(_requests_response, capture, response, bool(kwargs.get("stream")))
        return response

    def adapter_send(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        request = args[0] if args else kwargs.get("request")
        if request is not None:
            _guard(recorder.observe_credentials, request.headers, request.url)
            capture = context_api.get_value(_REQUESTS_CAPTURE_KEY)
            if capture is not None:
                # DigestAuth retries directly through the adapter, not Session.send.
                capture._requests_request = request
        return wrapped(*args, **kwargs)

    def dispatch_hook(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        capture = context_api.get_value(_REQUESTS_CAPTURE_KEY)
        if capture is None:
            return wrapped(*args, **kwargs)
        previous = getattr(capture, "_requests_in_hooks", False)
        capture._requests_in_hooks = True
        try:
            return wrapped(*args, **kwargs)
        finally:
            capture._requests_in_hooks = previous

    def eager_content(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        capture = context_api.get_value(_REQUESTS_CAPTURE_KEY)
        # Session.send eagerly reads through iter_content before it returns. Observe that
        # read without changing its stream argument (response hooks receive that argument).
        # Redirect bodies belong to the client's redirect machinery, not the final output.
        if (not recorder.enabled or capture is None or 300 <= instance.status_code < 400
                # Authentication hooks consume the challenge body before retrying.
                # It is not the final response; final 401s are read after the hooks.
                or instance.status_code == 401 and getattr(capture, "_requests_in_hooks", False)
                or instance.request is not getattr(capture, "_requests_request", None)):
            yield from wrapped(*args, **kwargs)
            return
        capture.on_headers(instance.status_code, instance.headers, decoded=True)
        yield from _tee_iterator(wrapped(*args, **kwargs), capture)

    def redirects(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        iterator = iter(wrapped(*args, **kwargs))
        while True:
            token = context_api.attach(context_api.set_value(_REQUESTS_REDIRECT_KEY, True))
            try:
                response = next(iterator)
            except StopIteration:
                return
            finally:
                context_api.detach(token)
            yield response

    _install(requests, requests.adapters.HTTPAdapter, "send", adapter_send)
    _install(requests, requests.sessions, "dispatch_hook", dispatch_hook)
    _install(requests, requests.Session, "send", send)
    _install(requests, requests.Session, "resolve_redirects", redirects)
    _install(requests, requests.models.Response, "iter_content", eager_content)


def unpatch_requests(requests: Any) -> None:
    _unpatch(requests)


# ---------------------------------------------------------------------------
# aiohttp
# ---------------------------------------------------------------------------


class _ContentTee(wrapt.ObjectProxy):  # type: ignore[misc]
    """Proxy for ``ClientResponse.content`` that copies every byte the application reads."""

    def __init__(self, inner: Any, capture: Capture) -> None:
        super().__init__(inner)
        self._self_capture = capture

    def _self_record(self, data: Any) -> Any:
        if data:
            self._self_capture.on_chunk(data)
        if self.__wrapped__.at_eof():
            self._self_capture.finish()
        return data

    async def _self_read(self, name: str, *args: Any) -> Any:
        try:
            return await getattr(self.__wrapped__, name)(*args)
        except Exception as error:
            partial = getattr(error, "partial", None)
            if partial:
                self._self_capture.on_chunk(partial)
            self._self_capture.fail(error)
            raise

    async def read(self, n: int = -1) -> bytes:
        return self._self_record(await self._self_read("read", n))

    async def readany(self) -> bytes:
        return self._self_record(await self._self_read("readany"))

    async def readline(self) -> bytes:
        return self._self_record(await self._self_read("readline"))

    async def readuntil(self, separator: bytes = b"\n") -> bytes:
        return self._self_record(await self._self_read("readuntil", separator))

    async def readexactly(self, n: int) -> bytes:
        return self._self_record(await self._self_read("readexactly", n))

    async def readchunk(self) -> Tuple[bytes, bool]:
        data, end = await self._self_read("readchunk")
        self._self_record(data)
        return data, end

    def read_nowait(self, n: int = -1) -> bytes:
        try:
            return self._self_record(self.__wrapped__.read_nowait(n))
        except Exception as error:
            self._self_capture.fail(error)
            raise

    def __aiter__(self) -> Any:
        return self._self_iterate(self.readline)

    def iter_chunked(self, n: int) -> Any:
        return self._self_iterate(lambda: self.read(n))

    def iter_any(self) -> Any:
        return self._self_iterate(self.readany)

    async def iter_chunks(self) -> Any:
        while True:
            data, end = await self.readchunk()
            if not data and not end:
                return
            yield data, end

    async def _self_iterate(self, read: Callable[[], Any]) -> Any:
        while True:
            data = await read()
            if not data:
                return
            yield data


def _aiohttp_begin(recorder: Recorder, method: str, url: Any, kwargs: Dict[str, Any]) -> Optional[Capture]:
    if _skip_current_context():
        return None
    body: Optional[bytes] = None
    headers = dict(kwargs.get("headers") or {})
    if kwargs.get("json") is not None:
        try:
            body = json.dumps(kwargs["json"]).encode("utf-8")
        except Exception:
            pass  # a custom session serializer may support objects the hook cannot copy
        headers.setdefault("Content-Type", "application/json")
    elif isinstance(kwargs.get("data"), (bytes, bytearray)):
        body = bytes(kwargs["data"])
    elif isinstance(kwargs.get("data"), str):
        body = kwargs["data"].encode("utf-8")
    fields, files = None, []
    data = kwargs.get("data")
    # Inspect FormData's already-built descriptors; never call it (one-shot) or read files.
    form_fields = getattr(data, "_fields", None)
    if isinstance(form_fields, list):
        fields = {}
        headers.setdefault("Content-Type", "multipart/form-data")
        for options, part_headers, value in form_fields[:128]:
            name = str(options.get("name", ""))[:256]
            filename = options.get("filename")
            if filename is not None or isinstance(value, io.IOBase):
                size = _file_size(value)
                files.append(_core.FilePart(name, str(filename)[:1024] if filename else None,
                                           part_headers.get("Content-Type"), size))
            elif isinstance(value, (str, bytes, bytearray)):
                fields[name] = value[:65536].decode("utf-8", "replace") if not isinstance(value, str) else value[:65536]
    params = kwargs.get("params")
    full_url = str(url)
    if params:
        try:
            import yarl

            full_url = str(yarl.URL(full_url).update_query(params))
        except Exception:
            pass
    capture = recorder.begin(method, full_url, headers, body, fields, files)
    if capture is not None:
        capture._follow_redirects = kwargs.get("allow_redirects", True)
    return capture


def _aiohttp_response(capture: Capture, response: Any, decoded: bool = True) -> None:
    if (capture._follow_redirects and response.status in (301, 302, 303, 307, 308)
            and (response.headers.get("Location") or response.headers.get("URI"))):
        return
    capture.on_headers(response.status, response.headers, decoded=decoded)
    # aiohttp's async raise_for_status callback runs before _request returns and
    # may have read/cache-filled the body already. Never read its stream again.
    if getattr(response, "_body", None) is not None:
        capture.on_chunk(response._body)
        capture.finish()
        return
    response.content = _ContentTee(response.content, capture)

    def wrap_close(original: Any) -> Any:
        def close() -> Any:
            incomplete = not response.content.at_eof()
            try:
                return original()
            except Exception as error:
                capture.fail(error)
                raise
            finally:
                capture.finish(incomplete=incomplete)
        return close

    response.release = wrap_close(response.release)
    response.close = wrap_close(response.close)


def patch_aiohttp(recorder: Recorder, aiohttp: Any) -> None:
    recorder = _new_gate(aiohttp, recorder)

    async def dispatch(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        # ClientRequest exists only after TraceConfig.on_request_start, URL resolution,
        # cookies and auth. Observe before sending, even when no wire span is wanted.
        _guard(recorder.observe_credentials, instance.headers, str(instance.url))
        try:
            return await wrapped(*args, **kwargs)
        finally:
            # on_request_headers_sent callbacks may mutate this same header mapping.
            # Register those values before ClientResponse.start or native span export.
            _guard(recorder.observe_credentials, instance.headers, str(instance.url))

    async def start(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        response = await wrapped(*args, **kwargs)
        pending = context_api.get_value(_AIOHTTP_CAPTURE_KEY)
        if pending is not None:
            capture, decoded = pending
            _guard(_aiohttp_response, capture, response, decoded)
        return response

    async def request(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        started = (time.time_ns(), time.perf_counter())
        method = args[0] if args else kwargs.get("method", "GET")
        url = args[1] if len(args) > 1 else kwargs.get("str_or_url")
        capture_kwargs = dict(kwargs)
        headers = dict(getattr(instance, "headers", {}) or {})
        headers.update(kwargs.get("headers") or {})
        auth = kwargs.get("auth") or getattr(instance, "_default_auth", None)
        if auth is not None and isinstance(auth, aiohttp.BasicAuth):
            headers.setdefault("Authorization", auth.encode())
        capture_kwargs["headers"] = headers
        # Use aiohttp's own non-I/O resolver so relative requests include base_url.
        build_url = getattr(instance, "_build_url", None)
        capture_url = _guard(build_url, url) if callable(build_url) else url
        capture = _guard(_aiohttp_begin, recorder, str(method), capture_url or url, capture_kwargs)
        if capture is None:
            pending = context_api.attach(context_api.set_value(_AIOHTTP_CAPTURE_KEY, None))
            try:
                return await wrapped(*args, **kwargs)
            finally:
                context_api.detach(pending)
        capture._start_ns, capture._start_perf = started
        token = mark_active()
        pending = context_api.attach(context_api.set_value(_AIOHTTP_CAPTURE_KEY, (
            capture, kwargs.get("auto_decompress", getattr(instance, "_auto_decompress", True)))))
        callback = kwargs.get("raise_for_status", getattr(instance, "_raise_for_status", False))
        callback_response = []
        if callable(callback):
            async def observed_callback(response: Any) -> Any:
                callback_response.append(response)
                try:
                    return await callback(response)
                except BaseException:
                    capture.finish(incomplete=not response.content.at_eof())
                    raise
            kwargs = {**kwargs, "raise_for_status": observed_callback}
        try:
            response = await wrapped(*args, **kwargs)
        except BaseException as error:
            if not callback_response:
                capture.fail(error)
            raise
        finally:
            context_api.detach(pending)
            context_api.detach(token)
        return response

    _install(aiohttp, aiohttp.ClientRequest, "send", dispatch)
    _install(aiohttp, aiohttp.ClientSession, "_request", request)
    _install(aiohttp, aiohttp.ClientResponse, "start", start)


def unpatch_aiohttp(aiohttp: Any) -> None:
    _unpatch(aiohttp)


TRANSPORTS: Dict[str, Tuple[Callable[[Recorder, Any], None], Callable[[Any], None]]] = {
    "httpx": (patch_httpx, unpatch_httpx),
    # httpx 2 ships as its own package; the openai and anthropic SDKs moved to it.
    "httpx2": (patch_httpx, unpatch_httpx),
    "requests": (patch_requests, unpatch_requests),
    "aiohttp": (patch_aiohttp, unpatch_aiohttp),
}
