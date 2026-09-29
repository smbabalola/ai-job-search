"""A fixture server for the 6D-B quarantine proofs (spec §22 S2). It serves
tests/webapp/fixtures/fill statically, and RECORDS every request that
reaches /record/<probe> and every WebSocket event on /ws/<name>. Proof of
blocking is absence here: a blocked request never reaches the server."""
from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

FIXTURES = Path(__file__).parent
PORT = 8420  # the one loopback origin in the extension's host_permissions
WS_PORT = 8421  # stdlib WebSocket recorder (uvicorn has no WebSocket library installed here)


@dataclass
class Recorder:
    requests: list[tuple[str, str]] = field(default_factory=list)  # (method, probe)
    ws_events: list[tuple[str, str]] = field(default_factory=list)  # (event, name)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def hit(self, method: str, probe: str) -> None:
        with self.lock:
            self.requests.append((method, probe))

    def ws(self, event: str, name: str) -> None:
        with self.lock:
            self.ws_events.append((event, name))

    def probes(self) -> set[str]:
        with self.lock:
            return {probe for _, probe in self.requests}

    def clear(self) -> None:
        with self.lock:
            self.requests.clear()
            self.ws_events.clear()


def build_app(recorder: Recorder):
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse, RedirectResponse, Response
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles

    async def record(request):
        probe = request.path_params["probe"]
        await request.body()
        recorder.hit(request.method, probe)
        media = "application/javascript" if probe.endswith(".js") else "text/plain"
        return Response("/* recorded */" if media.endswith("javascript") else "ok", media_type=media)

    async def submit(request):
        # 6E-A: an employer application endpoint. A native form POST gets
        # 303 to the confirmation page; an XHR gets 200 JSON.
        tenant, job = request.path_params["tenant"], request.path_params["job"]
        await request.body()
        recorder.hit(request.method, f"submit:{tenant}/{job}")
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return Response('{"ok": true}', media_type="application/json")
        return RedirectResponse(f"/{tenant}/jobs/{job}/confirmation", status_code=303)

    async def confirmation(request):
        tenant, job = request.path_params["tenant"], request.path_params["job"]
        recorder.hit(request.method, f"confirm:{tenant}/{job}")
        return HTMLResponse('<!doctype html><title>Confirmation</title>'
                            '<div id="application_confirmation">Thank you for applying</div>')

    return Starlette(routes=[
        Route("/record/{probe:path}", record, methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]),
        Route("/{tenant}/jobs/{job}", submit, methods=["POST"]),
        Route("/{tenant}/jobs/{job}/confirmation", confirmation, methods=["GET"]),
        Mount("/", app=StaticFiles(directory=str(FIXTURES))),
    ])


class WebSocketRecorder:
    """A minimal RFC 6455 server (stdlib only) on WS_PORT: it records
    connect / message / close per path name /ws/<name>. Only what the
    proofs need: the handshake, masked client frames, and close/EOF."""

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, recorder: Recorder):
        self.recorder = recorder
        self.loop = None
        self.server = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.ready = threading.Event()

    async def _handle(self, reader, writer):
        import asyncio
        import base64
        import hashlib
        name = "?"
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            path = lines[0].split(" ")[1]
            name = path.rsplit("/", 1)[-1]
            headers = {k.strip().lower(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)}
            accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] + self.GUID).encode()).digest())
            writer.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                         b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            await writer.drain()
            self.recorder.ws("connect", name)
            while True:
                b1, b2 = await reader.readexactly(2)
                opcode, length = b1 & 0x0F, b2 & 0x7F
                if length == 126:
                    length = int.from_bytes(await reader.readexactly(2), "big")
                elif length == 127:
                    length = int.from_bytes(await reader.readexactly(8), "big")
                if b2 & 0x80:
                    await reader.readexactly(4)  # client frames are masked
                await reader.readexactly(length)
                if opcode == 0x8:
                    break
                if opcode in (0x1, 0x2):
                    self.recorder.ws("message", name)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            self.recorder.ws("close", name)
            writer.close()

    def _run(self):
        import asyncio
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.server = self.loop.run_until_complete(asyncio.start_server(self._handle, "127.0.0.1", WS_PORT))
        self.ready.set()
        self.loop.run_forever()

    def __enter__(self) -> "WebSocketRecorder":
        self.thread.start()
        if not self.ready.wait(10):
            raise RuntimeError("WebSocket recorder did not start")
        return self

    def __exit__(self, *exc) -> None:
        self.loop.call_soon_threadsafe(self.server.close)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)


class RunningServer:
    def __init__(self, recorder: Recorder, port: int = PORT):
        import uvicorn
        self.recorder = recorder
        self.port = port
        self.server = uvicorn.Server(uvicorn.Config(build_app(recorder), host="127.0.0.1", port=port,
                                                    log_level="warning", access_log=False))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "RunningServer":
        self.ws = WebSocketRecorder(self.recorder).__enter__()
        self.thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.25):
                    return self
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("fixture server did not start")

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self.ws.__exit__()
