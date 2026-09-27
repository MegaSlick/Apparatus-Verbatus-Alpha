"""Test support shared by operations packages."""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def dribbling_loopback_server(status: bytes, *, headers_slowly: bool) -> Iterator[str]:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    stop = threading.Event()

    def serve() -> None:
        try:
            connection, _ = listener.accept()
        except OSError:
            return
        with connection:
            try:
                connection.recv(65536)
                if headers_slowly:
                    connection.sendall(b"HTTP/1.1 " + status + b"\r\n")
                    while not stop.wait(0.05):
                        connection.sendall(b"X-Pad: pad\r\n")
                else:
                    connection.sendall(
                        b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\n"
                        b"Content-Length: 4096\r\n\r\n"
                    )
                    while not stop.wait(0.05):
                        connection.sendall(b"x")
            except OSError:
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=5.0)
