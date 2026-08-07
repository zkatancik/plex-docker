import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "media-normalizer.py"
SPEC = importlib.util.spec_from_file_location("media_normalizer", MODULE_PATH)
media_normalizer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = media_normalizer
SPEC.loader.exec_module(media_normalizer)


def hvcc(*nal_types):
    data = bytearray(23)
    data[0] = 1
    data[22] = len(nal_types)
    for nal_type in nal_types:
        data.append(nal_type)
        data.extend((0, 1))
        data.extend((0, 1))
        data.append(1)
    return bytes(data)


def hexdump(data):
    lines = []
    for offset in range(0, len(data), 16):
        chunk = data[offset : offset + 16]
        groups = " ".join(chunk[index : index + 2].hex() for index in range(0, len(chunk), 2))
        lines.append("%08x: %-39s  ................" % (offset, groups))
    return "\n".join(lines)


def video_stream(codec="hevc", rate="24000/1001", complete=True, **updates):
    data = hvcc(32, 33, 34) if complete else hvcc()
    stream = {
        "index": 0,
        "codec_type": "video",
        "codec_name": codec,
        "width": 3840,
        "height": 2160,
        "pix_fmt": "yuv420p10le",
        "bits_per_raw_sample": "10",
        "avg_frame_rate": rate,
        "r_frame_rate": rate,
        "color_primaries": "bt709",
        "color_transfer": "bt709",
        "color_space": "bt709",
        "color_range": "tv",
        "extradata": hexdump(data),
        "extradata_size": len(data),
        "disposition": {},
        "tags": {},
    }
    stream.update(updates)
    return stream


def media_probe(video=None, *, duration=120.0, size=300_000_000, extras=None):
    streams = [video or video_stream()]
    streams.extend(extras or [])
    return {
        "streams": streams,
        "format": {
            "duration": str(duration),
            "size": str(size),
            "bit_rate": str(int(size * 8 / duration)),
        },
        "chapters": [],
    }


def settings(root):
    root = Path(root)
    return media_normalizer.Settings(
        repo_root=root,
        env_path=root / ".env",
        state_path=root / "runtime/state.json",
        lock_path=root / "runtime/lock",
        rollback_root=root / "rollback",
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
        sonarr_url="http://sonarr",
        sonarr_api_key="sonarr-secret",
        radarr_url="http://radarr",
        radarr_api_key="radarr-secret",
        tautulli_url="http://tautulli",
        tautulli_config=root / "tautulli.ini",
        plex_plist=root / "plex.plist",
        stable_age_seconds=0,
        settle_seconds=0,
        quality_window_seconds=1,
    )


class HvccTests(unittest.TestCase):
    def test_complete_hvcc_requires_vps_sps_and_pps(self):
        self.assertEqual(
            {"vps": 1, "sps": 1, "pps": 1},
            media_normalizer.hvcc_parameter_sets(hvcc(32, 33, 34)),
        )

    def test_empty_hvcc_is_incomplete(self):
        complete, missing = media_normalizer.hvcc_status(video_stream(complete=False))
        self.assertFalse(complete)
        self.assertEqual(["VPS", "SPS", "PPS"], missing)

    def test_each_parameter_set_is_individually_required(self):
        for omitted, expected in ((32, "VPS"), (33, "SPS"), (34, "PPS")):
            with self.subTest(omitted=omitted):
                present = [item for item in (32, 33, 34) if item != omitted]
                complete, missing = media_normalizer.hvcc_status(
                    video_stream(extradata=hexdump(hvcc(*present)))
                )
                self.assertFalse(complete)
                self.assertEqual([expected], missing)

    def test_truncated_hvcc_is_unknown(self):
        stream = video_stream(extradata=hexdump(b"\x01\x02"), extradata_size=2)
        self.assertIsNone(media_normalizer.hvcc_status(stream)[0])


class DetectionPolicyTests(unittest.TestCase):
    def test_supported_hfr_mappings(self):
        cases = {
            Fraction(48, 1): Fraction(24, 1),
            Fraction(50, 1): Fraction(25, 1),
            Fraction(60000, 1001): Fraction(30000, 1001),
            Fraction(60, 1): Fraction(30, 1),
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(expected, media_normalizer.hfr_target(source))

    def test_other_hfr_is_report_only(self):
        probe = media_probe(video_stream(rate="100/3"))
        report = media_normalizer.classify_media(Path("episode.mkv"), probe)
        self.assertEqual("report_only_unsupported_hfr", report["decision"])

    def test_hdr_is_never_selected_for_normalization(self):
        probe = media_probe(
            video_stream(
                complete=False,
                color_transfer="smpte2084",
                color_primaries="bt2020",
            )
        )
        report = media_normalizer.classify_media(Path("episode.mkv"), probe)
        self.assertEqual("report_only_hdr", report["decision"])
        self.assertIn("incomplete_hvcc", report["signatures"])

    def test_av1_sdr_is_normalizable(self):
        stream = video_stream(codec="av1", extradata="", extradata_size=0)
        report = media_normalizer.classify_media(Path("episode.mkv"), media_probe(stream))
        self.assertEqual("normalize", report["decision"])

    def test_resolution_bitrate_ranges_and_clamping(self):
        self.assertEqual((20_000_000, 35_000_000), media_normalizer.bitrate_range(3840, 2160))
        self.assertEqual((8_000_000, 15_000_000), media_normalizer.bitrate_range(1920, 1080))
        self.assertEqual((5_000_000, 10_000_000), media_normalizer.bitrate_range(1280, 720))
        probe = media_probe(size=100_000_000)
        self.assertEqual((20_000_000, 35_000_000), media_normalizer.select_bitrate(probe))

    def test_bitrate_retry_is_capped(self):
        self.assertEqual(12_500_000, media_normalizer.retry_bitrate(10_000_000, 15_000_000))
        self.assertEqual(15_000_000, media_normalizer.retry_bitrate(14_000_000, 15_000_000))
        self.assertIsNone(media_normalizer.retry_bitrate(15_000_000, 15_000_000))


class StateAndLoggingTests(unittest.TestCase):
    def test_atomic_state_recovers_from_backup(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            first = media_normalizer.empty_state()
            first["watermarks"]["sonarr"] = "first"
            second = media_normalizer.empty_state()
            second["watermarks"]["sonarr"] = "second"
            media_normalizer.atomic_write_json(path, first)
            media_normalizer.atomic_write_json(path, second)
            path.write_text("not json", encoding="utf-8")
            self.assertEqual("first", media_normalizer.load_state(path)["watermarks"]["sonarr"])

    def test_state_rejects_corrupt_primary_and_backup(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            path.write_text("bad", encoding="utf-8")
            path.with_suffix(".json.bak").write_text("also bad", encoding="utf-8")
            with self.assertRaises(media_normalizer.NormalizerError):
                media_normalizer.load_state(path)

    def test_token_redaction(self):
        text = "sonarr-secret api_key=radarr-secret X-Plex-Token=plex-secret"
        result = media_normalizer.redact_text(text, ["sonarr-secret", "radarr-secret"])
        self.assertNotIn("secret", result)
        self.assertEqual(3, result.count("[REDACTED]"))

    def test_subprocess_exception_is_wrapped(self):
        with mock.patch.object(subprocess, "run", side_effect=OSError("gone")):
            with self.assertRaises(media_normalizer.NormalizerError):
                media_normalizer.run_process(["missing"])

    def test_first_real_poll_establishes_watermark_without_enumerating_media(self):
        with tempfile.TemporaryDirectory() as root:
            config = settings(root)
            sonarr = mock.Mock()
            radarr = mock.Mock()
            with mock.patch.object(
                media_normalizer, "arr_clients", return_value={"sonarr": sonarr, "radarr": radarr}
            ):
                self.assertEqual(0, media_normalizer.run_poll(config, dry_run=False))
            state = media_normalizer.load_state(config.state_path)
            self.assertEqual({"sonarr", "radarr"}, set(state["watermarks"]))
            sonarr.managed_media.assert_not_called()
            radarr.managed_media.assert_not_called()

    def test_dry_run_does_not_create_first_watermark(self):
        with tempfile.TemporaryDirectory() as root:
            config = settings(root)
            with mock.patch.object(
                media_normalizer,
                "arr_clients",
                return_value={"sonarr": mock.Mock(), "radarr": mock.Mock()},
            ):
                self.assertEqual(0, media_normalizer.run_poll(config, dry_run=True))
            self.assertFalse(config.state_path.exists())

    def test_failed_future_import_is_recorded_for_retry(self):
        with tempfile.TemporaryDirectory() as root:
            config = settings(root)
            state = media_normalizer.empty_state()
            state["watermarks"] = {
                "sonarr": "2020-01-01T00:00:00Z",
                "radarr": "2020-01-01T00:00:00Z",
            }
            media_normalizer.atomic_write_json(config.state_path, state)
            item = media_normalizer.ManagedMedia(
                service="sonarr",
                owner_id=71,
                file_id=1,
                path=Path(root) / "episode.mkv",
                date_added="2026-01-01T00:00:00Z",
                season_number=4,
            )
            sonarr = mock.Mock()
            sonarr.managed_media.return_value = [item]
            radarr = mock.Mock()
            radarr.managed_media.return_value = []
            with (
                mock.patch.object(
                    media_normalizer,
                    "arr_clients",
                    return_value={"sonarr": sonarr, "radarr": radarr},
                ),
                mock.patch.object(
                    media_normalizer,
                    "normalize_one",
                    side_effect=media_normalizer.SafetyError("active playback"),
                ),
            ):
                self.assertEqual(1, media_normalizer.run_poll(config, dry_run=False))
            record = media_normalizer.load_state(config.state_path)["files"][str(item.path)]
            self.assertEqual("blocked", record["status"])
            self.assertTrue(record["automatic"])


class ArrTests(unittest.TestCase):
    def test_sonarr_media_polling_translates_container_paths(self):
        client = media_normalizer.ArrClient("sonarr", "http://sonarr", "key")

        def fake_get(endpoint, query=None):
            if endpoint == "series":
                return [{"id": 71, "path": "/data/Media/tv/Show"}]
            return [
                {
                    "id": 9,
                    "seriesId": 71,
                    "path": "/data/Media/tv/Show/Season 4/Episode.mkv",
                    "dateAdded": "2026-08-07T01:02:03Z",
                    "seasonNumber": 4,
                }
            ]

        with mock.patch.object(client, "get", side_effect=fake_get):
            result = client.managed_media()
        self.assertEqual(1, len(result))
        self.assertEqual(
            Path("/Volumes/HomeLabPool/Media/tv/Show/Season 4/Episode.mkv"), result[0].path
        )

    def test_custom_format_regex_is_read_from_api_shape(self):
        custom_format = {
            "specifications": [
                {
                    "implementation": "ReleaseTitleSpecification",
                    "fields": [{"name": "value", "value": media_normalizer.CUSTOM_HFR_REGEX}],
                }
            ]
        }
        self.assertEqual(
            media_normalizer.CUSTOM_HFR_REGEX,
            media_normalizer.custom_format_regex(custom_format),
        )


class ReplacementSafetyTests(unittest.TestCase):
    def test_hardlinked_replacement_never_changes_download_inode_or_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            library = root / "Episode.mkv"
            download = root / "Download.mkv"
            library.write_bytes(b"original torrent payload")
            os.link(library, download)
            original_inode = download.stat().st_ino
            output = media_normalizer.temporary_output(library)
            output.write_bytes(b"normalized library payload")
            media_normalizer.replace_atomically(output, library)
            self.assertEqual(b"original torrent payload", download.read_bytes())
            self.assertEqual(original_inode, download.stat().st_ino)
            self.assertEqual(b"normalized library payload", library.read_bytes())
            self.assertNotEqual(original_inode, library.stat().st_ino)

    def test_rollback_hardlink_is_atomic_source_preservation(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root / "source.mkv"
            destination = root / "rollback" / "source.mkv"
            source.write_bytes(b"payload")
            result = media_normalizer.create_rollback(source, destination)
            self.assertEqual("hardlink", result["method"])
            self.assertEqual(source.stat().st_ino, destination.stat().st_ino)

    def test_insufficient_space_is_blocked(self):
        usage = mock.Mock(free=99)
        with mock.patch.object(media_normalizer.shutil, "disk_usage", return_value=usage):
            with self.assertRaises(media_normalizer.SafetyError):
                media_normalizer.require_free_space(Path("/tmp"), 100)

    def test_lsof_activity_is_detected(self):
        result = subprocess.CompletedProcess([], 0, stdout="p123\n", stderr="")
        with mock.patch.object(media_normalizer, "run_process", return_value=result):
            self.assertTrue(media_normalizer.lsof_active(Path("episode.mkv")))

    def test_unexpected_temporary_path_cannot_replace_library(self):
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / "episode.mkv"
            output = Path(root) / "other.mkv"
            destination.touch()
            output.touch()
            with self.assertRaises(media_normalizer.SafetyError):
                media_normalizer.replace_atomically(output, destination)

    def test_idempotent_compatible_file_does_not_encode(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            path = root / "episode.mkv"
            path.touch()
            report = {"decision": "compatible", "signatures": [], "file_identity": "1:2:3"}
            state = media_normalizer.empty_state()
            with (
                mock.patch.object(media_normalizer, "inspect_path", return_value=report),
                mock.patch.object(media_normalizer, "run_process") as runner,
            ):
                result = media_normalizer.normalize_one(
                    path, settings(root), state, dry_run=False
                )
            self.assertEqual("compatible", result["status"])
            runner.assert_not_called()

    def test_interrupted_encode_keeps_original_and_records_rollback(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            path = root / "episode.mkv"
            path.write_bytes(b"original")
            config = settings(root)
            report = {
                "decision": "normalize",
                "signatures": ["incomplete_hvcc"],
                "frame_rate": "24000/1001",
                "file_identity": "1:2:3",
            }
            probe = media_probe(video_stream(complete=False))
            state = media_normalizer.empty_state()
            with (
                mock.patch.object(media_normalizer, "inspect_path", return_value=report),
                mock.patch.object(media_normalizer, "probe_media", return_value=probe),
                mock.patch.object(media_normalizer, "require_stable"),
                mock.patch.object(media_normalizer, "require_inactive"),
                mock.patch.object(media_normalizer, "require_free_space"),
                mock.patch.object(media_normalizer, "run_process", side_effect=KeyboardInterrupt),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    media_normalizer.normalize_one(path, config, state, dry_run=False)
            self.assertEqual(b"original", path.read_bytes())
            self.assertEqual("interrupted", state["files"][str(path.resolve())]["status"])
            rollback = Path(state["files"][str(path.resolve())]["rollback"]["path"])
            self.assertEqual(b"original", rollback.read_bytes())


class StreamValidationTests(unittest.TestCase):
    def test_audio_subtitles_chapters_and_dispositions_are_preserved(self):
        audio = {
            "codec_type": "audio",
            "codec_name": "eac3",
            "tags": {"language": "eng"},
            "disposition": {"default": 1, "forced": 0},
        }
        subtitle = {
            "codec_type": "subtitle",
            "codec_name": "subrip",
            "tags": {"language": "eng"},
            "disposition": {"default": 0, "forced": 1},
        }
        source = media_probe(video_stream(complete=False), extras=[audio, subtitle])
        output = media_probe(video_stream(complete=True), extras=[dict(audio), dict(subtitle)])
        source["chapters"] = [{"id": 1}]
        output["chapters"] = [{"id": 2}]
        report = {
            "frame_rate": "24000/1001",
            "signatures": ["incomplete_hvcc"],
        }
        media_normalizer.compare_streams(source, output, report)

    def test_quality_gate_requires_both_thresholds(self):
        media_normalizer.validate_quality({"mean_ssim": 0.98, "mean_psnr": 40.0})
        with self.assertRaises(media_normalizer.ValidationError):
            media_normalizer.validate_quality({"mean_ssim": 0.979, "mean_psnr": 50.0})
        with self.assertRaises(media_normalizer.ValidationError):
            media_normalizer.validate_quality({"mean_ssim": 0.99, "mean_psnr": 39.9})


if __name__ == "__main__":
    unittest.main()
