"""Task 5 harness: local render versus remote GPU offload, measured.

For every clip it runs the **same** ffmpeg settings twice - once on this
machine, once on the worker - and separates the remote wall-clock time into the
three phases that actually matter:

    t_remote_total = t_upload + t_compute + t_download

The headline numbers are then

    speedup              = t_local / t_remote_total      (end-to-end, what a user feels)
    compute_speedup      = t_local / t_compute           (the GPU, excluding the network)
    network_overhead_pct = 100 * (t_upload + t_download) / t_remote_total

Reporting only ``compute_speedup`` would be the dishonest choice, so both are
emitted and the report leads with the end-to-end figure.

    python tools/bench.py --host 192.168.1.1 --preset 720p
    python tools/bench.py --all --out benchmarks/results.json --markdown REPORT_data.md
"""

from __future__ import annotations

import argparse
import json
import platform
import socket
import statistics
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common.checksum import sha256_file  # noqa: E402
from common.messages import JobSpec  # noqa: E402
from common.protocol import FrameType, ProtocolError  # noqa: E402
from client.net.transport import Transport  # noqa: E402
from server.capabilities import find_ffmpeg, find_ffprobe, run_ffmpeg  # noqa: E402
from tools.make_sample_media import PRESETS  # noqa: E402

MEDIA_DIR = ROOT / "benchmarks" / "media"
LOCAL_OUT = ROOT / "benchmarks" / "local"
CLIENT_OUTPUT = ROOT / "benchmarks" / "remote"


@dataclass
class Measurement:
    label: str
    input_bytes: int
    resolution: str
    duration_s: float = 0.0
    local_seconds: float = 0.0
    upload_seconds: float = 0.0
    compute_seconds: float = 0.0
    download_seconds: float = 0.0
    remote_total: float = 0.0
    output_bytes: int = 0
    encoder_remote: str = ""
    encoder_local: str = ""
    device_remote: str = ""
    upload_mbits: float = 0.0
    download_mbits: float = 0.0
    progress_frames: int = 0
    progress_monotonic: bool = True
    verified: bool = False
    error: str = ""

    @property
    def speedup(self) -> float:
        return self.local_seconds / self.remote_total if self.remote_total else 0.0

    @property
    def compute_speedup(self) -> float:
        return self.local_seconds / self.compute_seconds if self.compute_seconds else 0.0

    @property
    def network_overhead_pct(self) -> float:
        if not self.remote_total:
            return 0.0
        return 100.0 * (self.upload_seconds + self.download_seconds) / self.remote_total

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["speedup"] = round(self.speedup, 3)
        payload["compute_speedup"] = round(self.compute_speedup, 3)
        payload["network_overhead_pct"] = round(self.network_overhead_pct, 2)
        return payload


def probe_media(path: Path) -> tuple[float, str]:
    """Return ``(duration_seconds, WxH)`` for a clip.

    ffprobe is used when it is available because parsing ffmpeg's human-readable
    banner is guesswork - the codec name sits where the frame size is expected on
    some builds, and the clip silently ends up labelled ``unknown``.
    """
    import re

    from server.engines.ffmpeg_engine import probe_duration

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return 0.0, "unknown"
    duration = probe_duration(ffmpeg, path)

    ffprobe = find_ffprobe()
    if ffprobe:
        from server.capabilities import _run

        code, out, _ = _run([
            ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path),
        ])
        if code == 0:
            token = out.strip().splitlines()[0].strip() if out.strip() else ""
            if re.fullmatch(r"\d+x\d+", token):
                return duration, token

    code, _, err = run_ffmpeg(ffmpeg, ["-i", str(path)])
    for line in (err or "").splitlines():
        if "Stream #" in line and "Video:" in line:
            match = re.search(r"(\d{2,5})x(\d{2,5})", line)
            if match:
                return duration, match.group(0)
            break
    return duration, "unknown"


def local_transcode(ffmpeg: str, source: Path, spec: JobSpec) -> tuple[float, int, str]:
    """Run the identical encode on this machine and time it."""
    from server.engines.ffmpeg_engine import build_command

    LOCAL_OUT.mkdir(parents=True, exist_ok=True)
    target = LOCAL_OUT / f"local-{source.stem}.mp4"
    target.unlink(missing_ok=True)
    command = build_command(ffmpeg, spec, source, target)
    started = time.perf_counter()
    code, _, err = run_ffmpeg(ffmpeg, command[1:], timeout=1800)
    elapsed = time.perf_counter() - started
    if code != 0 or not target.exists():
        return 0.0, 0, (err or "local encode failed")[-400:]
    size = target.stat().st_size
    target.unlink(missing_ok=True)
    return elapsed, size, ""


def remote_job(transport: Transport, source: Path, spec: JobSpec,
               destination: Path) -> dict:
    """Upload, submit, follow progress and download one clip."""
    uploaded = transport.upload(source)
    acknowledged = transport.submit(spec, asset_id=uploaded.asset_id)

    seen_progress = 0
    monotonic_ok = True
    last_pct = -1.0

    def on_frame(ftype: FrameType, body: dict) -> None:
        nonlocal seen_progress, last_pct
        if ftype is FrameType.PROGRESS:
            seen_progress += 1
            pct = float(body.get("pct", 0.0))
            if pct + 0.5 < last_pct:
                monotonic_ok = False
            last_pct = max(last_pct, pct)

    ftype, result = transport.await_result(on_frame=on_frame)
    if ftype is not FrameType.RESULT or result.get("status") != "ok":
        raise ProtocolError(f"remote job failed: {result.get('errors') or result}")

    download = transport.download(result["job_id"], destination)
    return {
        "result": result,
        "download": download,
        "upload_seconds": uploaded.seconds,
        "progress_frames": seen_progress,
        "progress_monotonic": monotonic_ok,
    }


def markdown_table(rows: list[dict]) -> str:
    header = (
        "| Clip | Size | Local (s) | Upload (s) | GPU (s) | Download (s) | "
        "Remote total (s) | Speedup | GPU-only | Net overhead |\n"
        "|---|---|---|---|---|---|---|---|---|---|\n"
    )
    lines = []
    for row in rows:
        lines.append(
            f"| {row['label']} | {row['input_bytes'] / (1024 * 1024):.1f} MiB | "
            f"{row['local_seconds']:.2f} | {row['upload_seconds']:.2f} | "
            f"{row['compute_seconds']:.2f} | {row['download_seconds']:.2f} | "
            f"{row['remote_total']:.2f} | **{row['speedup']:.2f}x** | "
            f"{row['compute_speedup']:.2f}x | {row['network_overhead_pct']:.1f}% |"
        )
    return header + "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local versus remote offload benchmark")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7575)
    parser.add_argument("--preset", default="720p", choices=sorted(PRESETS))
    parser.add_argument("--all", action="store_true",
                        help="benchmark every clip found in benchmarks/media")
    parser.add_argument("--repeat", type=int, default=1, help="runs per clip")
    parser.add_argument("--encoder", default=None,
                        help="force an encoder, e.g. h264_nvenc (default: best available)")
    parser.add_argument("--media-dir", default=str(MEDIA_DIR))
    parser.add_argument("--out", default=str(ROOT / "benchmarks" / "results.json"))
    parser.add_argument("--markdown", default=None, help="also write a Markdown table here")
    args = parser.parse_args(argv)

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("error: ffmpeg not found; a local baseline cannot be measured",
              file=sys.stderr)
        return 2

    media_dir = Path(args.media_dir)
    clips = sorted(media_dir.glob("*.mp4")) if args.all else sorted(
        media_dir.glob(f"sample-{args.preset}-*.mp4"))
    if not clips:
        print(f"error: no clips in {media_dir}\n"
              f"       run: python tools/make_sample_media.py --all", file=sys.stderr)
        return 2

    transport = Transport(args.host, args.port, client_name="rdo-bench")
    try:
        transport.connect()
    except (OSError, ProtocolError) as exc:
        print(f"error: cannot reach the worker at {args.host}:{args.port} - {exc}",
              file=sys.stderr)
        return 1

    caps = transport.capabilities
    encoder = args.encoder or ("h264_nvenc" if caps and "h264_nvenc" in caps.hardware_encoders
                               else "libx264")
    print(f"worker      : {transport.worker_id}")
    print(f"gpu         : {(caps.gpu_name if caps else '') or 'none detected'}")
    print(f"remote codec: {encoder}")
    print(f"local codec : {encoder if 'nvenc' not in encoder else 'libx264'}")
    print(f"clips       : {len(clips)}\n")

    try:
        latency = transport.ping_batch(count=25).to_dict()
    except (OSError, ProtocolError):
        latency = {}
    print(f"latency     : {latency.get('avg_ms', '?')} ms average\n")

    rows: list[dict] = []
    for clip in clips:
        for iteration in range(args.repeat):
            duration, resolution = probe_media(clip)
            spec = JobSpec(
                job_type="transcode",
                output_name=f"bench-{clip.stem}.mp4",
                resolution="source",
                video_codec=encoder,
                preset="p4" if encoder.endswith("_nvenc") else "veryfast",
                crf=23,
                audio_codec="aac",
            )
            # The honest local baseline is what this machine would run by itself:
            # a CPU software encode. Using NVENC locally is impossible - there is
            # no local GPU - so the comparison is CPU here against GPU there.
            local_spec = replace(
                spec,
                video_codec="libx264" if encoder.endswith("_nvenc") else encoder,
                preset="veryfast" if encoder.endswith("_nvenc") else spec.preset,
            )
            measurement = Measurement(
                label=f"{clip.stem} #{iteration + 1}",
                input_bytes=clip.stat().st_size,
                resolution=resolution,
                duration_s=round(duration, 2),
            )
            print(f"--- {measurement.label}  {resolution}  "
                  f"{measurement.input_bytes / (1024 * 1024):.1f} MiB  "
                  f"{duration:.1f}s of video")

            local_seconds, local_size, local_error = local_transcode(ffmpeg, clip, local_spec)
            if local_error:
                print(f"    local baseline failed: {local_error[:160]}")
            measurement.local_seconds = round(local_seconds, 4)
            measurement.encoder_local = local_spec.video_codec
            print(f"    local   : {local_seconds:8.2f}s  ({local_spec.video_codec})")

            try:
                remote = remote_job(transport, clip, spec, CLIENT_OUTPUT / f"{clip.stem}-remote.mp4")
                result, download = remote["result"], remote["download"]
                measurement.compute_seconds = result["compute_seconds"]
                measurement.upload_seconds = remote["upload_seconds"]
                measurement.download_seconds = download.seconds
                measurement.output_bytes = download.size
                measurement.encoder_remote = result.get("encoder", "")
                measurement.device_remote = result.get("device", "")
                measurement.upload_mbits = (
                    measurement.input_bytes * 8 / max(measurement.upload_seconds, 1e-9) / 1e6
                )
                measurement.download_mbits = download.megabits_per_s
                measurement.progress_frames = remote["progress_frames"]
                measurement.progress_monotonic = remote["progress_monotonic"]
                measurement.remote_total = round(
                    measurement.upload_seconds + measurement.compute_seconds
                    + measurement.download_seconds, 4)
                measurement.verified = (
                    download.sha256 == result.get("output_sha256") and download.verified)
                print(f"    upload  : {measurement.upload_seconds:8.2f}s "
                      f"({measurement.upload_mbits:.0f} Mbit/s)")
                print(f"    gpu     : {measurement.compute_seconds:8.2f}s on "
                      f"{measurement.device_remote or 'worker'}")
                print(f"    download: {measurement.download_seconds:8.2f}s "
                      f"({measurement.download_mbits:.0f} Mbit/s)")
                print(f"    remote  : {measurement.remote_total:8.2f}s   "
                      f"speedup {measurement.speedup:.2f}x "
                      f"(gpu-only {measurement.compute_speedup:.2f}x)")
                print(f"    progress: {remote['progress_frames']} frames, "
                      f"monotonic={remote['progress_monotonic']}, "
                      f"sha256 verified={measurement.verified}")
            except (OSError, ProtocolError) as exc:
                measurement.error = str(exc)
                print(f"    remote  : FAILED - {exc}")
            rows.append(measurement.to_dict())
            print()

    transport.close()

    good = [r for r in rows if not r["error"] and r["remote_total"] > 0]
    summary = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "client_host": socket.gethostname(),
        "platform": f"{platform.system()} {platform.release()}",
        "worker_id": transport.worker_id,
        "gpu": caps.gpu_name if caps else "",
        "nvenc": caps.nvenc_available if caps else False,
        "ffmpeg_version": caps.ffmpeg_version if caps else "",
        "encoder": encoder,
        "latency": latency,
        "runs": len(rows),
        "successful": len(good),
        "all_verified": all(r["verified"] for r in good) if good else False,
        "all_progress_monotonic": all(r["progress_monotonic"] for r in good) if good else False,
        "progress_frames_total": sum(r["progress_frames"] for r in rows),
        "mean_speedup": round(statistics.fmean(r["speedup"] for r in good), 3) if good else 0,
        "mean_compute_speedup": (
            round(statistics.fmean(r["compute_speedup"] for r in good), 3) if good else 0),
        "mean_network_overhead_pct": (
            round(statistics.fmean(r["network_overhead_pct"] for r in good), 2) if good else 0),
        "measurements": rows,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(markdown_table(rows))
    print(f"\nmean end-to-end speedup : {summary['mean_speedup']:.2f}x")
    print(f"mean GPU-only speedup   : {summary['mean_compute_speedup']:.2f}x")
    print(f"mean network overhead   : {summary['mean_network_overhead_pct']:.1f}%")
    print(f"checksums verified      : {summary['all_verified']}")
    print(f"progress monotonic      : {summary['all_progress_monotonic']} "
          f"({summary['progress_frames_total']} frames)")
    print(f"\nJSON written to {out}")

    if args.markdown:
        target = Path(args.markdown)
        target.write_text(markdown_table(rows) + "\n", encoding="utf-8")
        print(f"Markdown written to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())