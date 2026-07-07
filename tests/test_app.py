import unittest
import json
import tempfile
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import app
from tools import install_ffmpeg_multikey


class FfmpegCommandTests(unittest.TestCase):
    def config(self, **overrides):
        cfg = dict(app.DEFAULTS)
        cfg["ffmpeg"] = "ffmpeg"
        cfg.update(overrides)
        return cfg

    def test_drm_keys_and_provider_like_flags_are_input_options(self):
        keys = "00" * 16 + ":" + "11" * 16
        command = app.ffmpeg_command(
            "https://example.invalid/manifest.mpd",
            self.config(),
            decryption_keys=keys,
        )

        self.assertIn("-cenc_decryption_keys", command)
        self.assertLess(command.index("-cenc_decryption_keys"), command.index("-i"))
        self.assertLess(command.index("-fflags"), command.index("-i"))
        self.assertLess(command.index("-avioflags"), command.index("-i"))
        self.assertIn("+genpts+nobuffer", command)
        self.assertIn("direct", command)
        video_map_at = command.index("-map")
        self.assertEqual(command[video_map_at + 1], "0:v:0")

    def test_custom_drm_input_args_are_still_supported(self):
        keys = "00" * 16 + ":" + "11" * 16
        command = app.ffmpeg_command(
            "https://example.invalid/manifest.mpd",
            self.config(ffmpeg_drm_input_args=["-fflags", "+genpts"]),
            decryption_keys=keys,
        )

        self.assertLess(command.index("-fflags"), command.index("-i"))
        self.assertIn("+genpts", command)

    def test_event_input_args_are_kept_before_input_url(self):
        keys = "00" * 16 + ":" + "11" * 16
        command = app.ffmpeg_command(
            "https://example.invalid/manifest.mpd",
            self.config(),
            decryption_keys=keys,
            event_input_args=["-headers", "Origin: https://provider.invalid\r\n"],
        )

        self.assertLess(command.index("-headers"), command.index("-i"))
        self.assertIn("Origin: https://provider.invalid\r\n", command)

    def test_temporary_override_args_are_inserted_in_expected_sections(self):
        keys = "00" * 16 + ":" + "11" * 16
        command = app.ffmpeg_command(
            "https://example.invalid/manifest.mpd",
            self.config(),
            decryption_keys=keys,
            override_drm_input_args=["-probesize", "64k"],
            override_input_args=["-analyzeduration", "0"],
            override_output_args=["-max_interleave_delta", "0"],
        )

        self.assertLess(command.index("-probesize"), command.index("-i"))
        self.assertLess(command.index("-analyzeduration"), command.index("-i"))
        self.assertGreater(command.index("-max_interleave_delta"), command.index("-c"))

    def test_main_ffmpeg_sections_are_configurable(self):
        keys = "00" * 16 + ":" + "11" * 16
        command = app.ffmpeg_command(
            "https://example.invalid/manifest.mpd",
            self.config(
                ffmpeg_base_args=["-nostdin", "-loglevel", "error"],
                ffmpeg_reconnect_args=["-reconnect", "0"],
                ffmpeg_map_args=["-map", "0:v:1"],
                ffmpeg_codec_args=["-c:v", "copy", "-c:a", "copy"],
                ffmpeg_drm_output_args=["-copyts"],
                ffmpeg_drm_mpegts_flags="+initial_discontinuity",
                ffmpeg_mpegts_output_args=["-muxdelay", "0.25", "-f", "mpegts"],
            ),
            decryption_keys=keys,
        )

        self.assertNotIn("-hide_banner", command)
        self.assertEqual(command[1:4], ["-nostdin", "-loglevel", "error"])
        self.assertIn("-reconnect", command)
        self.assertIn("0:v:1", command)
        self.assertIn("-c:v", command)
        self.assertIn("-copyts", command)
        self.assertIn("+initial_discontinuity", command)
        self.assertEqual(command[-1], "pipe:1")

    def test_ffmpeg_config_args_can_be_strings(self):
        command = app.ffmpeg_command(
            "https://example.invalid/manifest.mpd",
            self.config(ffmpeg_base_args="-nostdin -loglevel fatal"),
        )

        self.assertEqual(command[1:4], ["-nostdin", "-loglevel", "fatal"])

    def test_streamlink_drm_command_uses_two_provider_keys(self):
        keys = (
            "00" * 16 + "=" + "11" * 16 + ":"
            + "22" * 16 + "=" + "33" * 16
        )
        command = app.streamlink_drm_command(
            "https://example.invalid/manifest.mpd",
            self.config(
                streamlink_drm="streamlink-drm",
                streamlink_stream="best",
                streamlink_extra_args=["--stream-segment-threads", "2"],
            ),
            decryption_keys=keys,
        )

        self.assertEqual(command[0], "streamlink-drm")
        self.assertIn("--stdout", command)
        self.assertIn("--ffmpeg-fout", command)
        self.assertIn("mpegts", command)
        self.assertIn("--stream-segment-threads", command)
        self.assertIn("-decryption_key", command)
        self.assertIn("11" * 16, command)
        self.assertNotIn("00" * 16 + ":" + "11" * 16, command)
        self.assertIn("-decryption_key_2", command)
        self.assertIn("33" * 16, command)
        self.assertNotIn("22" * 16 + ":" + "33" * 16, command)
        self.assertEqual(command[-2:], ["https://example.invalid/manifest.mpd", "best"])

    def test_stream_command_selects_streamlink_drm_engine(self):
        engine, command = app.stream_command(
            "https://example.invalid/manifest.mpd",
            self.config(stream_engine="streamlink_drm", streamlink_drm="streamlink-drm"),
            decryption_keys="00" * 16 + "=" + "11" * 16,
        )

        self.assertEqual(engine, "streamlink-drm")
        self.assertEqual(command[0], "streamlink-drm")

    def test_streamlink_drm_command_can_reverse_provider_keys(self):
        keys = (
            "00" * 16 + "=" + "11" * 16 + ":"
            + "22" * 16 + "=" + "33" * 16
        )
        command = app.streamlink_drm_command(
            "https://example.invalid/manifest.mpd",
            self.config(streamlink_drm="streamlink-drm", streamlink_reverse_keys=True),
            decryption_keys=keys,
        )

        self.assertLess(command.index("33" * 16), command.index("11" * 16))

    def test_streamlink_drm_command_can_use_first_key_for_both_tracks(self):
        keys = (
            "00" * 16 + "=" + "11" * 16 + ":"
            + "22" * 16 + "=" + "33" * 16
        )
        command = app.streamlink_drm_command(
            "https://example.invalid/manifest.mpd",
            self.config(streamlink_drm="streamlink-drm", streamlink_key_mode="first"),
            decryption_keys=keys,
        )

        self.assertIn("-decryption_key", command)
        self.assertIn("11" * 16, command)
        self.assertNotIn("-decryption_key_2", command)
        self.assertNotIn("33" * 16, command)

    def test_mask_streamlink_drm_command_hides_provider_keys(self):
        command = [
            "streamlink-drm",
            "-decryption_key",
            "11" * 16,
            "-decryption_key_2",
            "33" * 16,
        ]

        masked = app.mask_ffmpeg_command(command)

        self.assertIn("<masked key(s)>", masked)
        self.assertNotIn("11" * 16, masked)
        self.assertNotIn("33" * 16, masked)


class FfmpegInstallerTests(unittest.TestCase):
    def test_architecture_tokens_cover_common_linux_names(self):
        self.assertEqual(
            install_ffmpeg_multikey.architecture_tokens("x86_64"),
            ("amd64", "x86_64", "x64"),
        )
        self.assertEqual(
            install_ffmpeg_multikey.architecture_tokens("aarch64"),
            ("arm64", "aarch64"),
        )
        self.assertEqual(
            install_ffmpeg_multikey.architecture_tokens("arm", "7"),
            ("armv7", "armhf", "arm-7", "armv7l"),
        )

    def test_asset_score_accepts_matching_static_linux_archive(self):
        tokens = ("amd64", "x86_64", "x64")

        self.assertGreater(
            install_ffmpeg_multikey.asset_score(
                "ffmpeg-linux-amd64-static.tar.xz", tokens
            ),
            0,
        )
        self.assertEqual(
            install_ffmpeg_multikey.asset_score(
                "ffmpeg-windows-amd64-static.zip", tokens
            ),
            -1,
        )
        self.assertEqual(
            install_ffmpeg_multikey.asset_score(
                "ffmpeg-linux-arm64-static.tar.xz", tokens
            ),
            -1,
        )

    def test_dockerfile_installs_ffmpeg_before_copying_application_code(self):
        dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(
            encoding="utf-8"
        )

        self.assertLess(
            dockerfile.index(
                "RUN python /app/tools/install_ffmpeg_multikey.py "
                "--install-dir /opt/ffmpeg"
            ),
            dockerfile.index("COPY app.py /app/app.py"),
        )


class ManifestProbeTests(unittest.TestCase):
    def config(self, **overrides):
        cfg = dict(app.DEFAULTS)
        cfg.update(overrides)
        return cfg

    def fake_response(self, body=b"", status_error=None, headers=None):
        class Response:
            def __init__(self):
                self.headers = headers or {}

            def raise_for_status(self):
                if status_error:
                    raise status_error

            def iter_content(self, chunk_size=1):
                yield body[:chunk_size]

            def close(self):
                pass

        return Response()

    def with_fake_get(self, response, callback):
        old_get = app.requests.get
        try:
            app.requests.get = lambda *args, **kwargs: response
            return callback()
        finally:
            app.requests.get = old_get

    def test_manifest_probe_rejects_empty_mpd_response(self):
        with self.assertRaisesRegex(RuntimeError, "Manifest ist leer"):
            self.with_fake_get(
                self.fake_response(b""),
                lambda: app.validate_manifest_url(
                    "http://media.invalid/live/event.mpd",
                    self.config(
                        manifest_probe_failure_mode="block",
                        manifest_probe_attempts=1,
                    ),
                ),
            )

    def test_manifest_probe_warn_mode_does_not_block_stream_start(self):
        self.with_fake_get(
            self.fake_response(b""),
            lambda: app.validate_manifest_url(
                "http://media.invalid/live/event.mpd",
                self.config(manifest_probe_failure_mode="warn", manifest_probe_attempts=1),
            ),
        )

    def test_manifest_probe_accepts_mpd_response(self):
        self.with_fake_get(
            self.fake_response(
                b'<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011"/>',
                headers={"content-type": "application/dash+xml"},
            ),
            lambda: app.validate_manifest_url(
                "http://media.invalid/live/event.mpd", self.config()
            ),
        )


class PipeParserTests(unittest.TestCase):
    def test_pipe_parser_keeps_safe_input_args_and_uses_last_input(self):
        line = (
            "pipe://bash -c 'ffmpeg -headers \"Origin: https://provider.invalid\\r\\n\" "
            "-user_agent \"Provider Player\" "
            "-cenc_decryption_keys $(curl -s -i \"https://license.invalid/key\") "
            "-i \"https://media.invalid/manifest.mpd\" -c copy -f mpegts pipe:1'"
        )

        media_url, license_url, input_args = app.parse_pipe_stream(line)

        self.assertEqual(media_url, "https://media.invalid/manifest.mpd")
        self.assertEqual(license_url, "https://license.invalid/key")
        self.assertEqual(
            input_args,
            [
                "-headers",
                "Origin: https://provider.invalid\\r\\n",
                "-user_agent",
                "Provider Player",
            ],
        )


class LicenseParserTests(unittest.TestCase):
    def test_raw_hex_keys_are_accepted(self):
        keys = "00" * 16 + ":" + "11" * 16

        self.assertEqual(app.normalize_decryption_keys(keys), "00" * 16 + "=" + "11" * 16)

    def test_embedded_hex_keys_are_extracted_from_shell_like_text(self):
        keys = "00" * 16 + ":" + "11" * 16

        self.assertEqual(
            app.normalize_decryption_keys(f"d={keys}; echo ignored"),
            "00" * 16 + "=" + "11" * 16,
        )

    def test_telerising_equals_key_format_is_converted(self):
        self.assertEqual(
            app.normalize_decryption_keys(
                "00000000000000000000000000000000=00000000000000000000000000000000"
            ),
            "00000000000000000000000000000000=00000000000000000000000000000000",
        )

    def test_multiple_telerising_equals_keys_are_colon_separated(self):
        self.assertEqual(
            app.normalize_decryption_keys(
                "802868297ab2d6bd1a3a7c96f50d64c7=7cae669866af294483543d58ed9a6b63:"
                "b770d5b4bb6b594daf985845aae9aa5f=b0cb46d2d31cf044bc73db71e9865f6f"
            ),
            "802868297ab2d6bd1a3a7c96f50d64c7=7cae669866af294483543d58ed9a6b63:"
            "b770d5b4bb6b594daf985845aae9aa5f=b0cb46d2d31cf044bc73db71e9865f6f",
        )

    def test_decryption_key_count_handles_equals_format(self):
        self.assertEqual(
            app.decryption_key_count(
                "802868297ab2d6bd1a3a7c96f50d64c7=7cae669866af294483543d58ed9a6b63:"
                "b770d5b4bb6b594daf985845aae9aa5f=b0cb46d2d31cf044bc73db71e9865f6f"
            ),
            2,
        )

    def test_clearkey_json_is_converted_to_ffmpeg_hex_keys(self):
        payload = {
            "keys": [
                {
                    "kty": "oct",
                    "kid": "ABEiM0RVZneImaq7zN3u_w",
                    "k": "AQIDBAUGBwgJCgsMDQ4PEA",
                }
            ],
            "type": "temporary",
        }

        self.assertEqual(
            app.normalize_decryption_keys(json.dumps(payload)),
            "00112233445566778899aabbccddeeff=0102030405060708090a0b0c0d0e0f10",
        )

    def test_clearkey_top_level_json_is_converted(self):
        payload = {
            "kid": "00112233445566778899aabbccddeeff",
            "key": "0102030405060708090a0b0c0d0e0f10",
        }

        self.assertEqual(
            app.normalize_decryption_keys(json.dumps(payload)),
            "00112233445566778899aabbccddeeff=0102030405060708090a0b0c0d0e0f10",
        )


class M3uParserTests(unittest.TestCase):
    def config(self, **overrides):
        cfg = dict(app.DEFAULTS)
        cfg.update(
            {
                "key": "provider2",
                "event_group_filter": "",
                "date_format": "mm/dd",
                "title_split": "none",
            }
        )
        cfg.update(overrides)
        return cfg

    def test_kodiprop_clearkey_license_is_used_for_plain_mpd_url(self):
        text = "\n".join(
            [
                '#EXTM3U',
                '#EXTINF:0001 tvg-id="abc123" group-title="Boxing", [06/14 02:00] Rodriguez vs. Vargas: Full Event Replay',
                '#KODIPROP:contentlookup=False',
                '#KODIPROP:mimetype=application/dash+xml',
                '#KODIPROP:inputstream=inputstream.adaptive',
                '#KODIPROP:inputstream.adaptive.drm_legacy=org.w3.clearkey|http://license.invalid/api/license/src2/abc123',
                'http://media.invalid/api/src2/live/abc123.mpd',
            ]
        )

        events = app.parse_m3u(text, self.config())

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].url, "http://media.invalid/api/src2/live/abc123.mpd")
        self.assertEqual(
            events[0].license_url,
            "http://license.invalid/api/license/src2/abc123",
        )
        self.assertEqual(events[0].stream_type, "pipe_drm")

    def test_provider1_kodiprop_clearkey_license_is_used(self):
        text = "\n".join(
            [
                '#EXTM3U',
                '#EXTINF:0001 tvg-id="src1123" group-title="Events", [26/06/20 18:05] ZDFsportstudio live-FIFA WM 2026',
                '#KODIPROP:contentlookup=False',
                '#KODIPROP:mimetype=application/dash+xml',
                '#KODIPROP:inputstream=inputstream.adaptive',
                '#KODIPROP:inputstream.adaptive.drm_legacy=org.w3.clearkey|http://license.invalid/api/license/src1/src1123',
                'http://media.invalid/api/src1/live/src1123.mpd',
            ]
        )

        events = app.parse_m3u(
            text,
            self.config(key="provider1", event_group_filter="Events", date_format="auto"),
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].url, "http://media.invalid/api/src1/live/src1123.mpd")
        self.assertEqual(
            events[0].license_url,
            "http://license.invalid/api/license/src1/src1123",
        )
        self.assertEqual(events[0].stream_type, "pipe_drm")

    def test_live_marker_event_is_parsed_as_current_event(self):
        text = "\n".join(
            [
                '#EXTM3U',
                '#EXTINF:0001 tvg-id="live123" group-title="Darts", [Live] Slovak Darts Open | Day 1 (Session 2)',
                "pipe://bash -c 'ffmpeg -cenc_decryption_keys $(curl -s http://license.invalid/live123) "
                "-i \"http://media.invalid/live123.mpd\" -c copy -f mpegts pipe:1'",
            ]
        )

        events = app.parse_m3u(text, self.config())

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].title, "Slovak Darts Open | Day 1 (Session 2)")
        self.assertEqual(events[0].stable_key, "provider2|tvg:live123")
        self.assertEqual(events[0].license_url, "http://license.invalid/live123")
        self.assertEqual(events[0].stream_type, "pipe_drm")

    def test_parenthesized_provider3_live_countdown_sets_future_start(self):
        reference = datetime(2026, 6, 22, 10, 0, tzinfo=ZoneInfo("Europe/Berlin"))
        text = "\n".join(
            [
                '#EXTM3U',
                '#EXTINF:0001 tr-vod="0" tr-catchup="0" tvg-id="event54" tvg-chno="1" group-title="Events", (Live) NFL Network Noch 14 Std. 3 Min.',
                '#KODIPROP:contentlookup=False',
                '#KODIPROP:mimetype=application/dash+xml',
                '#KODIPROP:inputstream=inputstream.adaptive',
                '#KODIPROP:inputstream.adaptive.drm_legacy=org.w3.clearkey|http://license.invalid/api/license/provider3/event54',
                'http://media.invalid/api/provider3/live/event54.mpd',
            ]
        )

        events = app.parse_m3u(
            text,
            self.config(
                key="provider3",
                event_group_filter="Events",
                date_format="auto",
                title_split="first_dash",
                live_countdown_enabled=True,
            ),
            reference_time=reference,
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].title, "NFL Network")
        self.assertEqual(
            datetime.fromisoformat(events[0].start),
            datetime(2026, 6, 23, 0, 3, tzinfo=ZoneInfo("Europe/Berlin")),
        )
        self.assertEqual(events[0].stable_key, "provider3|tvg:event54")
        self.assertEqual(
            events[0].license_url,
            "http://license.invalid/api/license/provider3/event54",
        )
        self.assertEqual(events[0].stream_type, "pipe_drm")

    def test_provider3_compact_countdown_uses_german_datetime_hint(self):
        reference = datetime(2026, 7, 3, 9, 29, tzinfo=ZoneInfo("Europe/Berlin"))
        text = "\n".join(
            [
                '#EXTM3U',
                '#EXTINF:0001 tvg-id="event55" group-title="Events", [7H15M] U19-EM Frauen: Deutschland - Schweden Fußball • Fr., 03.07.26, 16:45 Uhr',
                'http://media.invalid/api/provider3/live/event55.mpd',
            ]
        )

        events = app.parse_m3u(
            text,
            self.config(
                key="provider3",
                event_group_filter="Events",
                title_split="none",
                live_countdown_enabled=True,
            ),
            reference_time=reference,
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0].title,
            "U19-EM Frauen: Deutschland - Schweden Fußball",
        )
        self.assertEqual(
            datetime.fromisoformat(events[0].start),
            datetime(2026, 7, 3, 16, 45, tzinfo=ZoneInfo("Europe/Berlin")),
        )
        self.assertEqual(events[0].stable_key, "provider3|tvg:event55")

    def test_provider3_german_datetime_hint_tolerates_missing_opening_bracket(self):
        parsed = app.parse_event_name(
            "1T7H] Qualifying - GP von Großbritannien Motorsport • Sa., 04.07.26, 16:30 Uhr",
            ZoneInfo("Europe/Berlin"),
            "none",
            reference_time=datetime(2026, 7, 3, 9, 29, tzinfo=ZoneInfo("Europe/Berlin")),
            live_countdown_enabled=True,
        )

        self.assertIsNotNone(parsed)
        start, title, desc = parsed
        self.assertEqual(start, datetime(2026, 7, 4, 16, 30, tzinfo=ZoneInfo("Europe/Berlin")))
        self.assertEqual(title, "Qualifying - GP von Großbritannien Motorsport")
        self.assertEqual(desc, "")

    def test_provider3_german_datetime_hint_works_without_countdown_prefix(self):
        parsed = app.parse_event_name(
            "U19-EM Männer: Spanien - Deutschland Fußball • Sa., 04.07.26, 14:45 Uhr",
            ZoneInfo("Europe/Berlin"),
            "none",
            live_countdown_enabled=True,
        )

        self.assertIsNotNone(parsed)
        start, title, desc = parsed
        self.assertEqual(start, datetime(2026, 7, 4, 14, 45, tzinfo=ZoneInfo("Europe/Berlin")))
        self.assertEqual(title, "U19-EM Männer: Spanien - Deutschland Fußball")
        self.assertEqual(desc, "")

    def test_provider3_compact_countdown_falls_back_to_relative_start(self):
        reference = datetime(2026, 7, 3, 9, 29, 35, tzinfo=ZoneInfo("Europe/Berlin"))
        parsed = app.parse_event_name(
            "[1T5H] Event ohne Datum",
            ZoneInfo("Europe/Berlin"),
            "none",
            reference_time=reference,
            live_countdown_enabled=True,
        )

        self.assertIsNotNone(parsed)
        start, title, desc = parsed
        self.assertEqual(start, datetime(2026, 7, 4, 14, 29, tzinfo=ZoneInfo("Europe/Berlin")))
        self.assertEqual(title, "Event ohne Datum")
        self.assertEqual(desc, "")

    def test_provider3_live_countdown_keeps_first_calculated_start(self):
        config = self.config(
            key="provider3",
            event_group_filter="Events",
            date_format="auto",
            title_split="first_dash",
            live_countdown_enabled=True,
        )
        text = "\n".join(
            [
                '#EXTM3U',
                '#EXTINF:0001 tvg-id="event54" group-title="Events", (Live) NFL Network Noch 14 Std. 3 Min.',
                'http://media.invalid/api/provider3/live/event54.mpd',
            ]
        )
        first = app.parse_m3u(
            text,
            config,
            reference_time=datetime(
                2026, 6, 22, 10, 0, tzinfo=ZoneInfo("Europe/Berlin")
            ),
        )[0]
        refreshed = app.parse_m3u(
            text,
            config,
            reference_time=datetime(
                2026, 6, 22, 12, 0, tzinfo=ZoneInfo("Europe/Berlin")
            ),
        )

        app.preserve_live_countdown_starts(
            refreshed,
            config,
            {"detected_events": [asdict(first)]},
        )

        self.assertEqual(refreshed[0].start, first.start)

    def test_provider3_live_countdown_restarts_when_title_changes(self):
        config = self.config(
            key="provider3",
            event_group_filter="Events",
            date_format="auto",
            title_split="first_dash",
            live_countdown_enabled=True,
        )
        previous = app.SourceEvent(
            stable_key="provider3|tvg:event54",
            start="2026-06-23T00:03:00+02:00",
            stop="",
            title="NFL Network",
            desc="",
            url="http://media.invalid/old.mpd",
            raw_name="(Live) NFL Network Noch 14 Std. 3 Min.",
            source_tvg_id="event54",
            source_key="provider3",
        )
        current = app.SourceEvent(
            stable_key="provider3|tvg:event54",
            start="2026-06-24T02:03:00+02:00",
            stop="",
            title="NFL RedZone",
            desc="",
            url="http://media.invalid/new.mpd",
            raw_name="(Live) NFL RedZone Noch 14 Std. 3 Min.",
            source_tvg_id="event54",
            source_key="provider3",
        )

        app.preserve_live_countdown_starts(
            [current],
            config,
            {"detected_events": [asdict(previous)]},
        )

        self.assertEqual(current.start, "2026-06-24T02:03:00+02:00")

    def test_provider3_countdown_keeps_dated_start_after_event_has_started(self):
        config = self.config(
            key="provider3",
            event_group_filter="Events",
            date_format="auto",
            title_split="first_dash",
            live_countdown_enabled=True,
        )
        previous = app.SourceEvent(
            stable_key="provider3|tvg:event54",
            start="2026-06-22T18:00:00+02:00",
            stop="",
            title="NFL Network",
            desc="",
            url="http://media.invalid/old.mpd",
            raw_name="[26/06/22 18:00] NFL Network",
            source_tvg_id="event54",
            source_key="provider3",
        )
        current = app.SourceEvent(
            stable_key="provider3|tvg:event54",
            start="2026-06-23T14:00:00+02:00",
            stop="",
            title="NFL Network",
            desc="",
            url="http://media.invalid/current.mpd",
            raw_name="(Live) NFL Network Noch 14 Std.",
            source_tvg_id="event54",
            source_key="provider3",
        )

        app.preserve_live_countdown_starts(
            [current],
            config,
            {"detected_events": [asdict(previous)]},
        )

        self.assertEqual(current.start, previous.start)

    def test_tvg_id_keeps_stable_key_when_event_changes_to_live_marker(self):
        dated = app.parse_m3u(
            "\n".join(
                [
                    '#EXTM3U',
                    '#EXTINF:0001 tvg-id="same123" group-title="Darts", [06/19 13:00] Slovak Darts Open | Day 1 (Session 1)',
                    "http://media.invalid/dated.mpd",
                ]
            ),
            self.config(),
        )[0]
        live = app.parse_m3u(
            "\n".join(
                [
                    '#EXTM3U',
                    '#EXTINF:0001 tvg-id="same123" group-title="Darts", [Live] Slovak Darts Open | Day 1 (Session 1)',
                    "http://media.invalid/live.mpd",
                ]
            ),
            self.config(),
        )[0]

        self.assertEqual(dated.stable_key, live.stable_key)
        self.assertEqual(live.stable_key, "provider2|tvg:same123")


class LiveChannelParserTests(unittest.TestCase):
    def config(self, **overrides):
        cfg = dict(app.DEFAULTS)
        cfg.update(
            {
                "key": "provider3",
                "source_name": "Provider 3",
                "event_group_filter": "",
                "date_format": "auto",
            }
        )
        cfg.update(overrides)
        return cfg

    def test_normal_channel_keeps_metadata_and_drm_details(self):
        text = "\n".join(
            [
                "#EXTM3U",
                '#EXTINF:-1 tvg-id="zdf" tvg-logo="https://img.invalid/zdf.png" '
                'tvg-chno="2" group-title="Deutschland",ZDF HD',
                "#KODIPROP:contentlookup=False",
                "#KODIPROP:inputstream.adaptive.drm_legacy="
                "org.w3.clearkey|http://license.invalid/zdf",
                "http://media.invalid/zdf.mpd",
            ]
        )

        channels = app.parse_live_channels(text, self.config())

        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0].name, "ZDF HD")
        self.assertEqual(channels[0].group, "Deutschland")
        self.assertEqual(channels[0].source_tvg_id, "zdf")
        self.assertEqual(channels[0].channel_number, "2")
        self.assertEqual(channels[0].license_url, "http://license.invalid/zdf")
        self.assertEqual(channels[0].stream_type, "pipe_drm")
        self.assertNotIn("media", channels[0].id)

    def test_event_and_explicit_vod_entries_are_not_live_channels(self):
        text = "\n".join(
            [
                "#EXTM3U",
                '#EXTINF:-1 tvg-id="event1" group-title="Events",'
                "[26/06/22 20:00] Team A - Team B",
                "http://media.invalid/event.mpd",
                '#EXTINF:-1 tr-vod="1" tvg-id="movie1" group-title="Filme",Film',
                "http://media.invalid/movie.mpd",
                '#EXTINF:-1 tr-vod="0" tvg-id="provider3" group-title="Deutschland",Provider 3 HD',
                "http://media.invalid/provider3.mpd",
            ]
        )

        channels = app.parse_live_channels(text, self.config())

        self.assertEqual([channel.name for channel in channels], ["Provider 3 HD"])

    def test_channel_id_stays_stable_when_stream_url_changes(self):
        first = app.parse_live_channels(
            '#EXTINF:-1 tvg-id="zdf" group-title="Deutschland",ZDF HD\n'
            "http://media.invalid/first.mpd",
            self.config(),
        )[0]
        second = app.parse_live_channels(
            '#EXTINF:-1 tvg-id="zdf" group-title="Deutschland",ZDF HD\n'
            "http://media.invalid/second.mpd",
            self.config(),
        )[0]

        self.assertEqual(first.id, second.id)


class RefreshLiveTvTests(unittest.TestCase):
    def test_refresh_downloads_each_source_once_for_events_and_live_tv(self):
        cfg = dict(app.DEFAULTS)
        cfg.update(
            {
                "sources": [
                    {
                        "key": "provider",
                        "name": "Provider",
                        "source_m3u": "http://example.invalid/provider.m3u",
                        "slots": 1,
                        "event_group_filter": "",
                        "date_format": "yy/mm/dd",
                    }
                ]
            }
        )
        source_text = "\n".join(
            [
                "#EXTM3U",
                '#EXTINF:-1 tvg-id="event1" group-title="Events",'
                "[26/06/22 20:00] Team A - Team B",
                "http://media.invalid/event.mpd",
                '#EXTINF:-1 tvg-id="zdf" group-title="Deutschland",ZDF HD',
                "http://media.invalid/zdf.mpd",
            ]
        )
        download_calls = []
        stored = []
        old_values = {
            "load_config": app.load_config,
            "current_raw_state": app.current_raw_state,
            "download_source_m3u": app.download_source_m3u,
            "store_state": app.store_state,
            "write_xmltv_file": app.write_xmltv_file,
        }
        try:
            app.load_config = lambda: cfg
            app.current_raw_state = lambda: {}
            app.download_source_m3u = lambda source_cfg: (
                download_calls.append(source_cfg["key"]) or source_text
            )
            app.store_state = lambda state: stored.append(state)
            app.write_xmltv_file = lambda state, config: None

            state = app.refresh_state()
        finally:
            for name, value in old_values.items():
                setattr(app, name, value)

        self.assertEqual(download_calls, ["provider"])
        self.assertEqual(state["detected_event_count"], 1)
        self.assertEqual(state["live_channel_count"], 1)
        self.assertEqual(len(stored), 1)

    def test_successful_empty_refresh_removes_disappeared_event_everywhere(self):
        cfg = dict(app.DEFAULTS)
        cfg["sources"] = [
            {
                "key": "provider1",
                "name": "Provider 1",
                "source_m3u": "http://example.invalid/provider1.m3u",
                "slots": 1,
                "event_group_filter": "Events",
            }
        ]
        old_event = {
            "stable_key": "provider1|tvg:event1",
            "start": "2026-06-22T18:00:00+02:00",
            "stop": "2026-06-22T21:00:00+02:00",
            "title": "Old Event",
            "desc": "",
            "url": "http://media.invalid/old.mpd",
            "group": "Events",
            "source_tvg_id": "event1",
            "source_key": "provider1",
        }
        previous = {
            "schema_version": 2,
            "sources": [
                {
                    "key": "provider1",
                    "name": "Provider 1",
                    "source_m3u": "http://example.invalid/provider1.m3u",
                    "updated_at": "2026-06-22T21:00:00+02:00",
                    "detected_event_count": 1,
                    "detected_events": [old_event],
                    "live_channel_count": 0,
                    "live_channels": [],
                    "scheduled_event_count": 1,
                    "dropped_events": [],
                    "slot_memory": {"provider1|tvg:event1": 1},
                    "slots": [
                        {
                            "id": 1,
                            "source_key": "provider1",
                            "events": [old_event],
                        }
                    ],
                    "error": None,
                    "stale": False,
                }
            ],
        }
        old_values = {
            "load_config": app.load_config,
            "current_raw_state": app.current_raw_state,
            "download_source_m3u": app.download_source_m3u,
            "store_state": app.store_state,
            "write_xmltv_file": app.write_xmltv_file,
        }
        try:
            app.load_config = lambda: cfg
            app.current_raw_state = lambda: previous
            app.download_source_m3u = lambda source_cfg: "#EXTM3U\n"
            app.store_state = lambda state: None
            app.write_xmltv_file = lambda state, config: None

            state = app.refresh_state()
        finally:
            for name, value in old_values.items():
                setattr(app, name, value)

        self.assertEqual(state["detected_events"], [])
        self.assertEqual(state["slots"][0]["events"], [])
        self.assertEqual(state["sources"][0]["slot_memory"], {})
        self.assertNotIn("<programme ", app.render_xmltv(state, cfg))
        status = app.public_status(state, cfg)
        self.assertIsNone(status["slots"][0]["current_event"])
        self.assertIsNone(status["slots"][0]["playback_mode"])
        event, mode, switch_at = app.playback_for_slot(
            1,
            state,
            cfg,
            "provider1",
        )
        self.assertIsNone(event)
        self.assertIsNone(mode)
        self.assertIsNone(switch_at)


class ScheduleTests(unittest.TestCase):
    def config(self, **overrides):
        cfg = dict(app.DEFAULTS)
        cfg.update(
            {
                "key": "provider2",
                "source_name": "Provider 2",
                "source_m3u": "http://example.invalid/source.m3u",
                "slots": 2,
                "channel_number_start": 911,
                "channel_name_template": "Provider 2 Event {slot}",
                "channel_id_template": "provider2.event{slot}",
                "group_title": "Provider 2 Events",
            }
        )
        cfg.update(overrides)
        return cfg

    def event(self, key, tvg_id, title, start):
        return app.SourceEvent(
            stable_key=key,
            start=start.isoformat(),
            stop="",
            title=title,
            desc="",
            url=f"http://media.invalid/{tvg_id}.mpd",
            group="Sport",
            logo="",
            raw_name=f"[Live] {title}",
            source_tvg_id=tvg_id,
            priority_score=0,
            source_key="provider2",
            source_name="Provider 2",
            stream_type="pipe_drm",
            license_url=f"http://license.invalid/{tvg_id}",
            ffmpeg_input_args=[],
        )

    def test_persisted_slot_memory_survives_reordered_live_events(self):
        zone = ZoneInfo("Europe/Berlin")
        start = datetime.now(zone) + timedelta(minutes=5)
        events = [
            self.event("provider2|tvg:motor", "motor", "Alpha Motorland", start),
            self.event("provider2|tvg:prague", "prague", "Zulu Prague", start),
        ]
        previous = {"slot_memory": {"provider2|tvg:motor": 2}}

        slots, _, _ = app.build_schedule(events, self.config(), previous)

        self.assertEqual(slots[1]["events"][0]["title"], "Alpha Motorland")
        self.assertEqual(slots[0]["events"][0]["title"], "Zulu Prague")

    def test_slot_memory_is_stored_with_scheduled_events(self):
        zone = ZoneInfo("Europe/Berlin")
        start = datetime.now(zone) + timedelta(minutes=5)
        events = [self.event("provider2|tvg:motor", "motor", "Alpha Motorland", start)]

        slots, _, _ = app.build_schedule(events, self.config())
        memory = app.slot_memory_from_slots(slots)

        self.assertEqual(memory["provider2|tvg:motor"], 1)

    def test_slot_memory_drops_events_missing_from_successful_plan(self):
        memory = app.slot_memory_from_slots(
            app.slot_shells(self.config()),
            {"slot_memory": {"provider2|tvg:gone": 2}},
        )

        self.assertEqual(memory, {})

    def test_running_event_is_extended_while_still_listed(self):
        zone = ZoneInfo("Europe/Berlin")
        now = datetime.now(zone)
        event = self.event(
            "provider2|tvg:long",
            "long",
            "Long Running Event",
            now - timedelta(hours=4),
        )
        previous_event = asdict(event)
        previous_event["stop"] = (now - timedelta(minutes=1)).isoformat()
        previous = {
            "slots": [
                {
                    "id": 1,
                    "source_key": "provider2",
                    "events": [previous_event],
                }
            ]
        }

        slots, candidates, dropped = app.build_schedule(
            [event],
            self.config(
                default_duration_minutes=180,
                refresh_seconds=120,
                keep_stream_while_listed=True,
            ),
            previous,
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(len(dropped), 0)
        self.assertGreater(
            datetime.fromisoformat(slots[0]["events"][0]["stop"]),
            now + timedelta(minutes=4),
        )

    def test_fresh_current_event_takes_slot_from_old_extended_event(self):
        zone = ZoneInfo("Europe/Berlin")
        now = datetime.now(zone)
        old_event = self.event(
            "provider2|tvg:old",
            "old",
            "Old Listed Event",
            now - timedelta(days=3),
        )
        fresh_event = self.event(
            "provider2|tvg:fresh",
            "fresh",
            "Fresh Current Event",
            now - timedelta(minutes=2),
        )
        previous_event = asdict(old_event)
        previous_event["stop"] = (now - timedelta(minutes=1)).isoformat()
        previous = {
            "slot_memory": {"provider2|tvg:old": 1},
            "slots": [
                {
                    "id": 1,
                    "source_key": "provider2",
                    "events": [previous_event],
                }
            ],
        }

        slots, candidates, dropped = app.build_schedule(
            [old_event, fresh_event],
            self.config(
                slots=1,
                default_duration_minutes=180,
                refresh_seconds=120,
                keep_stream_while_listed=True,
            ),
            previous,
        )

        self.assertEqual(len(candidates), 2)
        self.assertEqual(slots[0]["events"][0]["title"], "Fresh Current Event")
        self.assertEqual([event.title for event in dropped], ["Old Listed Event"])

    def test_running_event_is_not_kept_after_it_disappears_from_source(self):
        zone = ZoneInfo("Europe/Berlin")
        now = datetime.now(zone)
        previous = {
            "slots": [
                {
                    "id": 1,
                    "source_key": "provider2",
                    "events": [
                        {
                            **asdict(
                                self.event(
                                    "provider2|tvg:gone",
                                    "gone",
                                    "Finished Event",
                                    now - timedelta(hours=4),
                                )
                            ),
                            "stop": (now - timedelta(minutes=1)).isoformat(),
                        }
                    ],
                }
            ]
        }

        slots, candidates, dropped = app.build_schedule(
            [],
            self.config(keep_stream_while_listed=True),
            previous,
        )

        self.assertEqual(candidates, [])
        self.assertEqual(dropped, [])
        self.assertEqual(slots[0]["events"], [])


class PlaybackSelectionTests(unittest.TestCase):
    def config(self, allow_upcoming_stream):
        cfg = dict(app.DEFAULTS)
        cfg.update(
            {
                "timezone": "Europe/Berlin",
                "idle_replay_enabled": False,
                "sources": [
                    {
                        "key": "provider3",
                        "name": "Provider 3",
                        "source_m3u": "http://example.invalid/provider3.m3u",
                        "slots": 1,
                        "channel_number_start": 921,
                        "channel_name_template": "Provider 3 Event {slot}",
                        "channel_id_template": "provider3.event{slot}",
                        "allow_upcoming_stream": allow_upcoming_stream,
                    }
                ],
            }
        )
        return cfg

    def state(self, start):
        event = {
            "stable_key": "provider3|tvg:event54",
            "start": start.isoformat(),
            "stop": (start + timedelta(hours=3)).isoformat(),
            "title": "NFL Network",
            "url": "http://media.invalid/provider3/event54.mpd",
            "source_key": "provider3",
        }
        return {
            "slots": [
                {
                    "id": 1,
                    "source_key": "provider3",
                    "events": [event],
                }
            ],
            "detected_events": [event],
        }

    def test_upcoming_stream_can_be_played_before_epg_start(self):
        now = datetime(2026, 6, 22, 10, 0, tzinfo=ZoneInfo("Europe/Berlin"))
        event, mode, switch_at = app.playback_for_slot(
            1,
            self.state(now + timedelta(hours=14)),
            self.config(allow_upcoming_stream=True),
            "provider3",
            now=now,
        )

        self.assertIsNotNone(event)
        self.assertEqual(event["title"], "NFL Network")
        self.assertEqual(mode, "preview")
        self.assertIsNone(switch_at)

    def test_upcoming_stream_remains_unavailable_when_option_is_disabled(self):
        now = datetime(2026, 6, 22, 10, 0, tzinfo=ZoneInfo("Europe/Berlin"))
        event, mode, switch_at = app.playback_for_slot(
            1,
            self.state(now + timedelta(hours=14)),
            self.config(allow_upcoming_stream=False),
            "provider3",
            now=now,
        )

        self.assertIsNone(event)
        self.assertIsNone(mode)
        self.assertIsNone(switch_at)


class ReplaySelectionTests(unittest.TestCase):
    def test_nominally_finished_event_is_not_replayed_while_stream_is_extended(self):
        now = datetime(2026, 6, 23, 12, 0, tzinfo=ZoneInfo("Europe/Berlin"))
        cfg = dict(app.DEFAULTS)
        cfg["sources"] = [
            {
                "key": "provider3",
                "name": "Provider 3",
                "source_m3u": "http://example.invalid/provider3.m3u",
                "slots": 2,
                "idle_replay_enabled": True,
            }
        ]
        detected = {
            "stable_key": "provider3|tvg:event54",
            "source_tvg_id": "event54",
            "source_key": "provider3",
            "start": (now - timedelta(hours=4)).isoformat(),
            "stop": (now - timedelta(hours=1)).isoformat(),
            "title": "Long Running Event",
            "url": "http://media.invalid/event54.mpd",
        }
        scheduled = {
            **detected,
            "stop": (now + timedelta(minutes=5)).isoformat(),
        }
        state = {
            "detected_events": [detected],
            "slots": [
                {"id": 1, "source_key": "provider3", "events": [scheduled]},
                {"id": 2, "source_key": "provider3", "events": []},
            ],
        }

        replay = app.replay_event_for_slot(2, state, cfg, "provider3", now=now)

        self.assertIsNone(replay)

    def test_disappeared_event_is_not_available_for_replay(self):
        now = datetime(2026, 6, 23, 12, 0, tzinfo=ZoneInfo("Europe/Berlin"))
        cfg = dict(app.DEFAULTS)
        cfg["sources"] = [
            {
                "key": "provider1",
                "name": "Provider 1",
                "source_m3u": "http://example.invalid/provider1.m3u",
                "slots": 1,
                "idle_replay_enabled": True,
            }
        ]

        replay = app.replay_event_for_slot(
            1,
            {"detected_events": [], "slots": []},
            cfg,
            "provider1",
            now=now,
        )

        self.assertIsNone(replay)


class OutputTests(unittest.TestCase):
    def state_and_config(self, event_overrides=None):
        cfg = dict(app.DEFAULTS)
        cfg.update(
            {
                "host_for_playlist": "127.0.0.1",
                "key": "provider2",
                "source_name": "Provider 2",
                "source_m3u": "http://example.invalid/source.m3u",
                "slots": 1,
                "channel_number_start": 911,
                "channel_name_template": "Provider 2 Event {slot}",
                "channel_id_template": "provider2.event{slot}",
                "group_title": "Provider 2 Events",
                "_legacy_route": False,
            }
        )
        cfg["sources"] = [
            {
                "key": "provider2",
                "name": "Provider 2",
                "source_m3u": "http://example.invalid/source.m3u",
                "slots": 1,
                "channel_number_start": 911,
                "channel_name_template": "Provider 2 Event {slot}",
                "channel_id_template": "provider2.event{slot}",
                "group_title": "Provider 2 Events",
            }
        ]
        zone = ZoneInfo(cfg["timezone"])
        start = datetime.now(zone) - timedelta(minutes=5)
        stop = start + timedelta(hours=2)
        event = {
            "stable_key": "provider2|tvg:live123",
            "start": start.isoformat(),
            "stop": stop.isoformat(),
            "title": "Slovak Darts Open",
            "desc": "Day 1",
            "url": "http://media.invalid/live.mpd",
            "group": "Darts",
            "logo": "",
            "raw_name": "[Live] Slovak Darts Open",
            "source_tvg_id": "live123",
            "priority_score": 0,
            "source_key": "provider2",
            "source_name": "Provider 2",
            "stream_type": "pipe_drm",
            "license_url": "http://license.invalid/live123",
            "ffmpeg_input_args": [],
        }
        if event_overrides:
            event.update(event_overrides)
        slot = {
            "id": 1,
            "source_key": "provider2",
            "source_name": "Provider 2",
            "channel_id": "provider2.event1",
            "name": "Provider 2 Event 1",
            "number": 911,
            "group_title": "Provider 2 Events",
            "stream_path": "/slot/provider2/1.ts",
            "events": [event],
        }
        state = {
            "updated_at": start.isoformat(),
            "last_attempt_at": start.isoformat(),
            "source_count": 1,
            "detected_event_count": 1,
            "scheduled_event_count": 1,
            "dropped_event_count": 0,
            "detected_events": [event],
            "dropped_events": [],
            "slots": [slot],
            "sources": [],
            "errors": [],
            "error": None,
            "stale": False,
        }
        return state, cfg

    def with_state(self, callback):
        state, cfg = self.state_and_config()
        old_load_config = app.load_config
        old_get_state = app.get_state
        try:
            app.load_config = lambda: cfg
            app.get_state = lambda: state
            return callback()
        finally:
            app.load_config = old_load_config
            app.get_state = old_get_state

    def test_xmltv_includes_event_group_as_category(self):
        response = self.with_state(app.xmltv)
        text = response.body.decode("utf-8")

        self.assertIn("<category lang=\"de\">Event</category>", text)
        self.assertIn("<category lang=\"de\">Darts</category>", text)
        self.assertIn("<sport>Darts</sport>", text)

    def test_xmltv_channel_includes_event_slot_logo(self):
        state, cfg = self.state_and_config()
        state["slots"][0]["logo_path"] = "/slot/provider2/1/logo"

        text = app.render_xmltv(state, cfg)

        self.assertIn(
            '<icon src="http://127.0.0.1:8787/slot/provider2/1/logo" />',
            text,
        )

    def test_xmltv_caps_programme_at_four_hours_without_changing_slot_stop(self):
        state, cfg = self.state_and_config()
        start = datetime.fromisoformat(state["slots"][0]["events"][0]["start"])
        technical_stop = start + timedelta(hours=8)
        state["slots"][0]["events"][0]["stop"] = technical_stop.isoformat()

        text = app.render_xmltv(state, cfg)
        expected_epg_stop = start + timedelta(hours=4)

        self.assertIn(
            f'stop="{app.xmltv_datetime(expected_epg_stop.isoformat())}"',
            text,
        )
        self.assertEqual(
            state["slots"][0]["events"][0]["stop"],
            technical_stop.isoformat(),
        )

    def test_xmltv_marks_current_programme_as_live(self):
        state, cfg = self.state_and_config()

        text = app.render_xmltv(state, cfg)

        self.assertIn("<title lang=\"de\">Live: Slovak Darts Open</title>", text)

    def test_xmltv_marks_old_extended_programme_as_available(self):
        state, cfg = self.state_and_config()
        zone = ZoneInfo(cfg["timezone"])
        start = datetime.now(zone) - timedelta(hours=5)
        technical_stop = datetime.now(zone) + timedelta(minutes=5)
        event = state["slots"][0]["events"][0]
        event["start"] = start.isoformat()
        event["stop"] = technical_stop.isoformat()

        text = app.render_xmltv(state, cfg)

        self.assertIn(
            "<title lang=\"de\">Weiter verfügbar: Slovak Darts Open</title>",
            text,
        )
        self.assertIn(
            f'stop="{app.xmltv_datetime(technical_stop.isoformat())}"',
            text,
        )

    def test_mini_epg_uses_display_cap_instead_of_extended_stream_stop(self):
        state, cfg = self.state_and_config()
        zone = ZoneInfo(cfg["timezone"])
        start = datetime.now(zone) - timedelta(hours=5)
        technical_stop = datetime.now(zone) + timedelta(minutes=5)
        event = state["detected_events"][0]
        event["start"] = start.isoformat()
        event["stop"] = technical_stop.isoformat()
        state["slots"][0]["events"][0] = dict(event)

        entries = app.mini_epg_entries(state, cfg)

        self.assertEqual(entries, [])

    def test_xmltv_file_matches_http_output(self):
        state, cfg = self.state_and_config()
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "guide" / "xmltv.xml"
            cfg["xmltv_output_path"] = str(output_path)

            app.write_xmltv_file(state, cfg)

            self.assertEqual(
                output_path.read_bytes(),
                app.render_xmltv(state, cfg).encode("utf-8"),
            )
            self.assertFalse(output_path.with_name("xmltv.xml.tmp").exists())

    def test_unchanged_xmltv_file_is_not_rewritten(self):
        state, cfg = self.state_and_config()
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "xmltv.xml"
            cfg["xmltv_output_path"] = str(output_path)
            app.write_xmltv_file(state, cfg)
            original_mtime = output_path.stat().st_mtime_ns

            app.write_xmltv_file(state, cfg)

            self.assertEqual(output_path.stat().st_mtime_ns, original_mtime)
            self.assertFalse(output_path.with_name("xmltv.xml.tmp").exists())

    def test_changed_xmltv_file_is_rewritten(self):
        state, cfg = self.state_and_config()
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "xmltv.xml"
            cfg["xmltv_output_path"] = str(output_path)
            app.write_xmltv_file(state, cfg)
            original = output_path.read_bytes()
            state["slots"][0]["events"][0]["title"] = "Changed Event"

            app.write_xmltv_file(state, cfg)

            self.assertNotEqual(output_path.read_bytes(), original)
            self.assertIn(b"Changed Event", output_path.read_bytes())

    def test_empty_xmltv_output_path_disables_file_output(self):
        state, cfg = self.state_and_config()
        cfg["xmltv_output_path"] = ""

        app.write_xmltv_file(state, cfg)

    def test_xmltv_can_be_sent_to_unix_socket_without_file_output(self):
        state, cfg = self.state_and_config()
        cfg["xmltv_output_path"] = ""
        cfg["xmltv_socket_enabled"] = True
        cfg["xmltv_socket_path"] = "/tmp/xmltv.sock"
        sent = {}

        class FakeSocket:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def settimeout(self, value):
                sent["timeout"] = value

            def connect(self, path):
                sent["path"] = path

            def sendall(self, data):
                sent["data"] = data

            def shutdown(self, how):
                sent["shutdown"] = how

        original_socket = app.socket.socket
        had_af_unix = hasattr(app.socket, "AF_UNIX")
        original_af_unix = getattr(app.socket, "AF_UNIX", None)
        try:
            app.socket.AF_UNIX = original_af_unix if had_af_unix else 1
            app.socket.socket = lambda *args, **kwargs: FakeSocket()

            app.write_xmltv_file(state, cfg)
        finally:
            app.socket.socket = original_socket
            if had_af_unix:
                app.socket.AF_UNIX = original_af_unix
            else:
                delattr(app.socket, "AF_UNIX")

        self.assertEqual(sent["path"], "/tmp/xmltv.sock")
        self.assertIn(b"<tv generator-info-name=\"TR-EventMux\">", sent["data"])

    def test_xmltv_socket_can_be_disabled_with_path_configured(self):
        state, cfg = self.state_and_config()
        cfg["xmltv_output_path"] = ""
        cfg["xmltv_socket_enabled"] = False
        cfg["xmltv_socket_path"] = "/tmp/xmltv.sock"
        called = []

        original_socket = app.socket.socket
        original_af_unix = getattr(app.socket, "AF_UNIX", None)
        had_af_unix = hasattr(app.socket, "AF_UNIX")
        try:
            app.socket.AF_UNIX = object()
            app.socket.socket = lambda *args, **kwargs: called.append(args)

            app.write_xmltv_file(state, cfg)
        finally:
            app.socket.socket = original_socket
            if had_af_unix:
                app.socket.AF_UNIX = original_af_unix
            else:
                delattr(app.socket, "AF_UNIX")

        self.assertEqual(called, [])

    def test_unchanged_xmltv_file_is_still_sent_to_socket(self):
        state, cfg = self.state_and_config()
        sent = []
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "xmltv.xml"
            cfg["xmltv_output_path"] = str(output_path)
            cfg["xmltv_socket_enabled"] = True
            cfg["xmltv_socket_path"] = "/tmp/xmltv.sock"

            class FakeSocket:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def settimeout(self, value):
                    pass

                def connect(self, path):
                    pass

                def sendall(self, data):
                    sent.append(data)

                def shutdown(self, how):
                    pass

            original_socket = app.socket.socket
            had_af_unix = hasattr(app.socket, "AF_UNIX")
            original_af_unix = getattr(app.socket, "AF_UNIX", None)
            try:
                app.socket.AF_UNIX = original_af_unix if had_af_unix else 1
                app.socket.socket = lambda *args, **kwargs: FakeSocket()
                app.write_xmltv_file(state, cfg)
                original_mtime = output_path.stat().st_mtime_ns

                app.write_xmltv_file(state, cfg)
            finally:
                app.socket.socket = original_socket
                if had_af_unix:
                    app.socket.AF_UNIX = original_af_unix
                else:
                    delattr(app.socket, "AF_UNIX")

            self.assertEqual(output_path.stat().st_mtime_ns, original_mtime)
            self.assertEqual(len(sent), 2)

    def test_xmltv_includes_sport_and_team_tags_for_team_events(self):
        state, cfg = self.state_and_config(
            {"title": "Germany - Curacao", "group": "Football"}
        )
        old_load_config = app.load_config
        old_get_state = app.get_state
        try:
            app.load_config = lambda: cfg
            app.get_state = lambda: state
            response = app.xmltv()
        finally:
            app.load_config = old_load_config
            app.get_state = old_get_state
        text = response.body.decode("utf-8")

        self.assertIn("<category lang=\"de\">Football</category>", text)
        self.assertIn("<sport>Soccer</sport>", text)
        self.assertIn("<team>Germany</team>", text)
        self.assertIn("<team>Curacao</team>", text)

    def test_mini_epg_includes_group_column(self):
        response = self.with_state(app.index)
        text = response.body.decode("utf-8")

        self.assertIn("<th>Gruppe</th>", text)
        self.assertIn("<td>Darts</td>", text)

    def test_status_page_links_current_streams(self):
        response = self.with_state(app.index)
        text = response.body.decode("utf-8")

        self.assertIn('href="/slot/provider2/1.m3u"', text)
        self.assertIn('data-stream-url="/slot/provider2/1.ts"', text)
        self.assertIn('href="/slot/provider2/1/ffmpeg"', text)
        self.assertIn('>Slovak Darts Open</a>', text)
        self.assertIn('>Provider 2 Event 1 (911)</a>', text)
        self.assertIn('id="web-player-modal"', text)
        self.assertIn('src="/assets/mpegts.js"', text)

    def test_status_page_marks_current_slot_as_live(self):
        response = self.with_state(app.index)
        text = response.body.decode("utf-8")

        self.assertIn('class="slot-badge live"', text)
        self.assertIn(">Live</span>", text)

    def test_status_page_marks_old_extended_slot_as_available(self):
        state, cfg = self.state_and_config()
        zone = ZoneInfo(cfg["timezone"])
        start = datetime.now(zone) - timedelta(hours=5)
        technical_stop = datetime.now(zone) + timedelta(minutes=5)
        event = state["detected_events"][0]
        event["start"] = start.isoformat()
        event["stop"] = technical_stop.isoformat()
        state["slots"][0]["events"][0] = dict(event)
        old_load_config = app.load_config
        old_get_state = app.get_state
        try:
            app.load_config = lambda: cfg
            app.get_state = lambda: state
            response = app.index()
        finally:
            app.load_config = old_load_config
            app.get_state = old_get_state
        text = response.body.decode("utf-8")

        self.assertIn('class="slot-badge available"', text)
        self.assertIn(">Weiter verfügbar</span>", text)

    def test_status_page_supports_persisted_dark_mode(self):
        response = self.with_state(app.index)
        text = response.body.decode("utf-8")

        self.assertIn('id="theme-toggle"', text)
        self.assertIn('localStorage.getItem("tr-eventmux-theme")', text)
        self.assertIn(':root[data-theme="dark"]', text)

    def test_status_page_defaults_to_persisted_webplayer_mode(self):
        response = self.with_state(app.index)
        text = response.body.decode("utf-8")

        self.assertIn('id="player-mode-toggle"', text)
        self.assertIn('localStorage.getItem("tr-eventmux-player-mode")', text)
        self.assertIn('localStorage.setItem("tr-eventmux-player-mode", next)', text)
        self.assertIn('document.documentElement.dataset.playerMode === "vlc"', text)
        self.assertIn(
            "new URL(trigger.dataset.streamUrl, window.location.href).href",
            text,
        )
        self.assertIn("stashInitialSize: 1024 * 1024", text)
        self.assertIn("liveBufferLatencyChasing: false", text)
        self.assertIn("autoCleanupSourceBuffer: true", text)

    def test_vendored_mpegts_javascript_is_served_locally(self):
        response = app.mpegts_javascript()

        self.assertEqual(response.media_type, "application/javascript")
        self.assertIn(b"mpegts", response.body[:4096].lower())
        self.assertIn("immutable", response.headers["cache-control"])

    def test_slot_playlist_points_to_transport_stream(self):
        response = self.with_state(lambda: app.slot_source_playlist("provider2", 1))
        text = response.body.decode("utf-8")

        self.assertIn("#EXTM3U", text)
        self.assertIn("http://127.0.0.1:8787/slot/provider2/1.ts", text)
        self.assertEqual(response.media_type, "audio/x-mpegurl")

    def test_slot_playlist_includes_event_logo(self):
        state, cfg = self.state_and_config()
        state["slots"][0]["logo_path"] = "/slot/provider2/1/logo"
        old_load_config = app.load_config
        old_get_state = app.get_state
        try:
            app.load_config = lambda: cfg
            app.get_state = lambda: state
            response = app.slot_source_playlist("provider2", 1)
        finally:
            app.load_config = old_load_config
            app.get_state = old_get_state

        text = response.body.decode("utf-8")
        self.assertIn(
            'tvg-logo="http://127.0.0.1:8787/slot/provider2/1/logo"',
            text,
        )

    def test_event_slot_logo_endpoint_returns_transparent_png(self):
        response = self.with_state(lambda: app.source_slot_logo("provider2", 1))

        self.assertEqual(response.media_type, "image/png")
        self.assertEqual(response.body[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(response.body[25], 6)

    def test_all_configured_event_slot_logos_exist(self):
        expected = {
            "event-default.png",
            "event-provider1.png",
            "event-provider2.png",
            "event-provider3.png",
        }
        actual = {path.name for path in app.EVENT_LOGO_DIR.glob("*.png")}

        self.assertEqual(actual, expected)

    def test_slot_logos_do_not_depend_on_channel_numbers(self):
        cfg = self.state_and_config()[1]
        cfg["channel_number_start"] = 940
        cfg["logo_key"] = "provider2"
        cfg["slots"] = 15

        slots = app.slot_shells(cfg)

        self.assertEqual(slots[0]["number"], 940)
        self.assertEqual(slots[-1]["number"], 954)
        self.assertTrue(all(slot.get("logo_path") for slot in slots))

    def test_ffmpeg_debug_page_masks_keys_and_shows_overrides(self):
        state, cfg = self.state_and_config()
        old_load_config = app.load_config
        old_get_state = app.get_state
        old_fetch = app.fetch_decryption_keys
        try:
            app.load_config = lambda: cfg
            app.get_state = lambda: state
            app.fetch_decryption_keys = (
                lambda license_url, source_cfg: "00" * 16 + "=" + "11" * 16
            )
            response = app.ffmpeg_source_debug(
                "provider2",
                1,
                input_args="-analyzeduration 0",
                output_args="-max_interleave_delta 0",
            )
        finally:
            app.load_config = old_load_config
            app.get_state = old_get_state
            app.fetch_decryption_keys = old_fetch
        text = response.body.decode("utf-8")

        self.assertIn("Aktueller Befehl", text)
        self.assertIn("&lt;masked 1 key(s)&gt;", text)
        self.assertNotIn("11111111111111111111111111111111", text)
        self.assertIn("-analyzeduration 0", text)
        self.assertIn("-max_interleave_delta 0", text)
        self.assertIn("input_args=-analyzeduration+0", text)
        self.assertIn('id="theme-toggle"', text)
        self.assertIn(':root[data-theme="dark"]', text)

    def test_status_page_includes_app_log_section(self):
        app.LOG.info("unit-test-log-visible")
        response = self.with_state(app.index)
        text = response.body.decode("utf-8")

        self.assertIn("<h2>App-Log</h2>", text)
        self.assertIn('href="/logs"', text)
        self.assertIn("unit-test-log-visible", text)

    def test_logs_endpoint_returns_ring_buffer(self):
        app.LOG.info("unit-test-log-endpoint")
        response = app.logs()
        text = response.body.decode("utf-8")

        self.assertIn("unit-test-log-endpoint", text)
        self.assertEqual(response.media_type, "text/plain; charset=utf-8")


class LiveTvOutputTests(unittest.TestCase):
    def state_and_config(self):
        cfg = dict(app.DEFAULTS)
        cfg.update(
            {
                "host_for_playlist": "127.0.0.1",
                "sources": [
                    {
                        "key": "provider3",
                        "name": "Provider 3",
                        "source_m3u": "http://example.invalid/provider3.m3u",
                        "slots": 1,
                    }
                ],
            }
        )
        channel = {
            "id": "abc123",
            "name": "ZDF HD",
            "url": "http://media.invalid/zdf.mpd",
            "group": "Deutschland",
            "logo": "https://img.invalid/zdf.png",
            "source_tvg_id": "zdf",
            "channel_number": "2",
            "source_key": "provider3",
            "source_name": "Provider 3",
            "stream_type": "url",
            "license_url": "",
            "ffmpeg_input_args": [],
        }
        return {"live_channel_count": 1, "live_channels": [channel]}, cfg

    def with_state(self, callback):
        state, cfg = self.state_and_config()
        old_load_config = app.load_config
        old_get_state = app.get_state
        try:
            app.load_config = lambda: cfg
            app.get_state = lambda: state
            return callback()
        finally:
            app.load_config = old_load_config
            app.get_state = old_get_state

    def test_live_page_shows_channel_without_exposing_stream_url(self):
        response = self.with_state(app.live_tv_index)
        text = response.body.decode("utf-8")

        self.assertIn("ZDF HD", text)
        self.assertIn("/live/provider3/abc123.m3u", text)
        self.assertIn('data-stream-url="/live/provider3/abc123.ts"', text)
        self.assertIn('/live/provider3/abc123/logo"', text)
        self.assertIn('id="theme-toggle"', text)
        self.assertIn('id="player-mode-toggle"', text)
        self.assertIn('id="web-player-modal"', text)
        self.assertIn('localStorage.setItem("tr-eventmux-theme", next)', text)
        self.assertIn(':root[data-theme="dark"]', text)
        self.assertNotIn("http://media.invalid/zdf.mpd", text)
        self.assertNotIn("https://img.invalid/zdf.png", text)

    def test_live_playlist_points_to_local_transport_stream(self):
        response = self.with_state(app.live_tv_playlist)
        text = response.body.decode("utf-8")

        self.assertIn('tvg-id="zdf"', text)
        self.assertIn("http://127.0.0.1:8787/live/provider3/abc123.ts", text)
        self.assertIn(
            'tvg-logo="http://127.0.0.1:8787/live/provider3/abc123/logo"', text
        )
        self.assertNotIn("http://media.invalid/zdf.mpd", text)
        self.assertNotIn("https://img.invalid/zdf.png", text)

    def test_single_channel_playlist_points_to_local_transport_stream(self):
        response = self.with_state(lambda: app.live_channel_playlist("provider3", "abc123"))
        text = response.body.decode("utf-8")

        self.assertIn("ZDF HD", text)
        self.assertIn("http://127.0.0.1:8787/live/provider3/abc123.ts", text)

    def test_logo_proxy_returns_supported_upstream_image(self):
        class Upstream:
            headers = {"content-type": "application/octet-stream"}

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size=1):
                yield b"\x89PNG\r\n\x1a\nimage-data"

            def close(self):
                pass

        old_get = app.requests.get
        try:
            app.requests.get = lambda *args, **kwargs: Upstream()
            response = self.with_state(
                lambda: app.live_channel_logo("provider3", "abc123")
            )
        finally:
            app.requests.get = old_get

        self.assertEqual(response.media_type, "image/png")
        self.assertTrue(response.body.startswith(b"\x89PNG"))
        self.assertEqual(response.headers["cache-control"], "public, max-age=3600")

    def test_logo_proxy_uses_placeholder_for_invalid_response(self):
        class Upstream:
            headers = {"content-type": "text/html"}

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size=1):
                yield b"<html>not an image</html>"

            def close(self):
                pass

        old_get = app.requests.get
        try:
            app.requests.get = lambda *args, **kwargs: Upstream()
            response = self.with_state(
                lambda: app.live_channel_logo("provider3", "abc123")
            )
        finally:
            app.requests.get = old_get

        self.assertEqual(response.media_type, "image/svg+xml")
        self.assertIn(b">TV</text>", response.body)


if __name__ == "__main__":
    unittest.main()
