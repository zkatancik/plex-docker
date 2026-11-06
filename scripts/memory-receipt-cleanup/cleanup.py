#!/usr/bin/python3
"""One bounded acknowledgement pass; never acquires admission or starts work."""
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
HOME = Path('/Users/zack')
INSTALL = HOME / '.config/home-memory-coordinator'
STATUS_DIR = INSTALL / 'receipt-cleanup'
CLIENT_SHA256 = '24c915e4522d8d78abb09f1eea36270664b03933590375a520aa2ede6274442d'
DOCKER = '/usr/local/bin/docker'
DOCKER_CONTEXT = 'desktop-linux'
DOCKER_ENDPOINT = 'unix:///Users/zack/.docker/run/docker.sock'
CALL_TIMEOUT = 2
HELPER_SECONDS = 9
PROCESS_SECONDS = 12
ROLES = {
    'native': dict(container=None, uid=501, python='/usr/bin/python3',
                   receipt=str(HOME / '.config/media-normalizer/memory-admission.json'),
                   client=str(INSTALL / 'client.py'), token=str(INSTALL / 'token'),
                   url='http://127.0.0.1:8766'),
    'trailer': dict(container='trailer-processor', uid=0, python='python3',
                    receipt='/app/config/.memory-coordinator/trailer-processing.json',
                    client='/opt/memory-coordinator/client.py', token='/run/secrets/memory-coordinator-token',
                    url='http://host.docker.internal:8766'),
    'algo': dict(container='algoforge-worker-1', uid=10001, python='python',
                 receipt='/app/runtime/.memory-coordinator/research.json',
                 client='/opt/memory-coordinator/client.py', token='/run/secrets/memory-coordinator-token',
                 url='file:///run/memory-admission-bridge'),
    'nextcloud': dict(container='nc-cron', uid=33, python='python3',
                      receipt='/var/ncdata/.memory-coordinator/nextcloud-cron.json',
                      client='/opt/memory-coordinator/client.py', token='/run/secrets/memory-coordinator-token',
                      url='http://host.docker.internal:8766'),
}


class Deadline(Exception):
    pass


class UnreviewedClient(Exception):
    pass


@contextlib.contextmanager
def deadline(seconds):
    def expire(*_):
        raise Deadline()
    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def load_client(path):
    # Reuse only the reviewed installed bytes; never import a receipt-controlled path.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_size > 131072:
            raise ValueError('unsafe module')
        raw = stream.read(131073)
    if hashlib.sha256(raw).hexdigest() != CLIENT_SHA256:
        raise UnreviewedClient()
    # Execute the verified snapshot rather than re-opening a mutable mount path.
    spec = importlib.util.spec_from_loader('receipt_cleanup_client', loader=None)
    module = importlib.util.module_from_spec(spec)
    exec(compile(raw, str(path), 'exec'), module.__dict__)
    return module


def reconcile(spec, module, client=None):
    """No API operation until receipt ownership, phase and endpoint are proven."""
    if os.geteuid() != spec['uid']:
        return {'outcome': 'wrong_uid'}
    try:
        Path(spec['receipt']).lstat()
    except FileNotFoundError:
        return {'outcome': 'absent'}
    except OSError:
        return {'outcome': 'unsafe_or_unreadable'}
    receipt = module.RunReceipt(spec['receipt'])
    try:
        try:
            receipt.lock()
        except module.AdmissionError:
            # The client intentionally shares one error for busy and unsafe locks.
            return {'outcome': 'busy_or_unsafe'}
        try:
            value = receipt.read()
        except module.AdmissionError:
            return {'outcome': 'invalid_receipt'}
        if value is None:
            return {'outcome': 'absent'}
        if value['phase'] not in {'stopped', 'not_started'}:
            return {'outcome': 'uncertain_skipped', 'phase': value['phase']}
        if value['url'] != spec['url']:
            return {'outcome': 'coordinator_mismatch', 'phase': value['phase']}
        client = client or module.Client(spec['url'], spec['token'], timeout=CALL_TIMEOUT)
        if client.url != spec['url']:
            return {'outcome': 'coordinator_mismatch', 'phase': value['phase']}
        # recover re-reads under the held lock; typed409 alone permits an owner GET.
        receipt.recover(client)
        return {'outcome': 'acknowledged', 'phase': value['phase']}
    except module.AdmissionError:
        # Do not log exception text, raw HTTP bodies, URL, tokens or receipt IDs.
        return {'outcome': 'retry_pending'}
    finally:
        receipt.close()


def helper(role):
    spec = ROLES[role]
    try:
        with deadline(HELPER_SECONDS):
            # An absent receipt is quiet and needs neither a token nor module import.
            if os.geteuid() != spec['uid']:
                return {'outcome': 'wrong_uid'}
            try:
                Path(spec['receipt']).lstat()
            except FileNotFoundError:
                return {'outcome': 'absent'}
            return reconcile(spec, load_client(spec['client']))
    except Deadline:
        return {'outcome': 'deadline_retry_pending'}
    except UnreviewedClient:
        return {'outcome': 'client_unreviewed'}
    except Exception:
        return {'outcome': 'helper_unavailable'}


def invoke(argv, *, stdin=None, timeout=PROCESS_SECONDS):
    # Only this exact trusted helper emits stdout, always a small fixed JSON object.
    child_env = None
    if argv[0] == DOCKER:
        child_env = {key: value for key, value in os.environ.items()
                     if not key.startswith(('DOCKER_', 'BUILDKIT_'))}
    result = subprocess.run(argv, input=stdin, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=timeout, text=True, env=child_env)
    if result.returncode or len(result.stdout) > 4096:
        raise ValueError('helper invocation failed')
    return json.loads(result.stdout)


def pass_role(role, source, runner=invoke):
    spec = ROLES[role]
    try:
        if spec['container'] is None:
            result = runner([spec['python'], '-I', '-', '--helper', role], stdin=source)
        else:
            endpoint = runner([DOCKER, 'context', 'inspect', DOCKER_CONTEXT,
                               '--format', '{{json .Endpoints.docker.Host}}'], timeout=2)
            if endpoint != DOCKER_ENDPOINT:
                return {'outcome': 'docker_endpoint_mismatch'}
            # Pin the socket on subsequent commands too: a context edited between
            # verification and execution cannot redirect this pass to a remote engine.
            # Resolve once, then execute against the exact ID. No config/env dumps.
            meta = runner([DOCKER, '--host', DOCKER_ENDPOINT, 'inspect', '--format',
                           '{"id":{{json .Id}},"running":{{json .State.Running}},"paused":{{json .State.Paused}},"name":{{json .Name}}}',
                           spec['container']], timeout=2)
            cid = meta.get('id', '')
            if (len(cid) != 64 or any(c not in '0123456789abcdef' for c in cid)
                    or meta.get('name') != '/' + spec['container']):
                return {'outcome': 'container_identity_unconfirmed'}
            if meta.get('running') is not True or meta.get('paused') is not False:
                return {'outcome': 'container_unavailable'}
            result = runner([DOCKER, '--host', DOCKER_ENDPOINT, 'exec', '-i', '--user', str(spec['uid']), cid,
                             spec['python'], '-I', '-', '--helper', role], stdin=source)
        if (not isinstance(result, dict) or set(result) - {'outcome', 'phase'}
                or result.get('outcome') not in OUTCOMES
                or ('phase' in result and result['phase'] not in {'not_started', 'stopped', 'may_have_started'})):
            return {'outcome': 'invalid_helper_result'}
        return result
    except subprocess.TimeoutExpired:
        # A Docker helper has its own shorter alarm; CLI timeout is not death proof.
        return {'outcome': 'invocation_timeout'}
    except Exception:
        return {'outcome': 'invocation_unavailable'}


OUTCOMES = {'absent', 'wrong_uid', 'unsafe_or_unreadable', 'busy_or_unsafe',
            'invalid_receipt', 'uncertain_skipped', 'coordinator_mismatch',
            'acknowledged', 'retry_pending', 'deadline_retry_pending', 'helper_unavailable', 'client_unreviewed'}


def private_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('private status directory required')


def save_status(directory, value):
    # One bounded private latest snapshot; no history/log growth and no receipt IDs.
    private_directory(directory)
    raw = (json.dumps(value, sort_keys=True) + '\n').encode()
    if len(raw) > 8192:
        raise ValueError('status exceeds bound')
    fd, temporary = tempfile.mkstemp(prefix='.latest-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / 'latest.json')
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def run_pass(*, nextcloud=False, directory=STATUS_DIR):
    private_directory(directory)
    fd = os.open(directory / 'controller.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError('unsafe controller lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        source = Path(__file__).read_text()
        started = time.monotonic()
        roles = ['native', 'trailer', 'algo'] + (['nextcloud'] if nextcloud else [])
        results = {role: pass_role(role, source) for role in roles}
        save_status(directory, {'schema_version': 1,
                    'finished_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    'elapsed_s': round(time.monotonic() - started, 3), 'roles': results})
        return 0
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--helper', choices=tuple(ROLES))
    parser.add_argument('--nextcloud', action='store_true')
    args = parser.parse_args()
    if args.helper:
        print(json.dumps(helper(args.helper)))
        return 0
    return run_pass(nextcloud=args.nextcloud)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        # LaunchAgent stdout/stderr remain quiet; nonzero status exposes activation issues.
        sys.exit(1)
