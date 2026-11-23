import contextlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import tempfile
import time
import types
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('receipt_cleanup', HERE / 'cleanup.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
client_module = c.load_client(str(Path(__file__).resolve().parents[1] / 'memory-coordinator/client.py'))


class FakeClient(client_module.Client):
    def __init__(self, responses, url='http://127.0.0.1:8766'):
        super().__init__(url, '/not/read')
        self.responses = list(responses)
        self.calls = []

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if not self.responses:
            raise AssertionError('unexpected API call')
        answer = self.responses.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)
        self.parent.chmod(0o700)
        self.path = self.parent / 'receipt.json'
        self.config = dict(c.ROLES['native'], receipt=str(self.path), uid=os.geteuid())
        self.request = 'owned-request-1234'

    def receipt(self, phase='stopped'):
        r = client_module.RunReceipt(self.path)
        r.lock()
        r.begin(self.config['url'], self.request)
        r.save(phase, lease_id='lease-123')
        r.close()

    def terminal(self):
        return {'request_id': self.request, 'state': 'cancelled'}

    def reconcile(self, client):
        return c.reconcile(self.config, client_module, client)

    def test_no_next_job_needed_to_retry_stopped_ack(self):
        self.receipt()
        before = self.path.read_bytes()
        failed = FakeClient([client_module.AdmissionError('transport')])
        self.assertEqual(self.reconcile(failed)['outcome'], 'retry_pending')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([x[0] for x in failed.calls], ['POST'])
        recovered = FakeClient([self.terminal()])
        self.assertEqual(self.reconcile(recovered)['outcome'], 'acknowledged')
        self.assertFalse(self.path.exists())
        self.assertEqual(recovered.calls[0][1], '/v1/requests/' + self.request + '/cancel')
        self.assertEqual(recovered.calls[0][2], {'lease_id': 'lease-123', 'stopped': True})

    def test_never_started_exact_unknown_request_is_terminal(self):
        self.receipt('not_started')
        client = FakeClient([client_module.AdmissionError('gone', status=404, server_error='unknown request')])
        self.assertEqual(self.reconcile(client)['outcome'], 'acknowledged')
        self.assertFalse(self.path.exists())
        self.assertEqual(len(client.calls), 1)

    def test_explicit_conflict_alone_reconciles_lost_grant(self):
        self.receipt('not_started')
        client = FakeClient([client_module.AdmissionError('conflict', status=409),
            {'request_id': self.request, 'state': 'blocked_orphan', 'lease_id': 'lease-recovered'}, self.terminal()])
        self.assertEqual(self.reconcile(client)['outcome'], 'acknowledged')
        self.assertEqual([x[0] for x in client.calls], ['POST', 'GET', 'POST'])
        self.assertEqual(client.calls[-1][2]['lease_id'], 'lease-recovered')

    def test_transport_auth_server_and_unknown_404_never_get(self):
        for status in (None, 401, 403, 404, 429, 500, 503):
            with self.subTest(status=status):
                self.receipt('not_started')
                before = self.path.read_bytes()
                client = FakeClient([client_module.AdmissionError('private text', status=status)])
                result = self.reconcile(client)
                self.assertEqual(result['outcome'], 'retry_pending')
                self.assertNotIn('private', json.dumps(result))
                self.assertEqual([x[0] for x in client.calls], ['POST'])
                self.assertEqual(before, self.path.read_bytes())

    def test_uncertain_work_does_not_even_inspect_status(self):
        self.receipt('may_have_started')
        before = self.path.read_bytes()
        client = FakeClient([])
        self.assertEqual(self.reconcile(client)['outcome'], 'uncertain_skipped')
        self.assertEqual(client.calls, [])
        self.assertEqual(before, self.path.read_bytes())

    def test_busy_receipt_is_not_touched(self):
        self.receipt()
        owner = client_module.RunReceipt(self.path)
        owner.lock()
        self.addCleanup(owner.close)
        client = FakeClient([])
        self.assertEqual(self.reconcile(client)['outcome'], 'busy_or_unsafe')
        self.assertEqual(client.calls, [])
        self.assertTrue(self.path.exists())

    def test_absent_receipt_creates_nothing(self):
        client = FakeClient([])
        self.assertEqual(self.reconcile(client)['outcome'], 'absent')
        self.assertEqual(list(self.parent.iterdir()), [])
        self.assertEqual(client.calls, [])

    def test_invalid_unknown_and_foreign_receipts_preserved(self):
        for contents in ('{', 'null', '{}', json.dumps({'version': 1, 'phase': 'unknown'})):
            self.path.write_text(contents)
            self.path.chmod(0o600)
            client = FakeClient([])
            self.assertEqual(self.reconcile(client)['outcome'], 'invalid_receipt')
            self.assertEqual(self.path.read_text(), contents)
            self.assertEqual(client.calls, [])
        self.receipt()
        value = json.loads(self.path.read_text())
        value['url'] = 'http://another-coordinator:8766'
        self.path.write_text(json.dumps(value))
        self.assertEqual(self.reconcile(FakeClient([]))['outcome'], 'coordinator_mismatch')

    def test_wrong_uid_and_symlink_parent_refused(self):
        self.receipt()
        with mock.patch.object(os, 'geteuid', return_value=self.config['uid'] + 1):
            self.assertEqual(self.reconcile(FakeClient([]))['outcome'], 'wrong_uid')
        link = self.parent / 'link'
        link.symlink_to(self.parent, target_is_directory=True)
        config = dict(self.config, receipt=str(link / self.path.name))
        self.assertEqual(c.reconcile(config, client_module, FakeClient([]))['outcome'], 'busy_or_unsafe')

    def test_bad_ack_identity_retains_receipt(self):
        self.receipt()
        client = FakeClient([{'request_id': 'foreign-request-1234', 'state': 'cancelled'}])
        self.assertEqual(self.reconcile(client)['outcome'], 'retry_pending')
        self.assertTrue(self.path.exists())

    def test_deadline_releases_lock_but_retains_proof(self):
        self.receipt()
        client = FakeClient([c.Deadline()])
        with self.assertRaises(c.Deadline):
            self.reconcile(client)
        self.assertTrue(self.path.exists())
        owner = client_module.RunReceipt(self.path)
        owner.lock()
        owner.close()

    def test_docker_exec_uses_captured_id_and_fixed_uid(self):
        calls = []
        cid = 'a' * 64
        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            if 'context' in argv:
                return c.DOCKER_ENDPOINT
            if 'inspect' in argv:
                return dict(id=cid, running=True, paused=False, name='/algoforge-worker-1')
            return {'outcome': 'absent'}
        self.assertEqual(c.pass_role('algo', 'trusted source', runner)['outcome'], 'absent')
        self.assertIn(cid, calls[2][0])
        self.assertNotIn('algoforge-worker-1', calls[2][0])
        self.assertEqual(calls[2][0][calls[2][0].index('--user') + 1], '10001')
        self.assertEqual(calls[2][1]['stdin'], 'trusted source')
        self.assertEqual(calls[0][0][:4], [c.DOCKER, 'context', 'inspect', 'desktop-linux'])
        for argv, _ in calls[1:]:
            self.assertEqual(argv[:3], [c.DOCKER, '--host', c.DOCKER_ENDPOINT])
        self.assertIn('-I', calls[2][0])

    def test_stopped_paused_or_mismatched_container_never_executes(self):
        for meta in (dict(running=False, paused=False), dict(running=True, paused=True), dict(running=True, paused=False, name='/wrong')):
            calls = []
            def runner(argv, **kwargs):
                calls.append(argv)
                if 'context' in argv:
                    return c.DOCKER_ENDPOINT
                return dict(dict(id='a' * 64, name='/trailer-processor'), **meta)
            c.pass_role('trailer', 'source', runner)
            self.assertEqual(len(calls), 2)

    def test_repointed_context_never_inspects_or_executes_container(self):
        for endpoint in ('ssh://remote', 'tcp://127.0.0.1:2375', 'unix:///another/socket', None):
            calls = []
            def runner(argv, **kwargs):
                calls.append(argv)
                return endpoint
            self.assertEqual(c.pass_role('algo', 'source', runner)['outcome'], 'docker_endpoint_mismatch')
            self.assertEqual(len(calls), 1)
            self.assertIn('context', calls[0])

    def test_docker_child_environment_strips_all_override_prefixes(self):
        overrides = {'DOCKER_HOST': 'ssh://remote', 'DOCKER_CONTEXT': 'foreign',
                     'DOCKER_CONFIG': '/foreign/config', 'DOCKER_TLS_VERIFY': '1',
                     'BUILDKIT_HOST': 'tcp://remote', 'BUILDKIT_PROGRESS': 'plain',
                     'RECEIPT_TEST_BENIGN': 'retained'}
        result = types.SimpleNamespace(returncode=0, stdout='{}')
        with mock.patch.dict(os.environ, overrides), mock.patch.object(c.subprocess, 'run', return_value=result) as run:
            c.invoke([c.DOCKER, 'context', 'inspect', 'desktop-linux'])
        env = run.call_args.kwargs['env']
        self.assertFalse(any(key.startswith(('DOCKER_', 'BUILDKIT_')) for key in env))
        self.assertEqual(env['RECEIPT_TEST_BENIGN'], 'retained')

    def test_status_is_single_private_bounded_snapshot(self):
        value = {'roles': {'native': {'outcome': 'retry_pending'}}}
        for _ in range(20):
            c.save_status(self.parent, value)
        self.assertEqual([p.name for p in self.parent.iterdir()], ['latest.json'])
        self.assertEqual((self.parent / 'latest.json').stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            c.save_status(self.parent, {'unbounded': 'x' * 9000})
        self.assertEqual(json.loads((self.parent / 'latest.json').read_text()), value)

    def test_reviewed_client_bytes_required(self):
        path = self.parent / 'client.py'
        path.write_text('raise Exception("must never run")')
        path.chmod(0o600)
        with self.assertRaises(c.UnreviewedClient):
            c.load_client(path)

    def test_real_helper_alarm_preserves_receipt_and_unlocks(self):
        self.receipt()
        before = self.path.read_bytes()
        class BlockingClient(FakeClient):
            def call(self, *_):
                time.sleep(2)
                raise AssertionError('alarm should interrupt this wait')
        module = types.SimpleNamespace(RunReceipt=client_module.RunReceipt,
                    AdmissionError=client_module.AdmissionError, Client=lambda *_a, **_k: BlockingClient([]))
        with mock.patch.dict(c.ROLES, native=self.config), mock.patch.object(c, 'load_client', return_value=module), mock.patch.object(c, 'HELPER_SECONDS', .025):
            self.assertEqual(c.helper('native')['outcome'], 'deadline_retry_pending')
        self.assertEqual(self.path.read_bytes(), before)
        owner = client_module.RunReceipt(self.path)
        owner.lock()
        owner.close()

    def test_overlapping_controller_pass_runs_no_helpers(self):
        fd = os.open(self.parent / 'controller.lock', os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        c.fcntl.flock(fd, c.fcntl.LOCK_EX | c.fcntl.LOCK_NB)
        with mock.patch.object(c, 'pass_role') as run:
            self.assertEqual(c.run_pass(directory=self.parent), 0)
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
