"""No real media/API/process work: actual poll and wrapper, mocked subprocesses."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import test_media_normalizer as fixtures
from test_media_normalizer_admission import result_fixture

m = fixtures.media_normalizer


class DecodeFailFastTests(unittest.TestCase):
    def samples(self, codes):
        results = [subprocess.CompletedProcess([], code, "", "decoder failure" if code else "")
                   for code in codes]
        with mock.patch.object(m, "run_process", side_effect=results) as run:
            observed = m.sample_decode(Path("fixture.mkv"), fixtures.media_probe(), "ffmpeg")
        return observed, run

    def test_first_definite_failure_stops_remaining_samples(self):
        observed, run = self.samples([183, 0, 0])
        self.assertEqual(run.call_count, 1)
        self.assertEqual([row["ok"] for row in observed], [False])
        self.assertEqual(observed[0]["error"], "decoder failure")

    def test_second_definite_failure_retains_prior_success_and_stops_third(self):
        observed, run = self.samples([0, 1, 0])
        self.assertEqual(run.call_count, 2)
        self.assertEqual([row["ok"] for row in observed], [True, False])

    def test_success_still_checks_all_three_positions(self):
        observed, run = self.samples([0, 0, 0])
        self.assertEqual(run.call_count, 3)
        self.assertEqual([row["ok"] for row in observed], [True, True, True])
        self.assertEqual(len({row["position"] for row in observed}), 3)

    def poll(self, first_code, expected, retryable):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            paths = [root / "a-first.mkv", root / "b-later.mkv"]
            for path in paths:
                path.write_bytes(b"unchanged fixture")
            before = {path: (path.stat().st_ino, path.read_bytes()) for path in paths}
            settings = fixtures.settings(root)
            state = m.empty_state()
            state["watermarks"] = {"sonarr": "2020-01-01T00:00:00Z"}
            items = [m.ManagedMedia("sonarr", 1, n, path, "2026-01-01T00:00:00Z")
                     for n, path in enumerate(paths)]
            def probe(path, _ffprobe):
                codec = "av1" if path == paths[0] else "hevc"
                return fixtures.media_probe(fixtures.video_stream(codec=codec))
            with result_fixture(root, first_code) as launch, \
                    mock.patch.object(m, "load_state", return_value=state), \
                    mock.patch.object(m, "arr_clients", return_value={"sonarr": mock.Mock()}), \
                    mock.patch.object(m, "all_managed", return_value=items), \
                    mock.patch.object(m, "probe_media", side_effect=probe) as metadata, \
                    mock.patch.object(m, "log"), \
                    mock.patch.object(m, "create_rollback") as rollback, \
                    mock.patch.object(m, "replace_atomically") as replace:
                result = m.run_poll(settings, dry_run=False)
            self.assertEqual(result, 75 if retryable else 0)
            self.assertEqual(launch.call_count, 1)
            self.assertEqual([call.args[0] for call in metadata.call_args_list], paths[:1] if retryable else paths)
            saved = m.load_state(settings.state_path)
            self.assertEqual(saved["files"][str(paths[0])]["status"], expected)
            if retryable:
                self.assertNotIn(str(paths[1]), saved["files"])
                self.assertIn("pending_poll", saved)
            else:
                self.assertEqual(saved["files"][str(paths[1])]["status"], "compatible")
            priority, unseen = m.select_poll_work(items, saved, saved["watermarks"], m.utc_now())
            self.assertEqual(priority, items if retryable else [])
            self.assertEqual(unseen, [])
            if retryable:
                self.assertNotEqual(saved["files"][str(paths[0])].get("decision"),
                                    "report_only_decode_failure")
            for path in paths:
                self.assertEqual((path.stat().st_ino, path.read_bytes()), before[path])
            rollback.assert_not_called()
            replace.assert_not_called()

    def test_definite_failure_preserves_later_file_progress_and_source(self):
        self.poll(183, "report_only_decode_failure", False)

    def test_capacity_deferral_preserves_pending_poll_without_more_waits(self):
        self.poll(75, "capacity_deferred", True)

    def test_interruption_preserves_pending_poll_without_more_waits(self):
        self.poll(124, "interrupted", True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
