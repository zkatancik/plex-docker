"""Synthetic watcher shutdown checks; no provider, media, Docker or real roots."""
import contextlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'containers/agregarr/trailer-watcher.sh'
STUB = r'''
import os, pathlib, shutil, sys, time
name = pathlib.Path(sys.argv[0]).name
root = pathlib.Path(os.environ['WATCHER_FIXTURE'])
if name == 'mktemp':
    path = root / 'probe-source'
    path.write_bytes(b'')
    print(path)
elif name == 'dd':
    target = next(a[3:] for a in sys.argv[1:] if a.startswith('of='))
    pathlib.Path(target).write_bytes(b'synthetic storage probe bytes')
elif name == 'cp':
    shutil.copyfile(sys.argv[1], sys.argv[2])
    (root / 'probe-ready').touch()
    if os.environ['WATCHER_PHASE'] == 'probe':
        time.sleep(.15)
elif name == 'cmp':
    raise SystemExit(0 if pathlib.Path(sys.argv[-2]).read_bytes() == pathlib.Path(sys.argv[-1]).read_bytes() else 1)
elif name == 'sleep':
    (root / 'idle-ready').touch()
    time.sleep(.05)
elif name in ('sync', 'find'):
    pass
else:
    raise SystemExit('unexpected fixture command')
'''


class TrailerWatcherSignals(unittest.TestCase):
    def exercise(self, sent_signal, expected_status, phase):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            binary = root / 'bin'
            binary.mkdir()
            movies, tv = root / 'movies', root / 'tv'
            movies.mkdir()
            tv.mkdir()
            for command in ('mktemp', 'dd', 'cp', 'cmp', 'sync', 'find', 'sleep'):
                path = binary / command
                path.write_text('#!' + sys.executable + '\n' + STUB)
                path.chmod(0o755)
            (binary / 'rm').symlink_to('/bin/rm')
            fixture_script = root / 'watcher.sh'
            source = SCRIPT.read_text()
            source = source.replace('MOVIES_DIR="/data/movies"', 'MOVIES_DIR="' + str(movies) + '"')
            source = source.replace('TV_DIR="/data/tv"', 'TV_DIR="' + str(tv) + '"')
            fixture_script.write_text(source)
            env = dict(os.environ, PATH=str(binary), WATCHER_FIXTURE=str(root), WATCHER_PHASE=phase)
            process = subprocess.Popen(['/bin/sh', str(fixture_script)], env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True)
            try:
                ready = root / ('probe-ready' if phase == 'probe' else 'idle-ready')
                deadline = time.monotonic() + 3
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists(), 'fixture watcher never reached requested phase')
                # Match tini's parent-only forwarding. Mocked children return
                # shortly; a real foreground child may defer this shell trap.
                process.send_signal(sent_signal)
                stdout, stderr = process.communicate(timeout=3)
                self.assertEqual(process.returncode, expected_status, (stdout, stderr))
                self.assertFalse((root / 'probe-source').exists())
                self.assertEqual(list(movies.glob('.trailer-storage-probe.*')), [])
                self.assertEqual(list(tv.glob('.trailer-storage-probe.*')), [])
            finally:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=3)

    def test_idle_signal_exits_instead_of_resuming_watch_loop(self):
        for sent_signal, expected in ((signal.SIGTERM, 143), (signal.SIGINT, 130), (signal.SIGHUP, 129)):
            with self.subTest(signal=sent_signal):
                self.exercise(sent_signal, expected, 'idle')

    def test_probe_interruption_exits_and_removes_both_probe_files(self):
        for sent_signal, expected in ((signal.SIGTERM, 143), (signal.SIGINT, 130), (signal.SIGHUP, 129)):
            with self.subTest(signal=sent_signal):
                self.exercise(sent_signal, expected, 'probe')


if __name__ == '__main__':
    unittest.main()
