"""Client for the local Herdr socket (newline-delimited JSON).

Herdr closes a connection after answering one request, so every request
opens its own connection. An events.subscribe connection stays open and
streams events; it is replaced whenever the subscription set changes.
"""
import asyncio
import json
import os
from pathlib import Path

LINE_LIMIT = 16 * 1024 * 1024  # one reply is one line, and a screen or a snapshot easily outgrows asyncio's 64 KB


class HerdrError(RuntimeError):
    pass


class HerdrUnavailable(HerdrError):
    """No usable answer at all: no socket, nothing back in time, or something that is not a reply."""

    def __init__(self, message="Herdr is not reachable"):
        super().__init__(message)


class HerdrClient:
    def __init__(self, socket_path=None):
        self.socket_path = socket_path or os.environ.get("HERDR_SOCKET_PATH") or str(Path.home() / ".config/herdr/herdr.sock")
        self.events = asyncio.Queue()
        self.counter = 0
        self.stream_task = None

    async def call(self, method, params=None, timeout=15):
        self.counter += 1
        msg_id = f"ulak:{self.counter}"
        reader, writer = await asyncio.open_unix_connection(self.socket_path, limit=LINE_LIMIT)
        try:
            writer.write((json.dumps({"id": msg_id, "method": method, "params": params or {}}) + "\n").encode())
            await writer.drain()
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout)
                if not line:
                    raise HerdrError("Herdr socket closed")
                msg = json.loads(line)
                if msg.get("id") != msg_id:
                    continue
                if "error" in msg:
                    raise HerdrError(msg["error"].get("message", "Herdr error"))
                return msg.get("result", {})
        finally:
            writer.close()

    request = call

    async def subscribe(self, subscriptions):
        """(Re)open the event stream with the full subscription list."""
        if self.stream_task:
            self.stream_task.cancel()
        self.stream_task = asyncio.create_task(self._stream(list(subscriptions)))

    async def _stream(self, subscriptions):
        while True:
            try:
                reader, writer = await asyncio.open_unix_connection(self.socket_path, limit=LINE_LIMIT)
                try:
                    writer.write((json.dumps({"id": "ulak:events", "method": "events.subscribe", "params": {"subscriptions": subscriptions}}) + "\n").encode())
                    await writer.drain()
                    while line := await reader.readline():
                        msg = json.loads(line)
                        if msg.get("id") == "ulak:events":
                            continue
                        await self.events.put(msg)
                finally:
                    writer.close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.events.put({"_error": str(exc)})
            await asyncio.sleep(2)

    async def close(self):
        if self.stream_task:
            self.stream_task.cancel()
