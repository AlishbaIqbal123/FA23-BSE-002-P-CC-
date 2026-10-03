"""FFmpeg execution engine with hardware-accelerated encoder selection.

The engine drives ``ffmpeg -progress pipe:1``, which emits machine-readable
``key=value`` lines on stdout (``out_time_us``, ``frame``, ``fps``, ``speed``).
That is the mechanism behind Task 4: progress is derived from the encoder's own
clock rather than by polling the output file, so the percentage is meaningful
even for a slow software transcode.

Encoder selection is a chain. NVENC is preferred; if the driver, the DLL or the
GPU is missing, the failure is detected from ffmpeg's stderr and the job is
retried on the next candidate instead of dying. That is what makes one build of
the daemon work on a 4 GB NVIDIA workstation and on a GPU-less CI box.
"""

from __future__ import annotations

import math
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from common.messages import (
    GPU_ENCODER_FALLBACK,
    NVENC_PRESETS,
    RESOLUTIONS,
    X264_PRESETS,
)
from common.paths import CREATE_NO_WINDOW
from server.capabilities import find_ffprobe, run_ffmpeg, video_dimensions
from .base import Engine, EngineResult, JobContext

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)")
#: Audio encoders that older ffmpeg builds gate behind ``-strict experimental``.
_EXPERIMENTAL_AUDIO = frozenset({"aac", "libspeex", "libtwolame", "libvorbis"})
#: Preference order when the requested audio codec is missing from this build.
_AUDIO_FALLBACK = ("aac", "libvo_aacenc", "libmp3lame", "libopus", "libvorbis")
_NO_NVENC_MARKERS = (
    "cannot load libcuda",
    "cannot load nvcuda.dll",
    "nvcuda.dll",
    "no nvenc capable devices",
    "initialization failed",
    "unknown encoder 'h264_nvenc'",
    "unknown encoder 'hevc_nvenc'",
    "device failure",
    "cannot load nvcuvid",
    "unsupported codec",
    "invalid argument",
)


def parse_progress_line(line: str) -> dict[str, str]:
    """Turn one ``key=value`` progress line into a dict. Exposed for unit tests."""
    line = line.strip()
    if not line or "=" not in line:
        return {}
    key, _, value = line.partition("=")
    return {key.strip(): value.strip()}


def probe_duration(ffmpeg: str, path: Path) -> float:
    """Read the container duration. ``ffprobe`` if present, else parse ``ffmpeg -i``."""
    ffmpeg_dir = Path(ffmpeg).parent
    stem = "ffprobe.exe" if Path(ffmpeg).suffix.lower() == ".exe" else "ffprobe"
    ffprobe = ffmpeg_dir / stem
    if ffprobe.exists():
        try:
            proc = subprocess.run(
                [str(ffprobe), "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, text=True, timeout=60,
                creationflags=CREATE_NO_WINDOW, errors="replace",
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return float(proc.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

    # `ffmpeg -i` with no output target exits non-zero but still prints the
    # stream metadata on stderr, which is where the duration lives.
    code, _, err = run_ffmpeg(ffmpeg, ["-i", str(path)], timeout=60)
    match = _DURATION_RE.search(err or "")
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _even(value: float) -> int:
    """Round to the nearest even integer - every codec here needs even dimensions.

    ``int(round(x))`` is not used because Python rounds halves to the nearest even
    number, which makes a 721-pixel target come out as 720 and a 405 as 404. Going
    half away from zero keeps the mapping obvious and reproducible.
    """
    return max(2, int(math.floor(value / 2.0 + 0.5)) * 2)


def _scale_filter(target: tuple[int, int] | None,
                  source: tuple[int, int] | None) -> list[str]:
    """Build a ``-vf scale=W:H`` that preserves the source aspect ratio.

    Both numbers are computed here rather than left to ffmpeg's ``-2`` shorthand
    because ``scale=640:-2`` is rejected by older builds ("Size values less than -1
    are not acceptable"), which would make every non-source resolution fail on them.
    Upscaling is avoided: a source smaller than the target is left alone.
    """
    if target is None:
        return []
    target_w, target_h = target
    if source is None:
        # Without dimensions, fall back to the nominal size of the preset.
        return ["-vf", f"scale={target_w}:{_even(target_h)}"]
    source_w, source_h = source
    if source_w <= target_w and source_h <= target_h:
        return []
    ratio = source_h / float(source_w)
    if source_w >= source_h:
        # Landscape: width is the binding constraint.
        width = min(target_w, _even(source_w))
        height = _even(width * ratio)
    else:
        # Portrait or square: height is.
        height = min(target_h, _even(source_h))
        width = _even(height / ratio)
    return ["-vf", f"scale={width}:{height}"]


def build_command(ffmpeg: str, spec, input_path: Path, output_path: Path,
                  source: tuple[int, int] | None = None,
                  audio_codec: str | None = None) -> list[str]:
    # No -hide_banner here on purpose. It only exists from FFmpeg 2.x onwards, and
    # putting it in the command means a single old binary rejects *every* candidate
    # encoder, so the fallback chain below can never rescue the job. The banner is
    # harmless noise: this project parses -progress output and keeps its own logs,
    # and stderr is captured rather than printed verbatim.
    cmd: list[str] = [ffmpeg, "-nostdin", "-y", "-i", str(input_path)]

    cmd += _scale_filter(RESOLUTIONS.get(spec.resolution), source)

    if spec.video_codec.endswith("_nvenc"):
        cmd += ["-c:v", spec.video_codec, "-preset", spec.preset, "-tune", "ql",
                "-rc", "vbr", "-b_ref_mode", "0"]
        cmd += ["-b:v", f"{spec.bitrate_kbps or 8000}k"] if spec.bitrate_kbps else \
               ["-cq:v", str(spec.crf)]
        cmd += ["-spatial_aq", "1", "-temporal_aq", "1"]
    else:
        cmd += ["-c:v", spec.video_codec, "-preset", spec.preset]
        cmd += ["-b:v", f"{spec.bitrate_kbps}k"] if spec.bitrate_kbps else \
               ["-crf", str(spec.crf)]

    audio = audio_codec or spec.audio_codec
    if audio == "none":
        cmd += ["-an"]
    else:
        if audio in _EXPERIMENTAL_AUDIO:
            # Older builds ship the native AAC/AAC-alike encoders behind the
            # experimental gate and refuse them without this flag. Newer builds
            # accept the flag as a no-op, so it is safe to always send it here.
            cmd += ["-strict", "-2"]
        cmd += ["-c:a", audio]
        if audio != "copy":
            cmd += ["-b:a", f"{spec.audio_bitrate_kbps}k"]

    if spec.pixel_fmt:
        cmd += ["-pix_fmt", spec.pixel_fmt]
    if spec.extra_args:
        cmd += [str(a) for a in spec.extra_args]

    cmd += ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(output_path)]
    return cmd


def _looks_like_encoder_failure(stderr_tail: str) -> bool:
    text = stderr_tail.lower()
    return any(marker in text for marker in _NO_NVENC_MARKERS)


class FFmpegEngine(Engine):
    name = "ffmpeg"
    description = "FFmpeg transcode with NVENC / QSV / AMF / software encoders"

    def __init__(self, ffmpeg_path: str, encoders: list[str], device: str = "",
                 ffprobe_path: str | None = None) -> None:
        self.ffmpeg = ffmpeg_path
        self.encoders = list(encoders)
        self.device = device
        self.ffprobe = ffprobe_path
        # Keyed by (path, mtime, size) so a re-uploaded file is probed again.
        self._dimensions: dict[tuple[str, float, int], tuple[int, int] | None] = {}

    def _source_size(self, path: Path) -> tuple[int, int] | None:
        """Probe the input dimensions once per file, tolerating a missing ffprobe."""
        if not self.ffprobe:
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        key = (str(path), stat.st_mtime, stat.st_size)
        if key not in self._dimensions:
            self._dimensions[key] = video_dimensions(self.ffprobe, path)
            if len(self._dimensions) > 64:
                self._dimensions.pop(next(iter(self._dimensions)))
        return self._dimensions[key]

    def available(self) -> bool:
        return bool(self.ffmpeg) and bool(self.encoders)

    def candidates(self, requested: str) -> list[str]:
        chain = [requested]
        if requested.endswith("_nvenc"):
            chain += [e for e in ("hevc_nvenc", "h264_qsv", "h264_amf") if e != requested]
        chain += [e for e in GPU_ENCODER_FALLBACK if e != requested]
        usable = [e for e in chain if e in self.encoders]
        return usable or [requested]

    def audio_candidate(self, requested: str) -> str:
        """Pick an audio codec this ffmpeg build actually has.

        ``copy`` and ``none`` are always honoured as-is. Otherwise the requested
        codec is used when the encoder list mentions it, and otherwise the first
        entry of :data:`_AUDIO_FALLBACK` that this build advertises. That keeps a
        job working on a machine whose ffmpeg only ships ``libvo_aacenc``.
        """
        if requested in ("none", "copy") or requested in self.encoders:
            return requested
        for codec in _AUDIO_FALLBACK:
            if codec in self.encoders:
                return codec
        return requested

    def run(self, ctx: JobContext) -> EngineResult:
        result = EngineResult(encoder=ctx.spec.video_codec, device=self.device)
        if not self.ffmpeg:
            result.status, result.error = "error", "ffmpeg executable not found"
            return result

        duration = probe_duration(self.ffmpeg, ctx.input_path)
        ctx.report(2.0, "probing", f"duration {duration:.2f}s" if duration else "duration unknown")

        last_error = ""
        for index, encoder in enumerate(self.candidates(ctx.spec.video_codec)):
            if ctx.cancelled:
                result.status, result.error = "cancelled", "cancelled before start"
                return result
            outcome = self._run_once(ctx, encoder, duration, index > 0)
            if outcome is None:
                result.status = "ok"
                result.encoder = encoder
                result.device = self.device
                return result
            last_error = outcome
            ctx.log("warn", f"encoder {encoder} failed, trying the next candidate")

        result.status = "error"
        result.error = last_error or "all encoder candidates failed"
        return result

    def _run_once(self, ctx: JobContext, encoder: str, duration: float,
                  is_fallback: bool) -> str | None:
        """Returns ``None`` on success, or an error string on failure."""
        spec = ctx.spec
        if spec.video_codec.endswith("_nvenc") and encoder != spec.video_codec:
            spec.preset = NVENC_PRESETS[3] if encoder.endswith("_nvenc") else X264_PRESETS[3]
        elif not encoder.endswith("_nvenc"):
            spec.preset = spec.preset if spec.preset in X264_PRESETS else "medium"
        spec.video_codec = encoder

        ctx.output_path.parent.mkdir(parents=True, exist_ok=True)
        audio = self.audio_candidate(spec.audio_codec)
        if audio != spec.audio_codec:
            ctx.log("warn", f"audio codec {spec.audio_codec} is not in this build of "
                             f"ffmpeg, using {audio}")
        cmd = build_command(self.ffmpeg, spec, ctx.input_path, ctx.output_path,
                            source=self._source_size(ctx.input_path),
                            audio_codec=audio)
        ctx.log("info", ("retry with " if is_fallback else "") + "ffmpeg: " + " ".join(cmd[:14]) + " ...")

        started = time.perf_counter()
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL, text=True, bufsize=1,
                errors="replace", creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            return f"cannot spawn ffmpeg: {exc}"

        stderr_tail = deque(maxlen=40)
        pump_done = threading.Event()

        def pump_stderr() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                stderr_tail.append(line.rstrip())
            pump_done.set()

        pump = threading.Thread(target=pump_stderr, name="ffmpeg-stderr", daemon=True)
        pump.start()

        fields: dict[str, str] = {}
        last_emit = 0.0
        tail_text = ""

        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                if ctx.cancelled:
                    proc.terminate()
                    break
                fields.update(parse_progress_line(raw))
                now = time.perf_counter()
                if now - last_emit < 0.12:
                    continue
                last_emit = now
                tail_text = "\n".join(stderr_tail)
                elapsed = now - started
                out_us = float(fields.get("out_time_us") or fields.get("out_time_ms") or 0)
                # Older builds emit out_time_ms; both are microsecond-scaled here.
                out_seconds = out_us / 1_000_000.0 if out_us else 0.0
                pct = 99.0 if not duration else min(99.0, out_seconds / duration * 100.0)
                speed = fields.get("speed", "").strip()
                eta = 0.0
                if duration > out_seconds > 0 and speed.endswith("x"):
                    try:
                        rate = float(speed[:-1])
                        eta = (duration - out_seconds) / rate if rate > 0 else 0.0
                    except ValueError:
                        eta = 0.0
                ctx.report(
                    pct, "encoding", f"{encoder} | {speed or 'n/a'} | frame {fields.get('frame', '?')}",
                    fps=float(fields.get("fps") or 0.0), speed=speed or "n/a",
                    frame=int(fields.get("frame") or 0), eta_s=round(eta, 1),
                    elapsed_s=round(elapsed, 2),
                )
        finally:
            pump_done.wait(timeout=5.0)
            proc.wait()

        tail_text = "\n".join(stderr_tail)
        code = proc.returncode

        if ctx.cancelled:
            ctx.output_path.unlink(missing_ok=True)
            return "cancelled"

        if code != 0 or not ctx.output_path.exists():
            # An encoder that cannot initialise fails within the first second and
            # says so; anything else is a genuine job failure worth reporting.
            ctx.output_path.unlink(missing_ok=True)
            if _looks_like_encoder_failure(tail_text):
                return f"{encoder}: {tail_text.strip()[-400:]}"
            return f"ffmpeg exited {code}: {tail_text.strip()[-800:]}"

        ctx.report(100.0, "encoding", "encoder finished")
        return None


__all__ = ["FFmpegEngine", "parse_progress_line", "probe_duration", "build_command"]