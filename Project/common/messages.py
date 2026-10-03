"""Typed payloads for every control frame, plus shared validation.

Validation is implemented once and invoked by *both* peers: the client uses it
to reject impossible settings before touching the network, and the server
re-runs it as the authoritative check because a client is never trusted.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .paths import PROTOCOL_VERSION

JOB_TYPES = ("transcode", "compute")

RESOLUTIONS: dict[str, tuple[int, int] | None] = {
    "source": None,
    "2160p": (3840, 2160),
    "1440p": (2560, 1440),
    "1080p": (1920, 1080),
    "720p": (1280, 720),
    "480p": (854, 480),
    "360p": (640, 360),
}

GPU_ENCODERS = {
    "h264_nvenc": "NVIDIA NVENC (H.264)",
    "hevc_nvenc": "NVIDIA NVENC (HEVC/H.265)",
    "h264_qsv": "Intel Quick Sync (H.264)",
    "h264_amf": "AMD AMF (H.264)",
    "hevc_amf": "AMD AMF (HEVC/H.265)",
    "libx264": "CPU software (x264)",
    "libx265": "CPU software (x265)",
    "mpeg4": "CPU software (MPEG-4 Part 2)",
}

GPU_ENCODER_FALLBACK = ("libx264", "mpeg4")
NVENC_PRESETS = tuple(f"p{i}" for i in range(1, 8))
X264_PRESETS = (
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
)
AUDIO_CODECS = ("aac", "libopus", "copy", "none")
COMPUTE_OPS = ("matmul", "conv2d", "transformer_ffn", "elementwise")

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str, fallback: str = "asset.bin") -> str:
    """Strip directory components and exotic characters from a client-supplied name."""
    base = Path(str(name).replace("\\", "/")).name.strip()
    base = _SAFE_NAME.sub("_", base).lstrip(".")
    if not base or base in {".", ".."}:
        return fallback
    return base[:180]


@dataclass(slots=True)
class JobSpec:
    job_type: str = "transcode"
    asset_name: str = "asset.bin"
    asset_size: int = 0
    asset_sha256: str = ""
    output_name: str = "output.mp4"
    resolution: str = "source"
    video_codec: str = "h264_nvenc"
    preset: str = "p4"
    bitrate_kbps: int = 0
    crf: int = 23
    audio_codec: str = "aac"
    audio_bitrate_kbps: int = 128
    pixel_fmt: str = "yuv420p"
    compute_op: str = "matmul"
    compute_size: int = 2048
    compute_iters: int = 20
    timeout_s: float = 1800.0
    extra_args: list[str] = field(default_factory=list)

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.job_type not in JOB_TYPES:
            errors.append(f"job_type must be one of {JOB_TYPES}, got {self.job_type!r}")
        if self.job_type == "transcode":
            if self.resolution not in RESOLUTIONS:
                errors.append(f"unknown resolution {self.resolution!r}")
            if self.video_codec not in GPU_ENCODERS:
                errors.append(f"unknown video codec {self.video_codec!r}")
            if self.video_codec.endswith("_nvenc") and self.preset not in NVENC_PRESETS:
                errors.append(
                    f"NVENC preset must be one of {NVENC_PRESETS}, got {self.preset!r}"
                )
            if not self.video_codec.endswith("_nvenc") and self.preset not in X264_PRESETS:
                errors.append(
                    f"{self.video_codec} preset must be one of {X264_PRESETS}, "
                    f"got {self.preset!r}"
                )
            if self.audio_codec not in AUDIO_CODECS:
                errors.append(f"unknown audio codec {self.audio_codec!r}")
            if not 0 <= self.crf <= 51:
                errors.append(f"crf must be 0..51, got {self.crf}")
            if self.bitrate_kbps and not 50 <= self.bitrate_kbps <= 200_000:
                errors.append(f"bitrate_kbps must be 0 or 50..200000, got {self.bitrate_kbps}")
        else:
            if self.compute_op not in COMPUTE_OPS:
                errors.append(f"unknown compute_op {self.compute_op!r}")
            if not 64 <= self.compute_size <= 8192:
                errors.append(f"compute_size must be 64..8192, got {self.compute_size}")
            if not 1 <= self.compute_iters <= 500:
                errors.append(f"compute_iters must be 1..500, got {self.compute_iters}")
        if self.asset_size < 0:
            errors.append("asset_size cannot be negative")
        if self.extra_args and len(self.extra_args) > 16:
            errors.append("at most 16 extra ffmpeg arguments are permitted")
        return errors

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobSpec":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass(slots=True)
class Progress:
    pct: float = 0.0
    stage: str = "queued"
    detail: str = ""
    fps: float = 0.0
    speed: str = ""
    frame: int = 0
    eta_s: float = 0.0
    elapsed_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class JobResult:
    job_id: str = ""
    status: str = "ok"
    engine: str = ""
    encoder: str = ""
    device: str = ""
    output_name: str = ""
    output_size: int = 0
    output_sha256: str = ""
    upload_seconds: float = 0.0
    compute_seconds: float = 0.0
    download_seconds: float = 0.0
    total_seconds: float = 0.0
    upload_bytes: int = 0
    download_bytes: int = 0
    command: list[str] = field(default_factory=list)
    stderr_tail: str = ""
    error: str = ""
    finished_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Capabilities:
    worker_id: str = ""
    hostname: str = ""
    os: str = ""
    python: str = ""
    protocol_version: str = PROTOCOL_VERSION
    ffmpeg_path: str = ""
    ffmpeg_version: str = ""
    gpu_name: str = ""
    nvenc_available: bool = False
    hardware_encoders: list[str] = field(default_factory=list)
    engines: list[str] = field(default_factory=list)
    torch_available: bool = False
    cuda_available: bool = False
    cuda_device: str = ""
    cpu_count: int = 0
    max_concurrent_jobs: int = 1
    max_upload_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PingSample:
    nonce: str = ""
    rtt_ms: float = 0.0
    server_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "PROTOCOL_VERSION", "JOB_TYPES", "RESOLUTIONS", "GPU_ENCODERS",
    "GPU_ENCODER_FALLBACK", "NVENC_PRESETS", "X264_PRESETS", "AUDIO_CODECS",
    "COMPUTE_OPS", "JobSpec", "Progress", "JobResult", "Capabilities",
    "PingSample", "safe_filename",
]