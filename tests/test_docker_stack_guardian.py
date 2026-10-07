"""Exercise guardian discovery without depending on readable Compose secrets."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/docker-stack-guardian.sh'


class GuardianDiscoveryTests(unittest.TestCase):
    def run_probe(self, failure=False, missing=False):
        source = SCRIPT.read_text()
        functions = source[source.index('service_state()'):source.index('failures=0')]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docker = root / 'docker'
            docker.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$CALLS"
case "$1" in
  compose) exit 1 ;;
  ps) [ "$FAIL" = 1 ] && exit 1
      [ "$MISSING" = 1 ] || echo container-id ;;
  inspect) echo running ;;
esac
''')
            docker.chmod(0o755)
            env = dict(os.environ, CALLS=str(root / 'calls'), FAIL=str(int(failure)), MISSING=str(int(missing)))
            code = 'DOCKER="$1"\n' + functions + '\nservice_state "hockey-standings-service" hockey-standings\n'
            result = subprocess.run(['/bin/zsh', '-c', code, 'test', str(docker)], env=env, capture_output=True, text=True, timeout=5)
            return result.stdout.strip(), (root / 'calls').read_text()

    def test_running_service_does_not_require_readable_compose_configuration(self):
        state, calls = self.run_probe()
        self.assertEqual(state, 'running')
        self.assertNotIn('compose ', calls)
        self.assertIn('com.docker.compose.project=hockey-standings-service', calls)
        self.assertIn('com.docker.compose.service=hockey-standings', calls)

    def test_failed_daemon_query_is_unknown_instead_of_missing(self):
        state, _ = self.run_probe(failure=True)
        self.assertEqual(state, 'unknown')

    def test_successful_empty_query_identifies_missing_service(self):
        state, _ = self.run_probe(missing=True)
        self.assertEqual(state, 'missing')


    def test_unknown_query_defers_recovery_even_if_another_service_is_missing(self):
        source = SCRIPT.read_text()
        functions = source[source.index('recover_stack()'):source.index('failures=0')]
        with tempfile.TemporaryDirectory() as directory:
            compose = Path(directory) / 'compose.yaml'
            compose.touch()
            code = """stack_disabled() { return 1; }
log() { echo "$*"; }
service_state() { if [[ "$2" == missing-service ]]; then echo missing; else echo unknown; fi; }
DOCKER=/must-not-run-recovery
""" + functions + '\nrecover_stack test "$1" "$2" "" test-project missing-service unknown-service\n'
            result = subprocess.run(['/bin/zsh', '-c', code, 'test', directory, str(compose)], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertIn('deferring recovery', result.stdout)
            self.assertNotIn('recovery required', result.stdout)
            self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
