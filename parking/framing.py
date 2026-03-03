import json
import socket
import struct
from typing import Any


class FramingError(Exception):
    pass


def send_framed(sock: socket.socket, message: Any) -> None:
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    header = struct.pack("!I", len(payload))
    sock.sendall(header + payload)


def recv_exact(sock: socket.socket, nbytes: int) -> bytes:
    chunks = []
    remaining = nbytes
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise EOFError("socket closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_framed(sock: socket.socket) -> Any:
    try:
        header = recv_exact(sock, 4)
    except EOFError:
        raise

    if len(header) != 4:
        raise FramingError("invalid frame header")

    (size,) = struct.unpack("!I", header)
    if size <= 0:
        raise FramingError("invalid frame size")

    payload = recv_exact(sock, size)
    try:
        return json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise FramingError("invalid JSON payload") from exc
