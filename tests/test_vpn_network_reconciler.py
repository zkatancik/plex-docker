import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


SCRIPT = Path(__file__).parents[1] / "containers/vpn-network-reconciler/reconcile.sh"


class ReconcilerTests(unittest.TestCase):
    def run_cycle(self, *, expect_success_marker=False, **overrides):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docker = root / "docker"
            docker.write_text(textwrap.dedent("""\
                #!/usr/bin/env python3
                import json, os, sys
                args = sys.argv[1:]
                with open(os.environ['CALLS'], 'a') as log:
                    log.write(json.dumps(args) + '\\n')
                if args[0] == 'inspect':
                    template, service = args[2], args[3]
                    if '.State.Status' in template:
                        print('running')
                    elif '.State.Health' in template:
                        if os.environ.get('UNHEALTHY') == service:
                            print('unhealthy')
                        elif os.environ.get('STARTING') == service:
                            print('starting')
                        else:
                            print('healthy')
                    elif '.HostConfig.NetworkMode' in template:
                        print(os.environ.get('NETWORK_MODE', 'container:same-id'))
                    elif '.Id' in template:
                        print('same-id')
                    else:
                        sys.exit('Unexpected inspect: ' + repr(args))
                elif args[0] == 'exec':
                    service = args[1]
                    assert args[2:] == ['readlink', '/proc/1/ns/net'], args
                    if os.environ.get('NAMESPACE_UNREADABLE') == service:
                        sys.exit(1)
                    elif service == 'gluetun':
                        print('net:[200]')
                    else:
                        print(os.environ.get('DEPENDENT_NAMESPACE', 'net:[200]'))
                elif args[0] == 'restart':
                    sys.exit(1)
                elif args[0] == 'compose' and os.environ.get('COMPOSE_FAIL'):
                    sys.exit(1)
                elif args[0] != 'compose':
                    sys.exit('Unexpected docker call: ' + repr(args))
                """))
            docker.chmod(0o755)
            calls = root / "calls.jsonl"
            success = root / "success"
            heartbeat = root / "heartbeat"
            result = subprocess.run(
                ["/bin/sh", str(SCRIPT), "--once"],
                env={**os.environ, "PATH": f"{root}:{os.environ['PATH']}",
                     "CALLS": str(calls), "SUCCESS_PATH": str(success),
                     "HEARTBEAT_PATH": str(heartbeat), **overrides},
                capture_output=True, text=True, timeout=10,
            )
            commands = [json.loads(line) for line in calls.read_text().splitlines()]
            repairs = [cmd for cmd in commands if "--force-recreate" in cmd]
            self.assertEqual(success.exists(), expect_success_marker,
                             "Health must reflect verified healthy dependents, not just a running loop")
            return result, repairs

    def test_same_container_id_but_old_live_namespace_is_repaired(self):
        result, repairs = self.run_cycle(DEPENDENT_NAMESPACE="net:[100]")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(repairs), 2, result.stdout)
        self.assertEqual(repairs[0][-2:], ["qbittorrent", "flaresolverr"])
        self.assertEqual(repairs[1][-1:], ["qb-port-sync"])

    def test_matching_live_namespaces_do_not_restart_downloads(self):
        result, repairs = self.run_cycle(expect_success_marker=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(repairs, [])

    def test_replaced_gluetun_id_is_still_repaired(self):
        result, repairs = self.run_cycle(NETWORK_MODE="container:old-id")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(repairs), 2)

    def test_unreadable_namespace_does_not_cause_blind_recreation(self):
        result, repairs = self.run_cycle(NAMESPACE_UNREADABLE="qbittorrent")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(repairs, [])

    def test_unreadable_gluetun_namespace_defers_repair(self):
        result, repairs = self.run_cycle(NAMESPACE_UNREADABLE="gluetun")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(repairs, [])

    def test_failed_recreation_does_not_restart_port_manager_or_report_success(self):
        result, repairs = self.run_cycle(DEPENDENT_NAMESPACE="net:[100]", COMPOSE_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0][-2:], ["qbittorrent", "flaresolverr"])

    def test_failed_gluetun_restart_stops_recovery(self):
        result, repairs = self.run_cycle(UNHEALTHY="gluetun")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(repairs, [])

    def test_starting_dependents_are_neither_healthy_nor_recreated(self):
        result, repairs = self.run_cycle(STARTING="qbittorrent")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(repairs, [])


if __name__ == "__main__":
    unittest.main()
