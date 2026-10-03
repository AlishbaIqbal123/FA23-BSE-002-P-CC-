"""Capability probing and the subnet allow-list."""

from __future__ import annotations

from server.capabilities import available_encoders, build_capabilities, find_ffmpeg
from server.session import ip_allowed

LOOPBACK = ["127.0.0.0/8", "::1/128", "192.168.1.0/24"]


class TestAllowList:
    def test_loopback_is_permitted_by_default(self):
        assert ip_allowed("127.0.0.1", LOOPBACK)

    def test_the_documented_worker_address_is_permitted(self):
        assert ip_allowed("192.168.1.1", LOOPBACK)
        assert ip_allowed("192.168.1.2", LOOPBACK)

    def test_a_different_private_network_is_refused(self):
        assert not ip_allowed("10.0.0.5", LOOPBACK)
        assert not ip_allowed("172.16.4.9", LOOPBACK)

    def test_a_public_address_is_refused(self):
        assert not ip_allowed("8.8.8.8", LOOPBACK)

    def test_ipv6_loopback_is_permitted(self):
        assert ip_allowed("::1", LOOPBACK)

    def test_ipv4_mapped_into_the_allowed_v4_range(self):
        assert ip_allowed("192.168.1.55", LOOPBACK)

    def test_the_boundary_addresses_are_included(self):
        assert ip_allowed("192.168.1.0", LOOPBACK)
        assert ip_allowed("192.168.1.255", LOOPBACK)
        assert not ip_allowed("192.168.2.0", LOOPBACK)

    def test_garbage_is_refused_not_crashed(self):
        assert not ip_allowed("not-an-ip", LOOPBACK)
        assert not ip_allowed("", LOOPBACK)

    def test_a_malformed_subnet_entry_is_skipped(self):
        assert ip_allowed("192.168.1.4", ["not-a-cidr", "192.168.1.0/24"])

    def test_open_mode_permits_everything(self):
        assert ip_allowed("203.0.113.7", ["0.0.0.0/0", "::/0"])


class TestCapabilities:
    def test_defaults_are_safe_when_nothing_is_installed(self):
        caps = build_capabilities(max_concurrent_jobs=4, ffmpeg_path="definitely-not-here")
        assert caps.hardware_encoders == []
        assert caps.nvenc_available is False
        assert caps.max_concurrent_jobs == 4
        assert caps.worker_id

    def test_worker_ids_are_unique_per_call(self):
        first = build_capabilities(ffmpeg_path="nope")
        second = build_capabilities(ffmpeg_path="nope")
        assert first.worker_id != second.worker_id

    def test_compute_engine_is_always_offered(self):
        caps = build_capabilities(ffmpeg_path="definitely-not-here")
        assert "compute" in caps.engines

    def test_to_dict_is_json_serialisable(self):
        import json

        payload = build_capabilities(ffmpeg_path="nope").to_dict()
        assert json.loads(json.dumps(payload))["protocol_version"]


class TestEncoderDiscovery:
    def test_a_missing_binary_yields_nothing(self):
        assert available_encoders("definitely-not-ffmpeg") == set()

    def test_a_real_build_reports_x264(self):
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            return
        found = available_encoders(ffmpeg)
        if found:
            assert "libx264" in found or "mpeg4" in found

    def test_the_legend_lines_are_not_mistaken_for_encoders(self):
        # " V..... = Video" must not be parsed as an encoder named "=".
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            return
        assert "=" not in available_encoders(ffmpeg)