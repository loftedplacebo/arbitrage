from __future__ import annotations

import json
import select
import socket
import socketserver
import threading
from datetime import datetime
from typing import Callable

from core.models import OrderBook


def orderbook_to_payload(orderbook: OrderBook) -> dict:
    return {
        "exchange": orderbook.exchange,
        "symbol": orderbook.standard_symbol,
        "exchange_symbol": orderbook.exchange_symbol,
        "market_type": orderbook.market_type,
        "observed_at_utc": orderbook.observed_at_utc.isoformat(),
        "bids": [[level.price, level.quantity] for level in orderbook.bids],
        "asks": [[level.price, level.quantity] for level in orderbook.asks],
    }


class _EventRequestHandler(socketserver.StreamRequestHandler):
    def setup(self) -> None:
        super().setup()
        self.server.hub._register(self.request)  # type: ignore[attr-defined]

    def handle(self) -> None:
        while not self.server.hub.stopped:  # type: ignore[attr-defined]
            raw = self.rfile.readline()
            if not raw:
                break
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if message.get("type") != "subscribe":
                self.server.hub._handle_control(message)  # type: ignore[attr-defined]

    def finish(self) -> None:
        self.server.hub._unregister(self.request)  # type: ignore[attr-defined]
        super().finish()


class _ThreadingEventServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class LocalEventPublisher:
    """Small localhost JSON-lines publisher for scanner-to-strategy events."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        on_control: Callable[[dict], None] | None = None,
    ):
        self.host = host
        self.port = port
        self.on_control = on_control
        self._clients: set[socket.socket] = set()
        self._clients_lock = threading.RLock()
        self._server: _ThreadingEventServer | None = None
        self._thread: threading.Thread | None = None
        self.stopped = False

    def start(self) -> None:
        if self._server is not None:
            return
        self._server = _ThreadingEventServer((self.host, self.port), _EventRequestHandler)
        self._server.hub = self  # type: ignore[attr-defined]
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stopped = True
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._server = None
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            try:
                client.close()
            except OSError:
                pass

    def publish(self, channel: str, payload: dict) -> None:
        message = json.dumps(
            {"type": "event", "channel": channel, "payload": payload},
            separators=(",", ":"),
            default=_json_default,
        ).encode("utf-8") + b"\n"
        # Websocket callbacks can publish concurrently. Hold this lock while
        # writing a complete newline-delimited payload to each local client.
        with self._clients_lock:
            for client in list(self._clients):
                try:
                    client.sendall(message)
                except OSError:
                    self._clients.discard(client)

    def _register(self, client: socket.socket) -> None:
        client.settimeout(1.0)
        with self._clients_lock:
            self._clients.add(client)

    def _unregister(self, client: socket.socket) -> None:
        with self._clients_lock:
            self._clients.discard(client)

    def _handle_control(self, message: dict) -> None:
        if self.on_control is not None:
            self.on_control(message)


class LocalEventClient:
    def __init__(self, *, host: str = "127.0.0.1", port: int = 8765):
        self.host = host
        self.port = port
        self._socket: socket.socket | None = None
        self._buffer = b""

    def connect(self, timeout_seconds: float = 2.0) -> None:
        self.close()
        self._socket = socket.create_connection((self.host, self.port), timeout=timeout_seconds)
        self._socket.setblocking(False)
        self.send({"type": "subscribe"})

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
        self._socket = None
        self._buffer = b""

    def send(self, message: dict) -> None:
        if self._socket is None:
            raise ConnectionError("event client is not connected")
        self._socket.sendall(json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n")

    def receive(self, timeout_seconds: float) -> list[dict]:
        if self._socket is None:
            raise ConnectionError("event client is not connected")
        readable, _, _ = select.select([self._socket], [], [], timeout_seconds)
        if not readable:
            return []
        chunk = self._socket.recv(1_000_000)
        if not chunk:
            raise ConnectionError("event publisher disconnected")
        self._buffer += chunk
        rows = self._buffer.split(b"\n")
        self._buffer = rows.pop()
        messages = []
        for row in rows:
            try:
                messages.append(json.loads(row.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        return messages


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Cannot serialise {type(value)!r}")
