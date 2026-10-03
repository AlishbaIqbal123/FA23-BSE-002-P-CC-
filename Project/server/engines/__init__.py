"""Engine registry: maps a job type to the backend that can service it."""

from __future__ import annotations

from common.messages import Capabilities
from server.capabilities import find_ffprobe
from .base import Engine, EngineResult, JobContext
from .ffmpeg_engine import FFmpegEngine
from .torch_engine import TorchComputeEngine

__all__ = ["Engine", "EngineResult", "JobContext", "FFmpegEngine",
           "TorchComputeEngine", "build_registry"]


def build_registry(caps: Capabilities) -> dict[str, Engine]:
    engines: dict[str, Engine] = {}
    if caps.ffmpeg_path and caps.hardware_encoders:
        engines["transcode"] = FFmpegEngine(
            ffmpeg_path=caps.ffmpeg_path,
            encoders=caps.hardware_encoders,
            device=caps.gpu_name or "CPU (software encoder)",
            ffprobe_path=find_ffprobe(),
        )
    engines["compute"] = TorchComputeEngine()
    return engines