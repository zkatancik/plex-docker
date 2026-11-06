"""Exercise durable poll continuation with real state/locks and no media/API calls."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_media_normalizer as fixtures
from test_media_normalizer_admission import result_fixture

m = fixtures.media_normalizer


class DeferredPollTests(unittest.TestCase):
    def run_resume_scenario(self, change=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            settings = fixtures.settings(root)
            paths = [root / (name + '.mkv') for name in ('A', 'B', 'C')]
            for path in paths:
                path.write_bytes(b'unchanged source')
            items = [m.ManagedMedia('sonarr', 1, index, path, '2026-01-01T00:00:00Z')
                     for index, path in enumerate(paths)]
            state = m.empty_state()
            old_watermarks = {'sonarr': '2020-01-01T00:00:00Z'}
            state['watermarks'] = dict(old_watermarks)
            m.atomic_write_json(settings.state_path, state)
            calls = []
            def inspect(path, _settings):
                calls.append(path)
                if path == paths[1] and calls.count(path) == 1:
                    raise m.TransientMediaProcessError('capacity wait', status='capacity_deferred')
                return {'decision': 'compatible', 'file_identity': m.current_file_identity(path)}
            real_item = m.process_poll_item
            def process_item(item, *args, **kwargs):
                result = real_item(item, *args, **kwargs)
                if change == 'replace_before_checkpoint' and item.path == paths[0] and calls.count(item.path) == 1:
                    replacement = root / 'replacement'
                    replacement.write_bytes(b'replaced source')
                    replacement.replace(item.path)
                if change == 'edit_before_checkpoint' and item.path == paths[0] and calls.count(item.path) == 1:
                    before = item.path.stat()
                    item.path.write_bytes(b'x' * before.st_size)
                    os.utime(item.path, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000000))
                return result
            with mock.patch.object(m, 'process_poll_item', side_effect=process_item), \
                    mock.patch.object(m, 'arr_clients', return_value={'sonarr': mock.Mock()}), \
                    mock.patch.object(m, 'all_managed', return_value=items), \
                    mock.patch.object(m, 'inspect_path', side_effect=inspect), \
                    mock.patch.object(m, 'load_settings', return_value=settings), \
                    mock.patch.object(m, 'log'), \
                    mock.patch.dict(os.environ, {'MEDIA_NORMALIZER_MEMORY_COORDINATOR_URL': 'http://fixture.invalid'}):
                self.assertEqual(m._main(['poll']), 75)
                saved = m.load_state(settings.state_path)
                self.assertEqual(saved['watermarks'], old_watermarks)
                self.assertEqual(set(saved['pending_poll']['completed']), set() if change in ('replace_before_checkpoint', 'edit_before_checkpoint') else {str(paths[0])})
                self.assertEqual(calls, paths[:2])
                self.assertFalse(settings.media_mutation_lock_path.exists())
                if change == 'same_size_edit':
                    before = paths[0].stat()
                    paths[0].write_bytes(b'x' * before.st_size)
                    os.utime(paths[0], ns=(before.st_atime_ns, before.st_mtime_ns + 1000000000))
                self.assertEqual(m._main(['retry-deferred']), 0)
            self.assertEqual(calls, [paths[0], paths[1]] + ([paths[0]] if change else []) + [paths[1], paths[2]])
            final = m.load_state(settings.state_path)
            self.assertNotIn('pending_poll', final)
            self.assertNotEqual(final['watermarks'], old_watermarks)
            self.assertTrue(all(final['files'][str(path)]['status'] == 'compatible' for path in paths))
            self.assertFalse(settings.media_mutation_lock_path.exists())

    def test_completed_import_is_not_replayed_before_deferred_and_unvisited_imports(self):
        self.run_resume_scenario()

    def test_replacement_before_checkpoint_is_not_marked_completed(self):
        self.run_resume_scenario('replace_before_checkpoint')

    def test_same_size_edit_before_checkpoint_is_not_marked_completed(self):
        self.run_resume_scenario('edit_before_checkpoint')

    def test_same_size_edit_between_retries_is_revisited(self):
        self.run_resume_scenario('same_size_edit')

    def test_idle_retry_does_not_contact_services_or_take_media_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = fixtures.settings(Path(directory))
            with mock.patch.object(m, 'load_settings', return_value=settings), \
                    mock.patch.object(m, 'arr_clients') as api, \
                    mock.patch.object(m, 'media_mutation_lock') as lock:
                self.assertEqual(m._main(['retry-deferred']), 0)
            api.assert_not_called()
            lock.assert_not_called()
            self.assertFalse(settings.state_path.exists())

    def test_retry_requires_admission_and_preserves_intent_without_it(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = fixtures.settings(Path(directory))
            state = m.empty_state()
            state['pending_poll'] = {'version': 1, 'completed': {}}
            m.atomic_write_json(settings.state_path, state)
            before = settings.state_path.read_bytes()
            with mock.patch.object(m, 'load_settings', return_value=settings), \
                    mock.patch.object(m, 'run_poll') as poll, mock.patch.object(m, 'log'), \
                    mock.patch.dict(os.environ, {'MEDIA_NORMALIZER_MEMORY_COORDINATOR_URL': ''}):
                self.assertEqual(m._main(['retry-deferred']), 1)
            poll.assert_not_called()
            self.assertEqual(settings.state_path.read_bytes(), before)

    def test_legacy_day_long_wait_is_capped_without_reducing_encode_deadline(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {'MEDIA_NORMALIZER_MEMORY_WAIT_SECONDS': '86400'}):
            with result_fixture(directory, 0) as launch:
                m.run_process(['ffmpeg', '-f', 'null', '-'], timeout=86400)
            command = launch.call_args.args[0]
            self.assertEqual(command[command.index('--wait-timeout') + 1], '60')
            self.assertEqual(command[command.index('--run-timeout') + 1], '86400')

    def test_bad_resume_record_is_not_silently_accepted(self):
        for value in (True, False, {}, {'version': 1, 'completed': []},
                      {'version': 1, 'completed': {'file': None}}):
            state = m.empty_state()
            state['pending_poll'] = value
            self.assertFalse(m.valid_state(state))


if __name__ == '__main__':
    unittest.main()
