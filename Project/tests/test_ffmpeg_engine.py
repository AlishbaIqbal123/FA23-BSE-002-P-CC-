"""FFmpeg command construction, progress parsing and encoder fallback logic."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from common.messages import JobSpec
from server.engines.ffmpeg_engine import (
    FFmpegEngine,
    build_command,
    parse_progress_line,
    probe_duration,
)
from server.engines.base import JobContext


class Recorder:
    """Collects progress reports and log lines from an engine."""

    def __init__(self) -> None:
        self.progress: list[tuple[float, str, str]] = []
        self.logs: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    def report(self, pct, stage, detail="", **extra):
        with self.lock:
            self.progress.append((pct, stage, detail))

    def __call__(self, level, message):
        with self.lock:
            self.logs.append((level, message))

    @property
    def last_pct(self) -> float:
        return self.progress[-1][0] if self.progress else -1.0


def make_context(tmp_path: Path, **overrides) -> JobContext:
    spec = JobSpec(**overrides)
    recorder = Recorder()
    source = tmp_path / "in.mp4"
    source.write_bytes(b"not really a video")
    return JobContext(job_id="j1", spec=spec, input_path=source,
                      output_path=tmp_path / "out.mp4",
                      progress=recorder.report, log=recorder)


class TestProgressLineParsing:
    def test_simple_pair(self):
        assert parse_progress_line("frame=120") == {"frame": "120"}

    def test_value_may_contain_spaces(self):
        assert parse_progress_line("out_time_us=1500000") == {"out_time_us": "1500000"}

    def test_speed_value(self):
        assert parse_progress_line("speed=2.34x") == {"speed": "2.34x"}

    def test_blank_line_yields_nothing(self):
        assert parse_progress_line("   ") == {}

    def test_line_without_an_equals_sign_yields_nothing(self):
        assert parse_progress_line("progress continue") == {}

    def test_ffmpeg_block_marker_is_parsed_normally(self):
        assert parse_progress_line("progress=continue") == {"progress": "continue"}

    def test_equals_inside_the_value_is_preserved(self):
        assert parse_progress_line("encoder=a=b") == {"encoder": "a=b"}

    def test_whitespace_around_the_key_is_trimmed(self):
        assert parse_progress_line("  fps = 29.97 ") == {"fps": "29.97"}


class TestCommandBuilding:
    def test_nvenc_path_uses_the_hardware_encoder(self, tmp_path):
        cmd = build_command("ffmpeg", JobSpec(video_codec="h264_nvenc", preset="p4",
                                              crf=21), tmp_path / "i.mp4", tmp_path / "o.mp4")
        assert "h264_nvenc" in cmd
        assert cmd[cmd.index("-preset") + 1] == "p4"
        assert cmd[cmd.index("-cq:v") + 1] == "21"
        assert "-b:v" not in cmd

    def test_nvenc_bitrate_mode_replaces_cq(self, tmp_path):
        cmd = build_command("ffmpeg", JobSpec(video_codec="hevc_nvenc", preset="p2",
                                              bitrate_kbps=6000), tmp_path / "i.mp4",
                            tmp_path / "o.mp4")
        assert "hevc_nvenc" in cmd
        assert cmd[cmd.index("-b:v") + 1] == "6000k"
        assert "-cq:v" not in cmd

    def test_x264_path_uses_crf_not_cq(self, tmp_path):
        cmd = build_command("ffmpeg", JobSpec(video_codec="libx264", preset="fast",
                                              crf=20), tmp_path / "i.mp4", tmp_path / "o.mp4")
        assert cmd[cmd.index("-c:v") + 1] == "libx264"
        assert cmd[cmd.index("-crf") + 1] == "20"
        assert "-cq:v" not in cmd

    def test_resolution_scales_to_the_preset_when_the_source_is_unknown(self, tmp_path):
        cmd = build_command("ffmpeg", JobSpec(resolution="720p"), tmp_path / "i.mp4",
                            tmp_path / "o.mp4")
        assert cmd[cmd.index("-vf") + 1] == "scale=1280:720"

    def test_scale_keeps_the_aspect_ratio_and_never_uses_the_minus_two_shorthand(self, tmp_path):
        # 1920x1080 into a 1280-wide box is 1280x720; the old "scale=1280:-2" form is
        # rejected outright by pre-2015 ffmpeg builds.
        cmd = build_command("ffmpeg", JobSpec(resolution="720p"), tmp_path / "i.mp4",
                            tmp_path / "o.mp4", source=(1920, 1080))
        value = cmd[cmd.index("-vf") + 1]
        assert value == "scale=1280:720"
        assert "-2" not in value

    def test_scale_rounds_an_odd_aspect_ratio_to_even_dimensions(self, tmp_path):
        # 1000x721 exceeds the 720-tall box, so it is scaled, and the computed 721-pixel
        # height is rounded up to an even 722.
        cmd = build_command("ffmpeg", JobSpec(resolution="720p"), tmp_path / "i.mp4",
                            tmp_path / "o.mp4", source=(1000, 721))
        value = cmd[cmd.index("-vf") + 1]
        width, height = (int(part) for part in value.removeprefix("scale=").split(":"))
        assert width % 2 == 0 and height % 2 == 0
        assert (width, height) == (1000, 722)

    def test_a_small_source_is_not_upscaled(self, tmp_path):
        cmd = build_command("ffmpeg", JobSpec(resolution="1080p"), tmp_path / "i.mp4",
                            tmp_path / "o.mp4", source=(640, 360))
        assert "-vf" not in cmd

    def test_a_portrait_source_is_bounded_by_its_height(self, tmp_path):
        # 1080x1920 into a 720-tall box keeps the portrait shape; the width follows
        # from the aspect ratio and is rounded down to an even number.
        cmd = build_command("ffmpeg", JobSpec(resolution="720p"), tmp_path / "i.mp4",
                            tmp_path / "o.mp4", source=(1080, 1920))
        assert cmd[cmd.index("-vf") + 1] == "scale=406:720"

    def test_source_resolution_adds_no_filter(self, tmp_path):
        cmd = build_command("ffmpeg", JobSpec(resolution="source"), tmp_path / "i.mp4",
                            tmp_path / "o.mp4")
        assert "-vf" not in cmd

    def test_an_experimental_audio_codec_enables_the_strict_flag(self, tmp_path):
        cmd = build_command("ffmpeg", JobSpec(audio_codec="aac"), tmp_path / "i.mp4",
                            tmp_path / "o.mp4")
        assert cmd[cmd.index("-strict") + 1] == "-2"

    def test_the_command_never_passes_hide_banner(self, tmp_path):
        # Pre-2.x ffmpeg rejects the option outright, which would break every
        # candidate in the encoder fallback chain at once.
        for codec in ("h264_nvenc", "libx264", "mpeg4"):
            cmd = build_command("ffmpeg", JobSpec(video_codec=codec), tmp_path / "i.mp4",
                                tmp_path / "o.mp4")
            assert "-hide_banner" not in cmd

    def test_audio_none_disables_the_audio_stream(self, tmp_path):
        cmd = build_command("ffmpeg", JobSpec(audio_codec="none"), tmp_path / "i.mp4",
                            tmp_path / "o.mp4")
        assert "-an" in cmd
        assert "-c:a" not in cmd

    def test_audio_copy_passes_the_stream_through(self, tmp_path):
        cmd = build_command("ffmpeg", JobSpec(audio_codec="copy"), tmp_path / "i.mp4",
                            tmp_path / "o.mp4")
        assert cmd[cmd.index("-c:a") + 1] == "copy"

    def test_progress_pipe_is_always_requested(self, tmp_path):
        cmd = build_command("ffmpeg", JobSpec(), tmp_path / "i.mp4", tmp_path / "o.mp4")
        assert cmd[cmd.index("-progress") + 1] == "pipe:1"
        assert "-nostats" in cmd

    def test_stdin_is_closed_so_ffmpeg_cannot_steal_console_input(self, tmp_path):
        cmd = build_command("ffmpeg", JobSpec(), tmp_path / "i.mp4", tmp_path / "o.mp4")
        assert "-nostdin" in cmd

    def test_extra_arguments_are_appended_before_the_output(self, tmp_path):
        cmd = build_command("ffmpeg", JobSpec(extra_args=["-g", "48"]), tmp_path / "i.mp4",
                            tmp_path / "o.mp4")
        assert cmd.index("-g") < len(cmd) - 1
        assert cmd[-1].endswith("o.mp4")


class TestEncoderFallback:
    def test_nvenc_is_always_tried_first(self):
        engine = FFmpegEngine("ffmpeg", ["h264_nvenc", "hevc_nvenc", "h264_qsv", "libx264"])
        assert engine.candidates("h264_nvenc")[0] == "h264_nvenc"

    def test_chain_ends_at_a_software_encoder(self):
        engine = FFmpegEngine("ffmpeg", ["h264_nvenc", "libx264", "mpeg4"])
        chain = engine.candidates("h264_nvenc")
        assert chain[-1] in ("libx264", "mpeg4")

    def test_only_encoders_present_in_the_build_are_offered(self):
        engine = FFmpegEngine("ffmpeg", ["libx264"])
        assert engine.candidates("h264_nvenc") == ["libx264"]

    def test_an_unknown_still_yields_the_request(self):
        engine = FFmpegEngine("ffmpeg", [])
        assert engine.candidates("h264_nvenc") == ["h264_nvenc"]

    def test_engine_reports_unavailable_without_a_binary(self):
        assert FFmpegEngine("", ["libx264"]).available() is False

    def test_engine_is_available_with_a_binary_and_encoders(self):
        assert FFmpegEngine("ffmpeg", ["libx264"]).available() is True


class TestMissingBinary:
    def test_run_without_ffmpeg_reports_an_error(self, tmp_path):
        ctx = make_context(tmp_path)
        engine = FFmpegEngine("", [])
        result = engine.run(ctx)
        assert result.status == "error"
        assert "ffmpeg" in result.error

    def test_a_cancelled_job_stops_before_launching(self, tmp_path):
        ctx = make_context(tmp_path)
        ctx.cancel()
        engine = FFmpegEngine("ffmpeg-that-does-not-exist", ["libx264"])
        result = engine.run(ctx)
        assert result.status == "cancelled"


class TestCancellation:
    def test_cancel_event_is_visible_to_the_engine(self, tmp_path):
        ctx = make_context(tmp_path)
        assert ctx.cancelled is False
        ctx.cancel()
        assert ctx.cancelled is True

    def test_each_context_gets_its_own_event(self, tmp_path):
        first, second = make_context(tmp_path), make_context(tmp_path)
        first.cancel()
        assert second.cancelled is False


class TestDurationProbing:
    def test_missing_input_yields_zero(self):
        assert probe_duration("ffmpeg-does-not-exist", Path("nope.mp4")) == 0.0

    def test_garbage_input_yields_zero(self, tmp_path):
        bogus = tmp_path / "not-a-video.mp4"
        bogus.write_bytes(b"\x00\x01\x02")
        assert probe_duration("ffmpeg-does-not-exist", bogus) == 0.0


def test_progress_adapter_drops_unknown_fields():
    """An engine that invents a keyword must not crash the session."""
    from common.messages import Progress

    captured: dict = {}

    class FakeSession:
        halt = threading.Event()

        def emit(self, ftype, payload):
            captured.update(payload)

    from server.session import Session

    adapter = Session._progress_adapter(FakeSession(), "job-7")
    adapter(50.0, "encoding", "half way", fps=30.0, nonsense="ignored", speed="1.5x")
    assert captured["job_id"] == "job-7"
    assert captured["pct"] == 50.0
    assert captured["fps"] == 30.0
    assert "nonsense" not in captured
    assert Progress.__dataclass_fields__


def test_progress_adapter_clamps_the_percentage():
    from server.session import Session

    captured: dict = {}

    class FakeSession:
        halt = threading.Event()

        def emit(self, ftype, payload):
            captured.update(payload)

    adapter = Session._progress_adapter(FakeSession(), "j")
    adapter(-5.0, "s", "")
    assert captured["pct"] == 0.0
    adapter(120.0, "s", "")
    assert captured["pct"] == 100.0


def test_progress_adapter_is_silent_after_halt():
    from server.session import Session

    class FakeSession:
        halt = threading.Event()

        def __init__(self):
            self.sent = 0

        def emit(self, ftype, payload):
            self.sent += 1

    session = FakeSession()
    session.halt.set()
    Session._progress_adapter(session, "j")(50.0, "s", "")
    assert session.sent == 0