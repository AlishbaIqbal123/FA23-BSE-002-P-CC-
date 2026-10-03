"""Probe the worker node so the client can show what the GPU can actually do.

Capability discovery is deliberately cheap and cached at start-up: it runs once
per daemon launch, and the resulting :class:`Capabilities` snapshot travels to
every client in the ``HELLO_ACK`` frame. The GUI uses it to grey out encoders
this particular machine does not have, which prevents the user from submitting a
job that is guaranteed to fail.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import socket
import subprocess
import uuid
from pathlib import Path

from common.messages import Capabilities
from common.paths import CREATE_NO_WINDOW


def _run(cmd: list[str], timeout: float = 12.0) -> tuple[int, str, str]:
    """Run a probe command, returning ``(returncode, stdout, stderr)``.

    ``CREATE_NO_WINDOW`` keeps a console flash from appearing on Windows when the
    daemon is launched from a GUI-less context.
    """
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            errors="replace", creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def run_ffmpeg(ffmpeg: str, args: list[str], timeout: float = 20.0) -> tuple[int, str, str]:
    """Run ffmpeg with ``args``, transparently coping with old builds.

    ``-hide_banner`` only exists from FFmpeg 2.x onwards. Rather than requiring a
    modern build, the flag is retried without it when the binary rejects it, so
    capability probing still works on the 2013-era builds that ship inside some
    Python wheels.
    """
    for prefix in (["-hide_banner"], []):
        code, out, err = _run([ffmpeg, *prefix, *args], timeout=timeout)
        if "Unrecognized option 'hide_banner'" in (err or "") or \
           "Option hide_banner not found" in (err or ""):
            continue
        return code, out, err
    return code, out, err


def find_ffmpeg() -> str | None:
    explicit = os.environ.get("RDO_FFMPEG")
    if explicit and (os.path.isfile(explicit) or shutil.which(explicit)):
        return explicit
    return shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")


def find_ffprobe() -> str | None:
    """Locate ffprobe, preferring one that sits next to the chosen ffmpeg."""
    explicit = os.environ.get("RDO_FFPROBE")
    if explicit and (os.path.isfile(explicit) or shutil.which(explicit)):
        return explicit
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        sibling = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if sibling.exists():
            return str(sibling)
    return shutil.which("ffprobe") or shutil.which("ffprobe.exe")


def video_dimensions(ffprobe: str, path: Path) -> tuple[int, int] | None:
    """Return ``(width, height)`` of the first video stream, or ``None``.

    Used to work out an explicit scale target. ffmpeg's ``-2`` shorthand is not
    available in older builds (it fails with "Size values less than -1 are not
    acceptable"), so the arithmetic is done here instead of in the filter graph.
    """
    code, out, _ = _run([
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path),
    ], timeout=20.0)
    if code != 0:
        return None
    for line in out.splitlines():
        parts = line.strip().split("x")
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            width, height = int(parts[0]), int(parts[1])
            if width > 0 and height > 0:
                return width, height
    return None


def _ffmpeg_version(path: str) -> tuple[str, str]:
    code, out, _ = run_ffmpeg(path, ["-version"])
    if code != 0 or not out.strip():
        return "", ""
    first = out.splitlines()[0]
    match = re.search(r"ffmpeg version (\S+)", first)
    return first.strip(), (match.group(1) if match else "")


def available_encoders(path: str) -> set[str]:
    """Every encoder name this ffmpeg build advertises, e.g. ``{'libx264', 'aac'}``."""
    code, out, err = run_ffmpeg(path, ["-encoders"])
    if code != 0:
        return set()
    found: set[str] = set()
    for line in (out + err).splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        flags = parts[0]
        # Format is "<type><flag chars> <name> <description>", e.g. "A..X.. aac" or
        # "V..... libx264". The exact flag count varies between ffmpeg versions,
        # so only the leading type letter and the flag alphabet are relied upon.
        # The second token being "=" identifies the legend block (" V..... = Video"),
        # which is not an encoder at all.
        if parts[1] == "=":
            continue
        if flags[0] in "VAS" and len(flags) >= 6 and set(flags[1:]) <= set(".XDSBIL"):
            found.add(parts[1])
    return found


def _gpu_name_from_nvidia_smi() -> str:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return ""
    code, out, _ = _run([exe, "--query-gpu=name", "--format=csv,noheader"])
    if code != 0 or not out.strip():
        return ""
    return out.strip().splitlines()[0].strip()


def _probe_torch() -> tuple[bool, bool, str]:
    try:
        import torch  # type: ignore
    except Exception:
        return False, False, ""
    try:
        cuda = bool(torch.cuda.is_available())
        device = torch.cuda.get_device_name(0) if cuda else ""
    except Exception:
        cuda, device = False, ""
    return True, cuda, device


def build_capabilities(
    max_concurrent_jobs: int = 1,
    max_upload_bytes: int = 0,
    ffmpeg_path: str | None = None,
) -> Capabilities:
    ffmpeg = ffmpeg_path or find_ffmpeg()
    caps = Capabilities(
        worker_id=f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}",
        hostname=socket.gethostname(),
        os=f"{platform.system()} {platform.release()} ({platform.machine()})",
        python=platform.python_version(),
        cpu_count=os.cpu_count() or 1,
        max_concurrent_jobs=max(1, max_concurrent_jobs),
        max_upload_bytes=max_upload_bytes,
    )

    if ffmpeg:
        caps.ffmpeg_path = ffmpeg
        banner, build = _ffmpeg_version(ffmpeg)
        caps.ffmpeg_version = f"{build or 'unknown'} | {banner}" if banner else ""
        encoders = available_encoders(ffmpeg)
        interesting = (
            "h264_nvenc", "hevc_nvenc", "av1_nvenc", "h264_qsv", "hevc_qsv",
            "h264_amf", "hevc_amf", "libx264", "libx265", "mpeg4", "aac", "libopus",
        )
        caps.hardware_encoders = sorted(e for e in interesting if e in encoders)
        caps.nvenc_available = "h264_nvenc" in encoders and bool(_gpu_name_from_nvidia_smi())
        caps.engines.append("transcode" if encoders else "")
    caps.engines = [e for e in caps.engines if e]

    caps.gpu_name = _gpu_name_from_nvidia_smi()
    torch_ok, cuda_ok, cuda_dev = _probe_torch()
    caps.torch_available = torch_ok
    caps.cuda_available = cuda_ok
    caps.cuda_device = cuda_dev
    # The compute engine always exists: TorchComputeEngine falls back to NumPy
    # when PyTorch is absent, so it must be advertised either way. The
    # torch_available / cuda_available flags tell the GUI which one will run.
    caps.engines.append("compute")
    if not caps.nvenc_available and "h264_nvenc" in caps.hardware_encoders:
        caps.nvenc_available = True
    return caps


def encoder_is_usable(caps: Capabilities, encoder: str) -> bool:
    if not caps.hardware_encoders:
        return False
    return encoder in caps.hardware_encoders


__all__ = ["build_capabilities", "find_ffmpeg", "find_ffprobe", "video_dimensions",
           "encoder_is_usable", "run_ffmpeg", "available_encoders"]