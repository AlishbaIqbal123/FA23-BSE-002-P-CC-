"""Job specification validation and filename hardening."""

from __future__ import annotations

import pytest

from common.messages import (
    COMPUTE_OPS,
    JobSpec,
    NVENC_PRESETS,
    X264_PRESETS,
    safe_filename,
)


def test_default_transcode_spec_is_valid():
    assert JobSpec().validate() == []


def test_default_compute_spec_is_valid():
    assert JobSpec(job_type="compute").validate() == []


@pytest.mark.parametrize("preset", NVENC_PRESETS)
def test_every_nvenc_preset_is_accepted(preset):
    assert JobSpec(preset=preset).validate() == []


@pytest.mark.parametrize("preset", X264_PRESETS)
def test_every_x264_preset_is_accepted(preset):
    assert JobSpec(video_codec="libx264", preset=preset).validate() == []


def test_nvenc_preset_rejects_x264_names():
    errors = JobSpec(preset="veryfast").validate()
    assert any("NVENC preset" in error for error in errors)


def test_x264_preset_rejects_nvenc_names():
    spec = JobSpec(video_codec="libx264", preset="p4")
    assert any("libx264 preset" in error for error in spec.validate())


def test_unknown_job_type_is_rejected():
    assert JobSpec(job_type="mine").validate()


def test_unknown_resolution_is_rejected():
    assert JobSpec(resolution="4320p").validate()


def test_unknown_encoder_is_rejected():
    assert JobSpec(video_codec="h999_omg").validate()


def test_crf_out_of_range_is_rejected():
    assert JobSpec(crf=99).validate()
    assert JobSpec(crf=-1).validate()


def test_bitrate_out_of_range_is_rejected():
    assert JobSpec(bitrate_kbps=10).validate()
    assert JobSpec(bitrate_kbps=10_000_000).validate()


def test_zero_bitrate_means_use_crf_and_is_allowed():
    assert JobSpec(bitrate_kbps=0).validate() == []


def test_negative_asset_size_is_rejected():
    assert JobSpec(asset_size=-1).validate()


def test_too_many_extra_args_are_rejected():
    assert JobSpec(extra_args=[f"-x{i}" for i in range(20)]).validate()


def test_compute_size_bounds():
    assert JobSpec(job_type="compute", compute_size=32).validate()
    assert JobSpec(job_type="compute", compute_size=16384).validate()


def test_compute_iters_bounds():
    assert JobSpec(job_type="compute", compute_iters=0).validate()
    assert JobSpec(job_type="compute", compute_iters=501).validate()


def test_unknown_compute_op_is_rejected():
    assert JobSpec(job_type="compute", compute_op="teleport").validate()


def test_unknown_audio_codec_is_rejected():
    assert JobSpec(audio_codec="vorbis-on-top").validate()


def test_round_trip_through_a_dict():
    original = JobSpec(job_type="transcode", video_codec="hevc_nvenc", preset="p6",
                       resolution="1440p", bitrate_kbps=12000, crf=0)
    assert JobSpec.from_dict(original.to_dict()) == original


def test_from_dict_ignores_unknown_keys():
    spec = JobSpec.from_dict({"video_codec": "libx264", "preset": "fast", "evil": 1})
    assert spec.video_codec == "libx264"
    assert not hasattr(spec, "evil")


@pytest.mark.parametrize("operation", COMPUTE_OPS)
def test_every_compute_op_validates(operation):
    assert JobSpec(job_type="compute", compute_op=operation).validate() == []


class TestSafeFilename:
    def test_plain_name_is_unchanged(self):
        assert safe_filename("holiday.mp4") == "holiday.mp4"

    def test_directory_traversal_is_stripped(self):
        assert safe_filename("../../etc/passwd") == "passwd"
        assert safe_filename(r"C:\Windows\System32\evil.dll") == "evil.dll"

    def test_forward_slashes_are_stripped(self):
        assert safe_filename("a/b/c.mp4") == "c.mp4"

    def test_spaces_and_symbols_are_replaced(self):
        assert safe_filename("my clip (final)!.mp4") == "my_clip_final_.mp4"

    def test_empty_falls_back(self):
        assert safe_filename("") == "asset.bin"
        assert safe_filename("...") == "asset.bin"

    def test_leading_dots_are_removed(self):
        assert safe_filename(".hidden.mp4") == "hidden.mp4"

    def test_name_is_length_capped(self):
        assert len(safe_filename("x" * 500 + ".mp4")) <= 180