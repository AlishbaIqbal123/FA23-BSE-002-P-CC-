"""Frame protocol: framing, fragmentation, size limits and failure modes."""

from __future__ import annotations

import json
import socket
import threading

import pytest

from common.protocol import (
    HEADER,
    BadFrame,
    FrameTooLarge,
    FrameType,
    HandshakeError,
    JSON_TYPES,
    PeerClosed,
    ConnectionTimeout,
    ProtocolError,
    RemoteError,
    recv_exact,
    recv_frame,
    recv_json,
    send_frame,
    send_json,
    write_stream,
)
from common.paths import MAX_FRAME_PAYLOAD


def socketpair() -> tuple[socket.socket, socket.socket]:
    return socket.socketpair()


def test_send_and_receive_json_frame():
    left, right = socketpair()
    with left, right:
        send_json(left, FrameType.HELLO, {"magic": "RDO1", "protocol_version": "1.0"})
        ftype, payload = recv_frame(right)
        assert ftype is FrameType.HELLO
        assert json.loads(payload) == {"magic": "RDO1", "protocol_version": "1.0"}


def test_binary_payload_survives_round_trip():
    left, right = socketpair()
    with left, right:
        blob = bytes(range(256)) * 40
        send_frame(left, FrameType.DOWNLOAD_CHUNK, blob)
        ftype, payload = recv_frame(right)
        assert ftype is FrameType.DOWNLOAD_CHUNK
        assert payload == blob


def test_an_empty_binary_payload_is_legal():
    left, right = socketpair()
    with left, right:
        send_frame(left, FrameType.DOWNLOAD_CHUNK, b"")
        ftype, payload = recv_frame(right)
        assert ftype is FrameType.DOWNLOAD_CHUNK
        assert payload == b""


def test_an_empty_control_frame_is_rejected():
    # BYE is a JSON frame like every other control frame, so an empty payload is a
    # malformed frame rather than a legal one. Only DOWNLOAD_CHUNK may be empty.
    left, right = socketpair()
    with left, right:
        send_frame(left, FrameType.BYE, b"")
        with pytest.raises(BadFrame):
            recv_frame(right)


def test_every_control_frame_is_json_validated():
    binary = {FrameType.DOWNLOAD_CHUNK}
    for ftype in FrameType:
        expected = ftype not in binary
        assert (ftype in JSON_TYPES) is expected, f"{ftype.name} classification is wrong"


def test_frames_arrive_in_order_across_a_single_connection():
    left, right = socketpair()
    with left, right:
        expected = [
            (FrameType.PING, {"n": 1}), (FrameType.LOG, {"level": "info"}),
            (FrameType.PING, {"n": 2}), (FrameType.RESULT, {"status": "ok"}),
            (FrameType.PING, {"n": 3}),
        ]
        for ftype, body in expected:
            send_json(left, ftype, body)
        for ftype, body in expected:
            got_type, payload = recv_frame(right)
            assert got_type is ftype
            assert json.loads(payload) == body


def test_recv_exact_loops_until_all_bytes_arrive():
    """A single recv() may legally return fewer bytes than requested."""
    left, right = socketpair()
    with left, right:
        blob = b"0123456789" * 100

        def dribble(payload: bytes, step: int) -> None:
            for index in range(0, len(payload), step):
                left.sendall(payload[index:index + step])
                threading.Event().wait(0.001)

        writer = threading.Thread(target=dribble, args=(blob, 3))
        writer.start()
        try:
            assert recv_exact(right, len(blob)) == blob
        finally:
            writer.join()

    # A whole frame, delivered in 7-byte pieces, must reassemble just the same:
    # the 5-byte header can itself be split across packets.
    left, right = socketpair()
    with left, right:
        blob = b"A" * 5000
        framed = HEADER.pack(int(FrameType.DOWNLOAD_CHUNK), len(blob)) + blob
        writer = threading.Thread(target=dribble, args=(framed, 7))
        writer.start()
        try:
            ftype, payload = recv_frame(right)
            assert ftype is FrameType.DOWNLOAD_CHUNK
            assert payload == blob
        finally:
            writer.join()


def test_recv_exact_raises_on_early_close():
    left, right = socketpair()
    with left, right:
        left.sendall(b"only-a-few")
        left.close()
        with pytest.raises(PeerClosed):
            recv_exact(right, 64)


def test_recv_frame_raises_on_close_before_any_byte():
    left, right = socketpair()
    with left, right:
        left.close()
        with pytest.raises(PeerClosed):
            recv_frame(right)


def test_oversized_payload_is_rejected_before_sending():
    left, right = socketpair()
    with left, right:
        with pytest.raises(FrameTooLarge):
            send_frame(left, FrameType.PROGRESS, b"x" * (MAX_FRAME_PAYLOAD + 1))


def test_announced_length_above_the_ceiling_is_refused():
    """A hostile or buggy peer must not be able to make us allocate arbitrarily."""
    left, right = socketpair()
    with left, right:
        left.sendall(bytes([int(FrameType.PROGRESS)]) + (MAX_FRAME_PAYLOAD + 1).to_bytes(4, "big"))
        with pytest.raises(FrameTooLarge):
            recv_frame(right)


def test_unknown_frame_type_is_reported():
    left, right = socketpair()
    with left, right:
        left.sendall(b"\xf0" + (0).to_bytes(4, "big"))
        with pytest.raises(BadFrame):
            recv_frame(right)


def test_invalid_json_is_reported_as_bad_frame():
    left, right = socketpair()
    with left, right:
        send_frame(left, FrameType.LOG, b"this is not json")
        with pytest.raises(BadFrame):
            recv_frame(right)


def test_json_must_be_an_object():
    left, right = socketpair()
    with left, right:
        send_frame(left, FrameType.LOG, b"[1, 2, 3]")
        with pytest.raises(BadFrame):
            recv_frame(right)


def test_socket_timeout_becomes_connection_timeout():
    left, right = socketpair()
    with left, right:
        right.settimeout(0.15)
        with pytest.raises(ConnectionTimeout):
            recv_exact(right, 8)


def test_timeout_closes_the_socket_because_the_stream_is_unrecoverable():
    left, right = socketpair()
    with left, right:
        right.settimeout(0.15)
        left.sendall(b"\x01\x00")  # a partial frame header
        with pytest.raises(ConnectionTimeout):
            recv_frame(right)
        assert right.fileno() == -1


def test_error_frame_is_raised_as_a_protocol_error():
    left, right = socketpair()
    with left, right:
        send_json(left, FrameType.ERROR, {"code": "checksum_mismatch", "message": "bad"})
        with pytest.raises(RemoteError) as excinfo:
            recv_json(right)
        assert excinfo.value.code == "checksum_mismatch"
        assert "bad" in excinfo.value.message


def test_stream_hashes_while_sending():
    import hashlib
    import io

    left, right = socketpair()
    with left, right:
        data = b"offload" * 1000
        digest = write_stream(left, FrameType.DOWNLOAD_CHUNK, io.BytesIO(data), len(data),
                              chunk_size=64)
        assert digest == hashlib.sha256(data).hexdigest()

        received = bytearray()
        while len(received) < len(data):
            ftype, block = recv_frame(right)
            assert ftype is FrameType.DOWNLOAD_CHUNK
            received += block
        assert bytes(received) == data


def test_every_frame_type_has_a_distinct_code():
    codes = [int(f) for f in FrameType]
    assert len(codes) == len(set(codes))


def test_handshake_error_is_a_protocol_error_subclass():
    assert issubclass(HandshakeError, ProtocolError)
    assert issubclass(PeerClosed, ProtocolError)
    assert issubclass(ConnectionTimeout, ProtocolError)