"""Admission must precede expensive decoding and preserve deferred inputs."""
import os
import contextlib
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'containers/agregarr/trailer-postprocessor.sh'


class TrailerMemoryTests(unittest.TestCase):
    def test_group_termination_preserves_source_and_retry_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'Trailer.mp4'
            source.write_bytes(b'last good trailer')
            retry = Path(str(source) + '.retry')
            retry.write_text('2\n')
            ready = root / 'decoder.ready'
            for name, output in [('stat', 'fixture-identity'), ('sleep', ''),
                                 ('ffprobe', ''), ('sha256sum', '')]:
                path = root / name
                path.write_text('#!/bin/sh\nprintf "%s\\n" "' + output + '"\n')
                path.chmod(0o755)
            decoder = root / 'ffmpeg'
            decoder.write_text('#!' + sys.executable + '\n'
                               'import os,time\nfrom pathlib import Path\n'
                               'Path(os.environ["DECODER_READY"]).touch()\n'
                               'time.sleep(10)\n')
            decoder.chmod(0o755)
            env = dict(os.environ, PATH=str(root) + ':' + os.environ['PATH'],
                       DECODER_READY=str(ready), MEMORY_COORDINATOR_URL='',
                       TRAILER_MEMORY_SLOT_HELD='1')
            process = subprocess.Popen(['sh', str(SCRIPT), str(source)], env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True)
            try:
                deadline = time.monotonic() + 3
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists())
                os.killpg(process.pid, signal.SIGTERM)
                process.communicate(timeout=3)
                self.assertTrue(source.exists(), 'cancellation must not request redownload')
                self.assertEqual(source.read_bytes(), b'last good trailer')
                self.assertEqual(retry.read_text(), '2\n')
                self.assertFalse(Path(str(source) + '.redownloads').exists())
                self.assertEqual(process.returncode, 143)
            finally:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=3)

    def run_script(self, validated=False):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'Trailer.mp4'
            source.write_bytes(b'fixture')
            for name in ['ffmpeg', 'ffprobe', 'sha256sum']:
                path = root / name
                path.write_text('#!/bin/sh\necho unexpected-heavy-work >&2\nexit 99\n')
                path.chmod(0o755)
            stat = root / 'stat'
            stat.write_text('#!/bin/sh\necho fixture-identity\n')
            stat.chmod(0o755)
            client = root / 'client.py'
            client.write_text('import sys\nprint("admission-requested")\nsys.exit(75)\n')
            if validated:
                Path(str(source) + '.processed').write_text('identity=fixture-identity\n')
            env = dict(os.environ, PATH=str(root) + ':' + os.environ['PATH'],
                       MEMORY_COORDINATOR_URL='http://127.0.0.1:1',
                       MEMORY_COORDINATOR_CLIENT=str(client))
            env.pop('TRAILER_MEMORY_SLOT_HELD', None)
            result = subprocess.run(['sh', str(SCRIPT), str(source)], env=env,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(source.read_bytes(), b'fixture')
            self.assertNotIn('unexpected-heavy-work', result.stderr)
            return result

    def test_deferred_job_preserves_source_without_decoding(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 75)
        self.assertIn('admission-requested', result.stdout)

    def test_validated_marker_does_not_queue_again(self):
        result = self.run_script(validated=True)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn('admission-requested', result.stdout)


if __name__ == '__main__':
    unittest.main()
