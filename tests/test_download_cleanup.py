import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "download-cleanup.py"
SPEC = importlib.util.spec_from_file_location("download_cleanup", MODULE_PATH)
download_cleanup = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(download_cleanup)


class RetentionHoldTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "release.mkv"
        self.path.touch()

    def test_stopped_completed_torrent_is_protected_until_30_active_days(self):
        torrent = {
            "progress": 1,
            "seeding_time": download_cleanup.SEED_MIN_SECONDS - 1,
            "state": "stoppedUP",
        }
        self.assertIn("seeded", download_cleanup.retention_hold_reason(torrent))

    def test_error_state_can_only_be_cleaned_after_30_active_days(self):
        torrent = {
            "progress": 1,
            "seeding_time": download_cleanup.SEED_MIN_SECONDS,
            "state": "missingFiles",
        }
        self.assertIsNone(download_cleanup.retention_hold_reason(torrent))

    def test_incomplete_torrent_is_always_protected(self):
        torrent = {"progress": 0.99, "seeding_time": 0, "state": "stoppedDL"}
        self.assertIn("incomplete", download_cleanup.retention_hold_reason(torrent))

    def test_verified_dead_tracker_torrent_is_no_longer_protected(self):
        torrent = {
            "progress": 1,
            "seeding_time": download_cleanup.SEED_MIN_SECONDS - 1,
            "state": "stoppedUP",
            "_retention_verified": True,
        }
        self.assertIsNone(download_cleanup.retention_hold_reason(torrent))
        self.assertTrue(download_cleanup.seed_window_done(torrent))


class RetiredTrackerTests(unittest.TestCase):
    def test_all_real_trackers_must_explicitly_report_removed(self):
        trackers = [
            {"url": "** [DHT] **", "msg": ""},
            {"url": "https://seedpool.org/announce", "msg": "Torrent has been deleted."},
        ]
        self.assertTrue(download_cleanup.tracker_payload_is_retired(trackers))

    def test_working_tracker_prevents_retirement(self):
        trackers = [
            {"url": "https://seedpool.org/announce", "msg": "Torrent has been deleted."},
            {"url": "udp://tracker.example/announce", "msg": ""},
        ]
        self.assertFalse(download_cleanup.tracker_payload_is_retired(trackers))

    def test_transient_tracker_error_does_not_retire(self):
        trackers = [{"url": "https://seedpool.org/announce", "msg": "timed out"}]
        self.assertFalse(download_cleanup.tracker_payload_is_retired(trackers))

    def test_torrent_must_be_completed_for_full_window(self):
        now = 4_000_000
        torrent = {
            "progress": 1,
            "completion_on": now - download_cleanup.SEED_MIN_SECONDS + 1,
        }
        self.assertFalse(download_cleanup.torrent_is_old_enough_to_retire(torrent, now))
        torrent["completion_on"] -= 1
        self.assertTrue(download_cleanup.torrent_is_old_enough_to_retire(torrent, now))

    def test_verified_marker_survives_future_observations(self):
        state = download_cleanup.empty_retention_state()
        downloads = Path("/data/downloads/complete")
        torrent = {
            "hash": "hash",
            "name": "release.mkv",
            "content_path": str(downloads / "release.mkv"),
            "progress": 1,
            "seeding_time": 10,
        }
        download_cleanup.record_torrent_state(state, [torrent], downloads, 1_000_000)
        download_cleanup.mark_torrent_retention_verified(
            state, torrent, downloads, 1_000_000, "tracker deleted it"
        )
        torrent.pop("_retention_verified")
        download_cleanup.record_torrent_state(state, [torrent], downloads, 1_000_060)
        self.assertTrue(torrent["_retention_verified"])

    def test_verified_retirement_is_not_restarted(self):
        torrent = {
            "progress": 1,
            "seeding_time": download_cleanup.SEED_MIN_SECONDS - 1,
            "state": "stoppedUP",
            "_retention_verified": True,
        }
        self.assertFalse(download_cleanup.torrent_stopped_too_early(torrent))
        torrent["_retention_verified"] = False
        self.assertTrue(download_cleanup.torrent_stopped_too_early(torrent))



class UnmatchedQuarantineTests(unittest.TestCase):
    def test_unknown_path_enters_finite_quarantine(self):
        state = download_cleanup.empty_retention_state()
        reason = download_cleanup.unmatched_hold_reason(state, "release.mkv", 1_000_000, mutate=True)
        self.assertIn("quarantine", reason)
        self.assertEqual(1_000_000, state["paths"]["release.mkv"]["unmatched_since"])

    def test_unknown_path_is_cleanable_when_quarantine_expires(self):
        state = download_cleanup.empty_retention_state()
        download_cleanup.unmatched_hold_reason(state, "release.mkv", 1_000_000, mutate=True)
        reason = download_cleanup.unmatched_hold_reason(
            state,
            "release.mkv",
            1_000_000 + download_cleanup.UNMATCHED_GRACE_SECONDS,
            mutate=True,
        )
        self.assertIsNone(reason)
        self.assertEqual(
            "unmatched quarantine expired",
            download_cleanup.unmatched_cleanup_label(state, "release.mkv"),
        )

    def test_dry_run_does_not_start_quarantine_clock(self):
        state = download_cleanup.empty_retention_state()
        download_cleanup.unmatched_hold_reason(state, "release.mkv", 1_000_000, mutate=False)
        self.assertNotIn("release.mkv", state["paths"])

    def test_verified_seed_history_allows_immediate_cleanup(self):
        state = {
            "version": download_cleanup.STATE_VERSION,
            "paths": {
                "release.mkv": {
                    "torrents": {"hash": {"seed_window_done": True, "seeding_time": 2592000}}
                }
            },
        }
        self.assertIsNone(
            download_cleanup.unmatched_hold_reason(state, "release.mkv", 1_000_000, mutate=True)
        )
        self.assertEqual(
            "seed window verified",
            download_cleanup.unmatched_cleanup_label(state, "release.mkv"),
        )

    def test_record_preserves_highest_observed_seed_time(self):
        state = download_cleanup.empty_retention_state()
        downloads = Path("/data/downloads/complete")
        torrent = {
            "hash": "hash",
            "name": "release.mkv",
            "content_path": str(downloads / "release.mkv"),
            "progress": 1,
            "seeding_time": download_cleanup.SEED_MIN_SECONDS,
        }
        download_cleanup.record_torrent_state(state, [torrent], downloads, 1_000_000)
        torrent["seeding_time"] = 0
        download_cleanup.record_torrent_state(state, [torrent], downloads, 1_000_060)
        record = state["paths"]["release.mkv"]["torrents"]["hash"]
        self.assertEqual(download_cleanup.SEED_MIN_SECONDS, record["seeding_time"])
        self.assertTrue(record["seed_window_done"])


class CleanupRunTests(unittest.TestCase):
    def run_with_managed_release(self, torrent_fields):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            downloads = root / "complete"
            media_tv = root / "tv"
            media_movies = root / "movies"
            for path in (downloads, media_tv, media_movies):
                path.mkdir()

            release = downloads / "superseded-release.mkv"
            release.write_bytes(b"release payload")
            state_path = root / "state.json"
            torrent = {
                "hash": "original-hash",
                "name": release.name,
                "content_path": str(release),
                "save_path": str(downloads),
                **torrent_fields,
            }

            fake_qb = mock.Mock()
            fake_qb.torrents.return_value = [torrent]
            config = {
                "downloads": str(downloads),
                "media_tv": str(media_tv),
                "media_movies": str(media_movies),
                "state_path": str(state_path),
                "qb_url": "http://qbit",
                "qb_user": "user",
                "qb_pass": "pass",
            }
            with (
                mock.patch.dict(download_cleanup.DEFAULTS, config),
                mock.patch.object(download_cleanup, "QBittorrent", return_value=fake_qb),
                mock.patch("sys.stdout"),
            ):
                result = download_cleanup.run(dry_run=False, include_seeding=False)
            return result, release.exists(), fake_qb

    def run_with_unmatched_age(self, age_seconds):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            downloads = root / "complete"
            media_tv = root / "tv"
            media_movies = root / "movies"
            for path in (downloads, media_tv, media_movies):
                path.mkdir()
            orphan = downloads / "orphan.bin"
            orphan.write_bytes(b"orphan")
            state_path = root / "state.json"
            now = int(download_cleanup.time.time())
            state = download_cleanup.empty_retention_state()
            state["paths"][orphan.name] = {
                "first_seen": now - age_seconds,
                "last_seen": now - age_seconds,
                "unmatched_since": now - age_seconds,
                "torrents": {},
            }
            download_cleanup.save_retention_state(state_path, state)

            fake_qb = mock.Mock()
            fake_qb.torrents.return_value = []
            config = {
                "downloads": str(downloads),
                "media_tv": str(media_tv),
                "media_movies": str(media_movies),
                "state_path": str(state_path),
                "qb_url": "http://qbit",
                "qb_user": "user",
                "qb_pass": "pass",
            }
            with (
                mock.patch.dict(download_cleanup.DEFAULTS, config),
                mock.patch.object(download_cleanup, "QBittorrent", return_value=fake_qb),
                mock.patch("sys.stdout"),
            ):
                result = download_cleanup.run(dry_run=False, include_seeding=False)
            return result, orphan.exists()

    def test_unmatched_file_is_kept_during_quarantine(self):
        result, exists = self.run_with_unmatched_age(download_cleanup.UNMATCHED_GRACE_SECONDS - 60)
        self.assertEqual(0, result)
        self.assertTrue(exists)

    def test_unmatched_file_is_deleted_after_quarantine(self):
        result, exists = self.run_with_unmatched_age(download_cleanup.UNMATCHED_GRACE_SECONDS + 60)
        self.assertEqual(0, result)
        self.assertFalse(exists)

    def test_superseded_stopped_torrent_is_not_deleted_before_seed_window(self):
        result, exists, fake_qb = self.run_with_managed_release(
            {"progress": 1, "seeding_time": 20 * 3600, "state": "stoppedUP"}
        )
        self.assertEqual(0, result)
        self.assertTrue(exists)
        fake_qb.delete_torrents.assert_not_called()

    def test_startup_transitional_torrent_is_not_deleted(self):
        result, exists, fake_qb = self.run_with_managed_release(
            {"progress": 0, "seeding_time": 0, "state": "checkingResumeData"}
        )
        self.assertEqual(0, result)
        self.assertTrue(exists)
        fake_qb.delete_torrents.assert_not_called()


class ShareLimitTests(unittest.TestCase):
    def test_queueing_limits_downloads_without_limiting_seeding(self):
        updates = download_cleanup.required_preference_updates(
            {
                "max_seeding_time_enabled": True,
                "max_seeding_time": download_cleanup.SEED_MIN_MINUTES,
                "max_ratio_enabled": False,
                "max_inactive_seeding_time_enabled": False,
                "max_ratio_act": 0,
                "queueing_enabled": False,
                "max_active_downloads": 8,
                "max_active_uploads": 3,
                "max_active_torrents": 5,
            }
        )
        self.assertTrue(updates["queueing_enabled"])
        self.assertEqual(download_cleanup.MAX_ACTIVE_DOWNLOADS, updates["max_active_downloads"])
        self.assertEqual(-1, updates["max_active_uploads"])
        self.assertEqual(-1, updates["max_active_torrents"])

    def test_global_preferences_disable_early_ratio_and_inactivity_stops(self):
        updates = download_cleanup.required_preference_updates(
            {
                "max_seeding_time_enabled": True,
                "max_seeding_time": 14400,
                "max_ratio_enabled": True,
                "max_inactive_seeding_time_enabled": True,
            }
        )
        self.assertEqual(download_cleanup.SEED_MIN_MINUTES, updates["max_seeding_time"])
        self.assertFalse(updates["max_ratio_enabled"])
        self.assertFalse(updates["max_inactive_seeding_time_enabled"])

    def test_higher_global_seed_limit_is_preserved(self):
        updates = download_cleanup.required_preference_updates(
            {
                "max_seeding_time_enabled": True,
                "max_seeding_time": 60 * 24 * 60,
                "max_ratio_enabled": False,
                "max_inactive_seeding_time_enabled": False,
            }
        )
        self.assertNotIn("max_seeding_time", updates)

    def test_short_per_torrent_limit_is_raised(self):
        limits = download_cleanup.required_torrent_limits(
            {
                "ratio_limit": -2,
                "seeding_time_limit": 14400,
                "inactive_seeding_time_limit": -2,
            }
        )
        self.assertEqual((-2, download_cleanup.SEED_MIN_MINUTES, -2), limits)

    def test_short_effective_category_limit_is_overridden(self):
        limits = download_cleanup.required_torrent_limits(
            {
                "ratio_limit": -2,
                "seeding_time_limit": -2,
                "inactive_seeding_time_limit": -2,
                "max_ratio": -1,
                "max_seeding_time": 14400,
                "max_inactive_seeding_time": -1,
            }
        )
        self.assertEqual((-2, download_cleanup.SEED_MIN_MINUTES, -2), limits)

    def test_ratio_and_inactivity_cannot_end_seeding_early(self):
        limits = download_cleanup.required_torrent_limits(
            {
                "ratio_limit": 1.0,
                "seeding_time_limit": 50000,
                "inactive_seeding_time_limit": 60,
            }
        )
        self.assertEqual((-1, 50000, -1), limits)

    def test_inherit_and_no_limit_sentinels_are_preserved(self):
        self.assertIsNone(
            download_cleanup.required_torrent_limits(
                {
                    "ratio_limit": -2,
                    "seeding_time_limit": -1,
                    "inactive_seeding_time_limit": -2,
                }
            )
        )

    def test_qbit_share_limit_payload_supports_5_2_and_newer(self):
        qb = object.__new__(download_cleanup.QBittorrent)
        qb.post = mock.Mock()
        qb.set_share_limits("hash", -1, 43200, -1, "Default", "Default")
        _, values = qb.post.call_args.args
        self.assertEqual("Default", values["shareLimitAction"])
        self.assertEqual("Default", values["shareLimitsMode"])

    def test_qbit_stop_uses_stop_endpoint(self):
        qb = object.__new__(download_cleanup.QBittorrent)
        qb.post = mock.Mock()
        qb.stop_torrents(["a", "b"])
        qb.post.assert_called_once_with("/api/v2/torrents/stop", {"hashes": "a|b"})


class ArrRetentionTests(unittest.TestCase):
    def test_all_arr_download_auto_removal_is_disabled(self):
        api = mock.Mock()
        api.download_clients.return_value = [
            {
                "id": 1,
                "enable": True,
                "name": "qBittorrent",
                "implementation": "QBittorrent",
                "removeCompletedDownloads": True,
                "removeFailedDownloads": True,
            }
        ]
        with mock.patch.object(download_cleanup, "ArrApi", return_value=api):
            actions = download_cleanup.guard_arr_removal(
                "Sonarr", "http://sonarr:8989", "key", dry_run=False
            )
        self.assertEqual(1, len(actions))
        updated = api.update_download_client.call_args.args[0]
        self.assertFalse(updated["removeCompletedDownloads"])
        self.assertFalse(updated["removeFailedDownloads"])

    def test_dry_run_does_not_update_arr(self):
        api = mock.Mock()
        api.download_clients.return_value = [
            {
                "id": 1,
                "enable": True,
                "implementation": "QBittorrent",
                "removeCompletedDownloads": True,
                "removeFailedDownloads": True,
            }
        ]
        with mock.patch.object(download_cleanup, "ArrApi", return_value=api):
            download_cleanup.guard_arr_removal(
                "Radarr", "http://radarr:7878", "key", dry_run=True
            )
        api.update_download_client.assert_not_called()


class SonarrQueueReconciliationTests(unittest.TestCase):
    @staticmethod
    def record(message="Invalid season or episode", episode_id=101):
        return {
            "downloadId": "ABC123",
            "episodeId": episode_id,
            "status": "completed",
            "trackedDownloadStatus": "warning",
            "trackedDownloadState": "importPending",
            "statusMessages": [{"title": "00000.m2ts", "messages": [message]}],
        }

    @staticmethod
    def torrent(now=1_000_000):
        return {
            "hash": "abc123",
            "name": "disc release",
            "category": "sonarr",
            "progress": 1,
            "completion_on": now - 3600,
        }

    def test_only_explicit_terminal_reasons_are_eligible(self):
        self.assertTrue(
            download_cleanup.is_terminal_arr_queue_record(
                self.record(), download_cleanup.SONARR_TERMINAL_QUEUE_REASONS
            )
        )
        self.assertFalse(
            download_cleanup.is_terminal_arr_queue_record(
                self.record("Access to the path is denied"),
                download_cleanup.SONARR_TERMINAL_QUEUE_REASONS,
            )
        )

    def test_candidate_requires_existing_episode_and_grace_period(self):
        now = 1_000_000
        record = self.record()
        torrent = self.torrent(now)
        candidates = download_cleanup.terminal_arr_queue_groups(
            [record], [torrent], {101}, {"sonarr"}, "episodeId",
            download_cleanup.SONARR_TERMINAL_QUEUE_REASONS, (), now, 1800
        )
        self.assertEqual([(torrent, [record])], candidates)

        self.assertEqual(
            [],
            download_cleanup.terminal_arr_queue_groups(
                [record], [torrent], set(), {"sonarr"}, "episodeId",
                download_cleanup.SONARR_TERMINAL_QUEUE_REASONS, (), now, 1800
            ),
        )
        torrent["completion_on"] = now - 60
        self.assertEqual(
            [],
            download_cleanup.terminal_arr_queue_groups(
                [record], [torrent], {101}, {"sonarr"}, "episodeId",
                download_cleanup.SONARR_TERMINAL_QUEUE_REASONS, (), now, 1800
            ),
        )

    def test_reconcile_moves_category_without_deleting_torrent(self):
        api = mock.Mock()
        api.download_clients.return_value = [
            {
                "enable": True,
                "implementation": "QBittorrent",
                "fields": [{"name": "tvCategory", "value": "sonarr"}],
            }
        ]
        api.queue.return_value = [self.record()]
        api.managed_item.return_value = {"hasFile": True}

        qb = mock.Mock()
        qb.torrents.return_value = [self.torrent(now=1_000_000)]
        qb.categories.return_value = {}
        with (
            mock.patch.object(download_cleanup, "ArrApi", return_value=api),
            mock.patch.object(download_cleanup.time, "time", return_value=1_000_000),
            mock.patch("sys.stdout"),
        ):
            result = download_cleanup.reconcile_arr_queue(
                qb, False, "Sonarr", "http://sonarr", "key", "sonarr-rejected",
                "tvCategory", "episodeId", "episode",
                "page=1&includeUnknownSeriesItems=true",
                download_cleanup.SONARR_TERMINAL_QUEUE_REASONS, (), 1800,
            )

        self.assertEqual(0, result)
        qb.create_category.assert_called_once_with("sonarr-rejected")
        qb.set_category.assert_called_once_with(["abc123"], "sonarr-rejected")
        qb.delete_torrents.assert_not_called()

    def test_dry_run_never_changes_qbittorrent(self):
        api = mock.Mock()
        api.download_clients.return_value = [
            {
                "enable": True,
                "implementation": "QBittorrent",
                "fields": [{"name": "tvCategory", "value": "sonarr"}],
            }
        ]
        api.queue.return_value = [self.record()]
        api.managed_item.return_value = {"hasFile": True}
        qb = mock.Mock()
        qb.torrents.return_value = [self.torrent(now=1_000_000)]
        with (
            mock.patch.object(download_cleanup, "ArrApi", return_value=api),
            mock.patch.object(download_cleanup.time, "time", return_value=1_000_000),
            mock.patch("sys.stdout"),
        ):
            result = download_cleanup.reconcile_arr_queue(
                qb, True, "Sonarr", "http://sonarr", "key", "sonarr-rejected",
                "tvCategory", "episodeId", "episode",
                "page=1&includeUnknownSeriesItems=true",
                download_cleanup.SONARR_TERMINAL_QUEUE_REASONS, (), 1800,
            )

        self.assertEqual(0, result)
        qb.create_category.assert_not_called()
        qb.set_category.assert_not_called()


class RadarrQueueReconciliationTests(unittest.TestCase):
    @staticmethod
    def record(message="Invalid movie"):
        return {
            "downloadId": "MOVIE123",
            "movieId": 202,
            "status": "completed",
            "trackedDownloadStatus": "warning",
            "trackedDownloadState": "importBlocked",
            "statusMessages": [{"title": "00000.m2ts", "messages": [message]}],
        }

    def test_radarr_terminal_prefixes_are_explicit_and_permission_errors_remain(self):
        lower_quality = (
            "Not an upgrade for existing movie file. Existing quality: Bluray-2160p. "
            "New Quality WEBDL-2160p."
        )
        self.assertTrue(
            download_cleanup.is_terminal_arr_queue_record(
                self.record(lower_quality),
                download_cleanup.RADARR_TERMINAL_QUEUE_REASONS,
                download_cleanup.RADARR_TERMINAL_QUEUE_REASON_PREFIXES,
            )
        )
        self.assertFalse(
            download_cleanup.is_terminal_arr_queue_record(
                self.record("Access to the path is denied"),
                download_cleanup.RADARR_TERMINAL_QUEUE_REASONS,
                download_cleanup.RADARR_TERMINAL_QUEUE_REASON_PREFIXES,
            )
        )

    def test_radarr_uses_movie_api_and_separate_rejected_category(self):
        api = mock.Mock()
        api.download_clients.return_value = [
            {
                "enable": True,
                "implementation": "QBittorrent",
                "fields": [{"name": "movieCategory", "value": "radarr"}],
            }
        ]
        api.queue.return_value = [self.record()]
        api.managed_item.return_value = {"hasFile": True}

        qb = mock.Mock()
        qb.torrents.return_value = [
            {
                "hash": "movie123",
                "name": "disc movie",
                "category": "radarr",
                "progress": 1,
                "completion_on": 1_000_000 - 3600,
            }
        ]
        qb.categories.return_value = {"sonarr-rejected": {}}
        query = "page=1&pageSize=1000&includeUnknownMovieItems=true&includeMovie=false"
        with (
            mock.patch.object(download_cleanup, "ArrApi", return_value=api),
            mock.patch.object(download_cleanup.time, "time", return_value=1_000_000),
            mock.patch("sys.stdout"),
        ):
            result = download_cleanup.reconcile_arr_queue(
                qb, False, "Radarr", "http://radarr", "key", "radarr-rejected",
                "movieCategory", "movieId", "movie", query,
                download_cleanup.RADARR_TERMINAL_QUEUE_REASONS,
                download_cleanup.RADARR_TERMINAL_QUEUE_REASON_PREFIXES, 1800,
            )

        self.assertEqual(0, result)
        api.queue.assert_called_once_with(query)
        api.managed_item.assert_called_once_with("movie", 202)
        qb.create_category.assert_called_once_with("radarr-rejected")
        qb.set_category.assert_called_once_with(["movie123"], "radarr-rejected")
        qb.delete_torrents.assert_not_called()

    def test_combined_reconciler_invokes_sonarr_and_radarr(self):
        qb = mock.Mock()
        calls = []

        def capture(**kwargs):
            calls.append(kwargs)
            return 0

        with (
            mock.patch.dict(
                download_cleanup.DEFAULTS,
                {
                    "qb_url": "http://qbit",
                    "qb_user": "user",
                    "qb_pass": "pass",
                    "sonarr_url": "http://sonarr",
                    "sonarr_api_key": "sonarr-key",
                    "sonarr_detached_category": "sonarr-rejected",
                    "radarr_url": "http://radarr",
                    "radarr_api_key": "radarr-key",
                    "radarr_detached_category": "radarr-rejected",
                    "queue_reconcile_grace_seconds": 1800,
                },
            ),
            mock.patch.object(download_cleanup, "QBittorrent", return_value=qb),
            mock.patch.object(download_cleanup, "reconcile_arr_queue", side_effect=capture),
        ):
            result = download_cleanup.reconcile_arr_queues(dry_run=True)

        self.assertEqual(0, result)
        self.assertEqual(["Sonarr", "Radarr"], [call["label"] for call in calls])
        self.assertEqual("movieCategory", calls[1]["category_field"])
        self.assertEqual("movieId", calls[1]["item_id_field"])
        self.assertEqual("movie", calls[1]["item_resource"])


class UnmonitoredScopeTests(unittest.TestCase):
    def test_sonarr_unmonitored_season_selects_only_that_seasons_files(self):
        api = mock.Mock()

        def resources(resource, query=""):
            if resource == "series":
                return [
                    {
                        "id": 7,
                        "title": "Example",
                        "path": "/data/Media/tv/Example",
                        "monitored": True,
                        "seasons": [
                            {"seasonNumber": 1, "monitored": True},
                            {"seasonNumber": 2, "monitored": False},
                        ],
                    }
                ]
            if resource == "episodefile":
                return [
                    {"id": 10, "seasonNumber": 1, "path": "/data/Media/tv/Example/Season 01/a.mkv"},
                    {"id": 20, "seasonNumber": 2, "path": "/data/Media/tv/Example/Season 02/b.mkv"},
                ]
            if resource == "episode":
                return [
                    {"id": 101, "seasonNumber": 1, "episodeFileId": 10},
                    {"id": 201, "seasonNumber": 2, "episodeFileId": 20},
                ]
            return []

        api.resources.side_effect = resources
        scopes = download_cleanup.sonarr_eviction_scopes(api)
        self.assertEqual(1, len(scopes))
        self.assertEqual("season", scopes[0]["kind"])
        self.assertEqual(2, scopes[0]["season_number"])
        self.assertEqual({20}, scopes[0]["file_ids"])
        self.assertEqual({201}, scopes[0]["episode_ids"])

    def test_sonarr_unmonitored_series_supersedes_season_scopes(self):
        api = mock.Mock()
        api.resources.side_effect = lambda resource, query="": {
            "series": [
                {
                    "id": 7,
                    "title": "Example",
                    "path": "/data/Media/tv/Example",
                    "monitored": False,
                    "seasons": [{"seasonNumber": 1, "monitored": False}],
                }
            ],
            "episodefile": [
                {"id": 10, "seasonNumber": 1, "path": "/data/Media/tv/Example/Season 01/a.mkv"}
            ],
            "episode": [{"id": 101, "seasonNumber": 1, "episodeFileId": 10}],
        }.get(resource, [])
        scopes = download_cleanup.sonarr_eviction_scopes(api)
        self.assertEqual(["series"], [scope["kind"] for scope in scopes])
        self.assertEqual({10}, scopes[0]["file_ids"])

    def test_radarr_monitored_movie_is_not_a_candidate(self):
        api = mock.Mock()
        api.resources.return_value = [{"id": 1, "monitored": True}]
        self.assertEqual([], download_cleanup.radarr_eviction_scopes(api))

    def test_radarr_unmonitored_movie_selects_its_movie_file(self):
        api = mock.Mock()
        api.resources.side_effect = lambda resource, query="": {
            "movie": [
                {
                    "id": 8,
                    "title": "Example",
                    "path": "/data/Media/movies/Example",
                    "monitored": False,
                }
            ],
            "moviefile": [
                {"id": 80, "path": "/data/Media/movies/Example/movie.mkv"}
            ],
        }.get(resource, [])
        scopes = download_cleanup.radarr_eviction_scopes(api)
        self.assertEqual(1, len(scopes))
        self.assertEqual("movie", scopes[0]["kind"])
        self.assertEqual({80}, scopes[0]["file_ids"])

    def test_arr_bulk_file_deletion_uses_the_expected_payload(self):
        api = object.__new__(download_cleanup.ArrApi)
        api.request = mock.Mock()
        api.delete_files("episodefile", "episodeFileIds", [1, 2])
        api.request.assert_called_once_with(
            "/api/v3/episodefile/bulk",
            method="DELETE",
            payload={"episodeFileIds": [1, 2]},
        )

    def test_history_association_survives_a_broken_hardlink(self):
        scope = {
            "app": "Radarr",
            "kind": "movie",
            "owner_id": 8,
            "file_ids": {50},
            "file_paths": {Path("/data/Media/movies/Example/new-inode.mkv")},
            "episode_ids": set(),
        }
        history = [
            {
                "movieId": 8,
                "downloadId": "ABC123",
                "data": {
                    "fileId": "50",
                    "droppedPath": "/data/downloads/complete/release.mkv",
                },
            }
        ]
        download_cleanup.associate_scope_downloads(
            scope,
            history,
            [],
            {},
            Path("/data/downloads/complete"),
            {"abc123"},
        )
        self.assertEqual({"abc123"}, scope["torrent_hashes"])
        self.assertEqual({"release.mkv"}, scope["download_top_levels"])

    def test_season_cleanup_never_removes_a_directory_with_other_season_files(self):
        with tempfile.TemporaryDirectory() as root_value:
            root = Path(root_value) / "Series"
            mixed = root / "Mixed"
            mixed.mkdir(parents=True)
            target = mixed / "season-2.mkv"
            other = mixed / "season-1.mkv"
            target.touch()
            other.touch()
            scope = {
                "owner_path": root,
                "file_paths": {target},
                "all_file_paths": {target, other},
            }
            self.assertEqual(set(), download_cleanup.season_cleanup_dirs(scope))


class UnmonitoredEvictionTests(unittest.TestCase):
    def test_stale_media_mutation_lock_is_recovered(self):
        with tempfile.TemporaryDirectory() as root_value:
            path = Path(root_value) / "locks/media.lock"
            path.mkdir(parents=True)
            old = download_cleanup.time.time() - 10
            os.utime(path, (old, old))
            with download_cleanup.media_mutation_lock(path, stale_seconds=1):
                self.assertTrue(path.is_dir())
            self.assertFalse(path.exists())

    def run_sonarr_eviction(self, seed_time, *, dry_run=False, lock_busy=False):
        with tempfile.TemporaryDirectory() as root_value:
            root = Path(root_value)
            downloads = root / "downloads" / "complete"
            media_tv = root / "Media" / "tv"
            media_movies = root / "Media" / "movies"
            series_path = media_tv / "Example"
            season_path = series_path / "Season 02"
            for path in (downloads, season_path, media_movies):
                path.mkdir(parents=True)
            payload = downloads / "release.mkv"
            payload.write_bytes(b"episode")
            library_file = season_path / "episode.mkv"
            os.link(payload, library_file)
            lock_path = root / ".locks" / "media.lock"
            if lock_busy:
                lock_path.mkdir(parents=True)

            sonarr = mock.Mock()

            def sonarr_resources(resource, query=""):
                if resource == "series":
                    return [
                        {
                            "id": 7,
                            "title": "Example",
                            "path": str(series_path),
                            "monitored": True,
                            "seasons": [
                                {"seasonNumber": 1, "monitored": True},
                                {"seasonNumber": 2, "monitored": False},
                            ],
                        }
                    ]
                if resource == "episodefile":
                    return [
                        {
                            "id": 20,
                            "seasonNumber": 2,
                            "path": str(library_file),
                            "relativePath": "Season 02/episode.mkv",
                        }
                    ]
                if resource == "episode":
                    return [{"id": 201, "seasonNumber": 2, "episodeFileId": 20}]
                return []

            sonarr.resources.side_effect = sonarr_resources
            sonarr.history.return_value = [
                {
                    "seriesId": 7,
                    "episodeId": 201,
                    "downloadId": "ABC123",
                    "data": {"fileId": "20", "droppedPath": str(payload)},
                }
            ]
            sonarr.queue.return_value = []
            sonarr.managed_item.return_value = {
                "id": 7,
                "monitored": True,
                "seasons": [{"seasonNumber": 2, "monitored": False}],
            }

            radarr = mock.Mock()
            radarr.resources.return_value = []
            radarr.history.return_value = []
            radarr.queue.return_value = []
            qb = mock.Mock()
            torrent = {
                "hash": "abc123",
                "name": payload.name,
                "content_path": str(payload),
                "save_path": str(downloads),
                "progress": 1,
                "seeding_time": seed_time,
                "state": "stoppedUP",
            }
            cfg = {
                **download_cleanup.DEFAULTS,
                "downloads": str(downloads),
                "media_tv": str(media_tv),
                "media_movies": str(media_movies),
                "sonarr_url": "http://sonarr",
                "sonarr_api_key": "sonarr-key",
                "radarr_url": "http://radarr",
                "radarr_api_key": "radarr-key",
                "unmonitored_eviction_enabled": True,
                "media_mutation_lock_path": str(lock_path),
                "normalizer_state_path": str(root / "normalizer-state.json"),
                "normalizer_host_pool_path": str(root),
            }
            with mock.patch.object(
                download_cleanup,
                "ArrApi",
                side_effect=lambda url, key: sonarr if "sonarr" in url else radarr,
            ):
                result = download_cleanup.evict_unmonitored_media(
                    cfg,
                    qb,
                    [torrent],
                    download_cleanup.empty_retention_state(),
                    downloads,
                    dry_run,
                )
            return result, library_file.exists(), season_path.exists(), qb, sonarr

    def test_under_seeded_season_is_left_completely_untouched(self):
        result, file_exists, directory_exists, qb, sonarr = self.run_sonarr_eviction(
            download_cleanup.SEED_MIN_SECONDS - 1
        )
        actions, changed, failed = result
        self.assertTrue(any("EVICT-WAIT" in action for action in actions))
        self.assertFalse(changed)
        self.assertFalse(failed)
        self.assertTrue(file_exists)
        self.assertTrue(directory_exists)
        qb.stop_torrents.assert_not_called()
        sonarr.delete_files.assert_not_called()

    def test_eligible_season_files_are_evicted_but_arr_entry_is_retained(self):
        result, file_exists, directory_exists, qb, sonarr = self.run_sonarr_eviction(
            download_cleanup.SEED_MIN_SECONDS
        )
        actions, changed, failed = result
        self.assertTrue(any(action.startswith("EVICT      ") for action in actions))
        self.assertTrue(changed)
        self.assertFalse(failed)
        self.assertFalse(file_exists)
        self.assertFalse(directory_exists)
        qb.stop_torrents.assert_called_once_with(["abc123"])
        sonarr.delete_files.assert_called_once_with("episodefile", "episodeFileIds", [20])
        sonarr.delete.assert_not_called()

    def test_dry_run_does_not_stop_or_delete_anything(self):
        result, file_exists, directory_exists, qb, sonarr = self.run_sonarr_eviction(
            download_cleanup.SEED_MIN_SECONDS,
            dry_run=True,
        )
        actions, changed, failed = result
        self.assertTrue(any("WOULD EVICT" in action for action in actions))
        self.assertFalse(changed)
        self.assertFalse(failed)
        self.assertTrue(file_exists)
        self.assertTrue(directory_exists)
        qb.stop_torrents.assert_not_called()
        sonarr.delete_files.assert_not_called()

    def test_busy_media_mutation_lock_defers_the_whole_phase(self):
        result, file_exists, _, qb, sonarr = self.run_sonarr_eviction(
            download_cleanup.SEED_MIN_SECONDS,
            lock_busy=True,
        )
        actions, changed, failed = result
        self.assertEqual(["EVICT-WAIT media normalizer is mutating the library"], actions)
        self.assertFalse(changed)
        self.assertFalse(failed)
        self.assertTrue(file_exists)
        qb.stop_torrents.assert_not_called()
        sonarr.delete_files.assert_not_called()


class TorrentMatchingTests(unittest.TestCase):
    def test_multiple_jobs_for_one_payload_are_all_retained(self):
        downloads = Path("/data/downloads/complete")
        torrents = [
            {"hash": "a", "name": "release.mkv", "content_path": str(downloads / "release.mkv")},
            {"hash": "b", "name": "release.mkv", "content_path": str(downloads / "release.mkv")},
        ]
        by_name, by_top = download_cleanup.torrent_maps(torrents, downloads)
        matches = download_cleanup.matched_torrents("release.mkv", by_name, by_top)
        self.assertEqual({"a", "b"}, {torrent["hash"] for torrent in matches})

    def test_nested_basename_collision_does_not_match_loose_file(self):
        downloads = Path("/data/downloads/complete")
        torrents = [
            {"hash": "loose", "name": "release.mkv", "content_path": str(downloads / "release.mkv")},
            {
                "hash": "nested",
                "name": "release.mkv",
                "content_path": str(downloads / "Other" / "release.mkv"),
            },
        ]
        by_name, by_top = download_cleanup.torrent_maps(torrents, downloads)
        matches = download_cleanup.matched_torrents("release.mkv", by_name, by_top)
        self.assertEqual({"loose"}, {torrent["hash"] for torrent in matches})

    def test_out_of_scope_basename_is_never_matched(self):
        downloads = Path("/data/downloads/complete")
        torrents = [
            {"hash": "outside", "name": "release.mkv", "content_path": "/data/downloads/incomplete/release.mkv"}
        ]
        by_name, by_top = download_cleanup.torrent_maps(torrents, downloads)
        self.assertEqual([], download_cleanup.matched_torrents("release.mkv", by_name, by_top))


if __name__ == "__main__":
    unittest.main()
