"""Shared wire protocol, message schemas and integrity helpers.

Both the client and the server import from this package so that a change to the
frame layout or to a default value cannot drift out of sync.
"""

from .messages import (
    AUDIO_CODECS,
    COMPUTE_OPS,
    GPU_ENCODER_FALLBACK,
    GPU_ENCODERS,
    JOB_TYPES,
    NVENC_PRESETS,
    RESOLUTIONS,
    X264_PRESETS,
    Capabilities,
    JobResult,
    JobSpec,
    PingSample,
    Progress,
    safe_filename,
)
from .protocol import (
    MAGIC,
    PROTOCOL_VERSION,
    FrameType,
    ProtocolError,
    PeerClosed,
    HandshakeError,
    RemoteError,
    ConnectionTimeout,
    BadFrame,
    FrameTooLarge,
    recv_exact,
    send_frame,
    send_json,
    recv_frame,
    recv_json,
    frame_name,
    write_stream,
)

__all__ = [
    "MAGIC", "PROTOCOL_VERSION", "FrameType", "ProtocolError", "PeerClosed",
    "HandshakeError", "RemoteError", "ConnectionTimeout", "BadFrame",
    "FrameTooLarge", "recv_exact", "send_frame", "send_json", "recv_frame",
    "recv_json", "frame_name", "write_stream", "JOB_TYPES", "RESOLUTIONS",
    "GPU_ENCODERS", "GPU_ENCODER_FALLBACK", "NVENC_PRESETS", "X264_PRESETS",
    "AUDIO_CODECS", "COMPUTE_OPS", "JobSpec", "Progress", "JobResult",
    "Capabilities", "PingSample", "safe_filename",
]