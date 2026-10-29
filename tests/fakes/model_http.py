"""Serve scripted actual HTTP bytes to the installed model transport in tests."""

import asyncio
from collections import deque
from dataclasses import dataclass
import json

@dataclass(frozen=True)
class WireRequest:
    method: str
    target: str
    headers: dict[str, str]
    content: bytes


@dataclass(frozen=True)
class Reply:
    status: int = 200
    body: bytes = b""
    headers: tuple[tuple[str, str], ...] = (("content-type", "application/json"),)
    wait_for: asyncio.Event | None = None
    disconnect: bool = False
    declared_bytes: int | None = None


class LoopbackHTTP:
    """One-response-per-connection HTTP/1.1 server for the SDK's real socket path."""
    def __init__(self, script):
        self.script = deque(script)
        self.requests = []
        self.request_seen = asyncio.Event()
        self._server = None
        self._connections = set()

    async def start(self):
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        host, port = self._server.sockets[0].getsockname()[:2]
        self.base_url = f"http://{host}:{port}/v1"
        return self

    async def _accept(self, reader, writer):
        task = asyncio.current_task()
        self._connections.add(task)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, target, _version = lines[0].split(" ", 2)
            headers = {}
            for line in lines[1:]:
                if not line:
                    continue
                name, value = line.split(":", 1)
                headers[name.lower()] = value.strip()
            length = int(headers.get("content-length", "0"))
            body = await reader.readexactly(length) if length else b""
            request = WireRequest(method, target, headers, body)
            self.requests.append(request)
            self.request_seen.set()
            step = self.script.popleft() if self.script else Reply(500, b"{}")
            if callable(step):
                step = step(request, len(self.requests) - 1)
                if asyncio.iscoroutine(step):
                    step = await step
            if isinstance(step, BaseException):
                writer.close()
                await writer.wait_closed()
                return
            if step.wait_for is not None:
                await step.wait_for.wait()
            if step.disconnect:
                writer.close()
                await writer.wait_closed()
                return
            reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 408: "Request Timeout",
                      409: "Conflict", 429: "Too Many Requests", 500: "Server Error",
                      502: "Bad Gateway", 503: "Unavailable", 504: "Gateway Timeout"}.get(step.status, "Response")
            response_headers = list(step.headers) + [("content-length", str(len(step.body) if step.declared_bytes is None else step.declared_bytes)), ("connection", "close")]
            writer.write((f"HTTP/1.1 {step.status} {reason}\r\n" +
                          "".join(f"{key}: {value}\r\n" for key, value in response_headers) + "\r\n").encode("latin-1") + step.body)
            await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            self._connections.discard(task)

    async def close(self):
        self._server.close()
        tasks = tuple(self._connections)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._server.wait_closed()


def reply(document, *, status=200, headers=()):
    body = document if isinstance(document, bytes) else json.dumps(document, separators=(",", ":")).encode()
    return Reply(status=status, body=body, headers=(("content-type", "application/json"), *headers))
