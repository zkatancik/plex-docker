"""A killed guardian must not exclude its successor."""
from pathlib import Path
import subprocess
import tempfile
import unittest


class GuardianLockTests(unittest.TestCase):
    def test_process_death_releases_lock(self):
        script = Path(__file__).resolve().parents[1] / "scripts/docker-stack-guardian.sh"
        source = script.read_text()
        start = source.index("zmodload zsh/system")
        end = source.index("\nfi", source.index("if ! zsystem flock", start)) + 3
        body = 'LOCK_FILE="$1"; log() { :; }\n' + source[start:end]
        with tempfile.TemporaryDirectory() as directory:
            command = ["/bin/zsh", "-c", body, "guardian", directory + "/lock"]
            holder = command.copy()
            holder[2] += '\necho ready; read -r unused'
            with subprocess.Popen(holder, stdin=subprocess.PIPE, stdout=subprocess.PIPE) as proc:
                try:
                    self.assertEqual(proc.stdout.readline(), b"ready\n")
                    self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
                finally:
                    proc.kill()
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
