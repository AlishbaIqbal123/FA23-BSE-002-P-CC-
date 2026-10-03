"""Generate synthetic test clips so the project can be demonstrated without
supplying copyrighted footage.

    python tools/make_sample_media.py                 # the default ladder
    python tools/make_sample_media.py --preset 4k --seconds 20
    python tools/make_sample_media.py --list

``testsrc2`` burns a frame counter and a moving pattern into every frame, which
makes it obvious at a glance whether the returned file really is the one that
was sent - useful evidence when grading a networked pipeline.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common.paths import CREATE_NO_WINDOW  # noqa: E402,F401
from server.capabilities import available_encoders, find_ffmpeg, run_ffmpeg  # noqa: E402

PRESETS = {
    "360p": (640, 360),
    "480p": (854, 480),
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "4k": (3840, 2160),
}

#: Bitrate used by ``--hq``: 40 Mbit/s, so a 20 s 1080p clip is roughly 100 MiB.
HQ_BITRATE = "40000k"


def available_filter(ffmpeg: str, name: str) -> bool:
    """True when this ffmpeg build provides the named lavfi source filter.

    Old builds (anything before FFmpeg 2.1) have ``testsrc`` but not
    ``testsrc2``, and some minimal builds lack ``sine`` as well.
    """
    code, out, err = run_ffmpeg(ffmpeg, ["-filters"], timeout=30)
    if code != 0:
        return False
    for line in (out + err).splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == name:
            return True
    return False


def video_source(ffmpeg: str, width: int, height: int, fps: int, seconds: int) -> list[str]:
    """Pick the richest synthetic source this build actually supports."""
    if available_filter(ffmpeg, "testsrc2"):
        pattern = f"testsrc2=size={width}x{height}:rate={fps}:duration={seconds}"
    elif available_filter(ffmpeg, "testsrc"):
        pattern = f"testsrc=size={width}x{height}:rate={fps}:duration={seconds}"
    elif available_filter(ffmpeg, "color"):
        pattern = f"color=c=navy:size={width}x{height}:rate={fps}:duration={seconds}"
    else:
        raise RuntimeError("this ffmpeg build has no usable synthetic video source")
    return ["-f", "lavfi", "-i", pattern]


def audio_source(ffmpeg: str, seconds: int) -> list[str]:
    if available_filter(ffmpeg, "sine"):
        return ["-f", "lavfi", "-i",
                f"sine=frequency=440:sample_rate=48000:duration={seconds}"]
    return []


def build_command(ffmpeg: str, width: int, height: int, seconds: int, fps: int,
                  out: Path, video_codec: str, audio_codec: str,
                  hq: bool = False) -> list[str]:
    command = [ffmpeg, "-y", *video_source(ffmpeg, width, height, fps, seconds)]
    has_audio = bool(audio_codec)
    if has_audio:
        command += audio_source(ffmpeg, seconds)
    command += [
        "-c:v", video_codec, "-pix_fmt", "yuv420p", "-g", str(fps * 2),
    ]
    if video_codec.startswith("libx26"):
        # hq injects noise and pins a high bitrate. Both are needed: x264 treats a
        # bitrate as a target rather than a floor, and a synthetic test pattern is
        # so compressible that a nominal 40 Mbit/s encode lands around 1 Mbit/s.
        # The result is a predictable tens-of-MiB payload, which is the only way to
        # make the transfer phases measurable - a CRF 23 test pattern is a few
        # hundred kilobytes and finishes before the first progress frame is sent.
        if hq:
            command += ["-vf", "noise=alls=26:allf=t+u"]
            quality = ["-preset", "veryfast", "-b:v", HQ_BITRATE]
        else:
            quality = ["-preset", "veryfast", "-crf", "23"]
        command[command.index("-pix_fmt"):command.index("-pix_fmt")] = quality
    if has_audio:
        command += ["-c:a", audio_codec, "-b:a", "192k" if hq else "128k", "-shortest"]
    else:
        command += ["-an"]
    command += ["-movflags", "+faststart", str(out)]
    return command


def codec_candidates(ffmpeg: str) -> tuple[list[str], list[str]]:
    """Video and audio encoders to try, best first, limited to what exists here."""
    have = available_encoders(ffmpeg)
    video = [c for c in ("libx264", "mpeg4") if c in have] or ["mpeg4"]
    audio = [c for c in ("aac", "libvo_aacenc", "libmp3lame", "libopus") if c in have]
    return video, audio + [None]  # type: ignore[list-item]


def make_clip(ffmpeg: str, preset: str, seconds: int, fps: int, out_dir: Path,
              hq: bool = False) -> Path:
    width, height = PRESETS[preset]
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "-hq" if hq else ""
    target = out_dir / f"sample-{preset}-{seconds}s{suffix}.mp4"
    videos, audios = codec_candidates(ffmpeg)
    print(f"[make] {preset:>5}  {width}x{height}  {seconds}s  -> {target.name}")

    failures: list[str] = []
    for video_codec in videos:
        for audio_codec in audios:
            target.unlink(missing_ok=True)
            command = build_command(ffmpeg, width, height, seconds, fps, target,
                                    video_codec, audio_codec, hq=hq)
            code, _, err = run_ffmpeg(ffmpeg, command[1:], timeout=max(180, seconds * 60))
            if code == 0 and target.exists() and target.stat().st_size > 0:
                size = target.stat().st_size
                print(f"[make]       {video_codec}/{audio_codec or 'no audio'}  "
                      f"{size / (1024 * 1024):.2f} MiB")
                return target
            failures.append(f"{video_codec}/{audio_codec}: {(err or '').strip()[-300:]}")

    raise RuntimeError("every codec combination failed:\n  " + "\n  ".join(failures[:4]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate synthetic benchmark clips")
    parser.add_argument("--preset", default="720p", choices=sorted(PRESETS),
                        help="a single preset to build")
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--out", default=str(ROOT / "benchmarks" / "media"))
    parser.add_argument("--all", action="store_true",
                        help="build the full ladder used by the report")
    parser.add_argument("--hq", action="store_true",
                        help="near-lossless encode (tens of MiB) for transfer measurements")
    parser.add_argument("--list", action="store_true", help="list presets and exit")
    args = parser.parse_args(argv)

    if args.list:
        for name, (width, height) in sorted(PRESETS.items()):
            print(f"{name:>6}  {width}x{height}")
        return 0

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("error: ffmpeg was not found on PATH. Install it and retry.\n"
              "       Windows: https://www.gyan.dev/ffmpeg/builds/\n"
              "       Debian : sudo apt install ffmpeg", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    presets = ["360p", "720p", "1080p", "4k"] if args.all else [args.preset]
    try:
        for preset in presets:
            make_clip(ffmpeg, preset, args.seconds, args.fps, out_dir, hq=args.hq)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"\nDone. Clips are in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())