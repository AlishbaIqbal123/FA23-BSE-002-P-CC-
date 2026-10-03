"""Length-prefixed frame protocol spoken over a raw TCP socket.

Wire format of every frame::

    +---------+------------------+---------------------------+
    | TYPE    | LENGTH           | PAYLOAD                   |
    | 1 byte  | 4 bytes uint32BE | LENGTH bytes              |
    +---------+------------------+---------------------------+

The 1-byte type selects how the payload is interpreted: JSON for control
frames, opaque bytes for stream frames.

Two rules make the protocol safe on a byte stream:

1.  ``recv()`` is never trusted to return the requested count. Every read goes
    through :func:`recv_exact`, which loops until the full count has arrived.
2.  A socket read timeout is fatal to the connection. If a timeout fires while
    a partially-read frame is in flight, the remaining bytes are still queued
    in the kernel and can no longer be located, so the stream is unrecoverable.
    :func:`recv_frame` therefore closes the socket before re-raising.
"""

from __future__ import annotations

import json
import socket
import struct
from enum import IntEnum
from typing import Any, BinaryIO

from .paths import MAX_FRAME_PAYLOAD, PROTOCOL_VERSION

MAGIC = b"RDO1"
HEADER = struct.Struct("!BI")
HEADER_SIZE = HEADER.size  # 5


class FrameType(IntEnum):
    HELLO = 0x01
    HELLO_ACK = 0x02
    PING = 0x03
    PONG = 0x04
    UPLOAD_BEGIN = 0x05
    UPLOAD_ACK = 0x06
    UPLOAD_END = 0x07
    UPLOAD_DONE = 0x08
    JOB_SUBMIT = 0x09
    JOB_ACK = 0x0A
    JOB_REJECT = 0x0B
    PROGRESS = 0x0C
    LOG = 0x0D
    RESULT = 0x0E
    DOWNLOAD_REQ = 0x0F
    STREAM_BEGIN = 0x10
    STREAM_END = 0x11
    CANCEL = 0x12
    ERROR = 0x13
    BYE = 0x14
    PING_BATCH = 0x15
    DOWNLOAD_CHUNK = 0x16


#: Frames whose payload is opaque bytes rather than a JSON object.
BINARY_TYPES = frozenset({FrameType.DOWNLOAD_CHUNK})
#: Every other frame type carries a JSON object. Having one rule - with a single,
#: explicit exception for the download chunks - means the validator below can never
#: be forgotten at a call site, which is exactly the bug this replaced.
JSON_TYPES = frozenset(FrameType) - BINARY_TYPES


class ProtocolError(Exception):
    """Base class for every failure raised by this module."""


class PeerClosed(ProtocolError):
    """The remote end closed the connection cleanly (EOF)."""


class HandshakeError(ProtocolError):
    """Magic bytes or protocol version did not match."""


class FrameTooLarge(ProtocolError):
    """Peer announced a payload larger than the configured ceiling."""


class BadFrame(ProtocolError):
    """Payload was not valid for the frame type that carried it."""


class RemoteError(ProtocolError):
    """The peer sent an ERROR frame."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class ConnectionTimeout(ProtocolError):
    """A socket operation exceeded its deadline."""


def frame_name(ftype: int) -> str:
    try:
        return FrameType(ftype).name
    except ValueError:
        return f"UNKNOWN(0x{ftype:02x})"


def _close(sock: socket.socket) -> None:
    try:
        sock.close()
    except OSError:
        pass


def recv_exact(sock: socket.socket, count: int) -> bytes:
    """Read exactly ``count`` bytes or raise.

    Raises :class:`PeerClosed` on EOF and :class:`ConnectionTimeout` when the
    socket deadline expires part-way through the read.
    """
    if count == 0:
        return b""
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        try:
            chunk = sock.recv(min(remaining, 1 << 20))
        except socket.timeout as exc:
            raise ConnectionTimeout(
                f"timed out after {count - remaining}/{count} bytes of a "
                f"{count}-byte read"
            ) from exc
        if not chunk:
            raise PeerClosed(
                f"peer closed after {count - remaining} of {count} expected bytes"
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def send_frame(sock: socket.socket, ftype: FrameType | int, payload: bytes = b"") -> None:
    if len(payload) > MAX_FRAME_PAYLOAD:
        raise FrameTooLarge(
            f"{frame_name(int(ftype))} payload {len(payload)} exceeds "
            f"{MAX_FRAME_PAYLOAD}"
        )
    try:
        sock.sendall(HEADER.pack(int(ftype), len(payload)) + payload)
    except socket.timeout as exc:
        raise ConnectionTimeout(f"send of {frame_name(int(ftype))} timed out") from exc


def send_json(sock: socket.socket, ftype: FrameType, obj: Any) -> None:
    send_frame(sock, ftype, json.dumps(obj, separators=(",", ":")).encode("utf-8"))


def send_bytes(sock: socket.socket, data: bytes) -> None:
    """Write a raw byte run - the upload body between UPLOAD_BEGIN/UPLOAD_END.

    This is deliberately *not* a frame: one frame per 256 KiB chunk would add a
    header every chunk and, more importantly, would let the receiver interleave
    control frames in the middle of a file. A single contiguous run means the
    worker can hash it as it lands, and the SHA-256 in UPLOAD_BEGIN is what proves
    the run arrived intact.
    """
    try:
        sock.sendall(data)
    except socket.timeout as exc:
        raise ConnectionTimeout("send of a raw byte run timed out") from exc


def _validate_payload(ftype: FrameType, payload: bytes) -> None:
    """Reject a malformed payload at the frame layer.

    Doing this here rather than at each call site means no handler can forget it.
    ERROR frames are only structurally validated; interpreting them is left to
    ``recv_json`` and to the client, which needs the body either way.
    """
    if ftype not in JSON_TYPES:
        return
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise BadFrame(f"{frame_name(int(ftype))} payload is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise BadFrame(f"{frame_name(int(ftype))} payload must be a JSON object")


def recv_frame(sock: socket.socket) -> tuple[FrameType, bytes]:
    """Read one frame. The socket is closed if the read is cut short."""
    try:
        raw = recv_exact(sock, HEADER_SIZE)
    except ProtocolError:
        _close(sock)
        raise
    ftype, length = HEADER.unpack(raw)
    if length > MAX_FRAME_PAYLOAD:
        _close(sock)
        raise FrameTooLarge(f"{frame_name(ftype)} announced {length} bytes")
    try:
        payload = recv_exact(sock, length) if length else b""
        try:
            resolved = FrameType(ftype)
        except ValueError as exc:
            raise BadFrame(f"unknown frame type 0x{ftype:02x}") from exc
        _validate_payload(resolved, payload)
    except ProtocolError:
        _close(sock)
        raise
    return resolved, payload


def recv_json(sock: socket.socket) -> tuple[FrameType, dict]:
    """Like :func:`recv_frame`, but decodes the payload and unwraps ``ERROR``."""
    ftype, payload = recv_frame(sock)
    if ftype is FrameType.ERROR:
        code, message = "unknown", payload.decode("utf-8", "replace")
        try:
            error = json.loads(payload)
            code = error.get("code", code)
            message = error.get("message", message)
        except (ValueError, AttributeError):
            pass
        raise RemoteError(code, message)
    return ftype, json.loads(payload.decode("utf-8"))


def write_stream(sock: socket.socket, ftype: FrameType, source: BinaryIO, size: int,
                 on_chunk=None, chunk_size: int = 1 << 18) -> str:
    """Send ``size`` bytes from ``source`` as repeated frames of type ``ftype``.

    Returns the SHA-256 of the bytes actually written, computed while streaming
    so nothing has to be re-read to verify the transfer.
    """
    import hashlib

    digest = hashlib.sha256()
    sent = 0
    while sent < size:
        block = source.read(min(chunk_size, size - sent))
        if not block:
            break
        digest.update(block)
        send_frame(sock, ftype, block)
        sent += len(block)
        if on_chunk is not None:
            on_chunk(sent, size)
    return digest.hexdigest()


__all__ = [
    "MAGIC",
    "PROTOCOL_VERSION",
    "HEADER_SIZE",
    "FrameType",
    "ProtocolError",
    "PeerClosed",
    "HandshakeError",
    "FrameTooLarge",
    "BadFrame",
    "RemoteError",
    "ConnectionTimeout",
    "JSON_TYPES",
    "BINARY_TYPES",
    "recv_exact",
    "send_frame",
    "send_json",
    "send_bytes",
    "recv_frame",
    "recv_json",
    "write_stream",
    "frame_name",
]