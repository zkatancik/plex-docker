#!/usr/bin/env python3
"""Vendorable stdlib admission client and supervised-process CLI.

The server only grants permission. Clients must stop actual work before release.
"""
import argparse
import hashlib
import hmac
import itertools
import json
import os
import pathlib
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class AdmissionError(RuntimeError):
    pass


class Client:
    def __init__(self, url, token_file, timeout=8):
        self.url = url.rstrip("/")
        self.token_file = pathlib.Path(token_file)
        self.timeout = timeout

    def call(self, method, path, body=None):
        try:
            token = self.token_file.read_text().strip()
        except OSError:
            raise AdmissionError("admission token file unavailable") from None
        if self.url.startswith("file://"):
            return self._file_call(method, path, body, token)
        request = urllib.request.Request(self.url + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        # Do not use environment HTTP proxies for local/host.docker.internal control traffic.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=self.timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read(16384)).get("error", "admission rejected")
            except Exception:
                message = "admission rejected"
            finally:
                exc.close()
            raise AdmissionError(f"HTTP {exc.code}: {message}") from None
        except (OSError, ValueError) as exc:
            raise AdmissionError("admission connection or response unavailable") from exc
        if not isinstance(result, dict):
            raise AdmissionError("invalid admission response")
        return result

    def _file_call(self, method, path, body, token):
        parsed = urllib.parse.urlparse(self.url)
        if parsed.netloc not in {"", "localhost"}:
            raise AdmissionError("invalid admission bridge URL")
        directory = pathlib.Path(urllib.parse.unquote(parsed.path))
        nonce = uuid.uuid4().hex
        request_path, response_path = directory / f"{nonce}.request.json", directory / f"{nonce}.response.json"
        def encoded(value):
            return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        payload = dict(nonce=nonce, method=method, path=path, body=body, created_at=time.time())
        message = dict(payload=payload, signature=hmac.new(token.encode(), encoded(payload), hashlib.sha256).hexdigest())
        temporary = None
        try:
            # Bound files left by this transport without opening arbitrary entries.
            if sum(1 for _ in itertools.islice(directory.iterdir(), 128)) >= 128:
                raise AdmissionError("admission bridge busy")
            fd, temporary = tempfile.mkstemp(prefix=".bridge-", dir=directory)
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o644)
                stream.write(encoded(message))
            os.replace(temporary, request_path)
            end = time.monotonic() + self.timeout
            while time.monotonic() < end:
                try:
                    fd = os.open(response_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, "rb") as stream:
                        info = os.fstat(stream.fileno())
                        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                            raise AdmissionError("invalid bridge response")
                        response = json.load(stream)
                    value = response["payload"]
                    expected = hmac.new(token.encode(), encoded(value), hashlib.sha256).hexdigest()
                    if not hmac.compare_digest(response["signature"], expected) or value["nonce"] != nonce:
                        raise AdmissionError("bridge response authentication failed")
                    if value["status"] != 200:
                        raise AdmissionError(f"HTTP {value['status']}: {value['body'].get('error', 'admission rejected')}")
                    return value["body"]
                except FileNotFoundError:
                    time.sleep(0.05)
            raise AdmissionError("admission bridge unavailable")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise AdmissionError("admission bridge unavailable or invalid") from exc
        finally:
            for candidate in (request_path, response_path, pathlib.Path(temporary) if temporary else None):
                if candidate is not None:
                    try:
                        candidate.unlink(missing_ok=True)
                    except OSError:
                        pass

    def acquire(self, job, peak_mib, run_timeout_s, *, guest=True, wait_timeout_s=86400, cancel=None):
        request_id = str(uuid.uuid4())
        endpoint = "/v1/requests/" + request_id
        body = dict(request_id=request_id, job=job, peak_mib=peak_mib, run_timeout_s=run_timeout_s,
                    guest=guest, wait_timeout_s=wait_timeout_s)
        result = None
        deadline = time.monotonic() + wait_timeout_s
        try:
            result = self.call("POST", "/v1/requests", body)
            while True:
                if cancel is not None and cancel.is_set():
                    raise AdmissionError("admission wait cancelled")
                if result.get("state") == "granted":
                    return Lease(self, result)
                if result.get("state") != "queued":
                    raise AdmissionError("admission did not grant work: " + str(result.get("state")))
                if time.monotonic() >= deadline:
                    raise AdmissionError("admission wait deadline reached")
                if cancel is not None:
                    cancel.wait(2)
                else:
                    time.sleep(2)
                result = self.call("GET", endpoint)
        except BaseException:
            # A lost response may conceal a grant; retrieve identity before cancellation.
            try:
                result = self.call("GET", endpoint)
                self.call("POST", endpoint + "/cancel", {"lease_id": result.get("lease_id"), "stopped": True})
            except Exception:
                pass  # Never start work; an unreachable grant remains safely blocked.
            raise


class Lease:
    def __init__(self, client, info):
        self.client, self.info = client, info
        self.endpoint = "/v1/requests/" + info["request_id"]
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    def start_heartbeat(self):
        if self._thread is not None:
            raise AdmissionError("heartbeat already started")

        def beat():
            while not self._stop.wait(self.info["heartbeat_interval_s"]):
                try:
                    value = self.client.call("POST", self.endpoint + "/heartbeat", {"lease_id": self.info["lease_id"]})
                    if value.get("state") != "granted":
                        raise AdmissionError("lease is no longer granted")
                except Exception:
                    self.lost.set()
                    return
        self._thread = threading.Thread(target=beat, daemon=True)
        self._thread.start()

    def release(self, *, stopped):
        if stopped is not True:
            raise AdmissionError("release requires actual work to have stopped")
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.client.timeout + 1)
            if self._thread.is_alive():
                raise AdmissionError("heartbeat still in flight; retain lease for explicit resolution")
        return self.client.call("POST", self.endpoint + "/release", {"lease_id": self.info["lease_id"], "stopped": True})


def process_group_exists(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def stop_group(proc, grace=10):
    """Wait for the owned process group, not just its parent, before freeing slot."""
    for sig, seconds in [(signal.SIGTERM, grace), (signal.SIGKILL, 5)]:
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            proc.poll()  # Reap parent; orphan zombies can still keep the group present.
            if not process_group_exists(proc.pid):
                return True
            time.sleep(0.1)
    return not process_group_exists(proc.pid)


def write_status(path, value):
    if path is None:
        return
    path = pathlib.Path(path)
    fd, name = tempfile.mkstemp(prefix=".batch-status-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def run_command(client, args):
    cancelled = threading.Event()
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, lambda *_: cancelled.set())
    lease, proc, stopped, started = None, None, True, None
    status_path = getattr(args, "status_file", None)
    try:
        lease = client.acquire(args.job, args.peak_mib, args.run_timeout, guest=not args.native,
                               wait_timeout_s=args.wait_timeout, cancel=cancelled)
        if cancelled.is_set():
            return 130
        lease.start_heartbeat()
        deadline = time.monotonic() + max(0, lease.info["run_deadline"] - time.time())
        if time.monotonic() >= deadline or lease.lost.is_set():
            return 124
        started = time.monotonic()
        write_status(status_path, {"state": "running", "started_monotonic": started})
        proc = subprocess.Popen(args.command, start_new_session=True)
        stopped = False
        while proc.poll() is None:
            if cancelled.is_set() or lease.lost.is_set() or time.monotonic() >= deadline:
                stopped = stop_group(proc)
                return 124 if not cancelled.is_set() else 130
            time.sleep(0.2)
        result = proc.returncode
        stopped = not process_group_exists(proc.pid)
        if not stopped:
            stopped = stop_group(proc)
        return result if result >= 0 else 128 - result
    finally:
        if proc is not None and not stopped:
            stopped = stop_group(proc)
        if started is not None:
            try:
                write_status(status_path, {"state": "stopped" if stopped else "unconfirmed",
                    "started_monotonic": started, "elapsed_seconds": time.monotonic() - started})
            except OSError:
                print("Could not write batch timing status.", file=sys.stderr)
        if lease is not None:
            if stopped:
                try:
                    lease.release(stopped=True)
                except Exception:
                    print("Work stopped; release unconfirmed. Coordinator retains slot for explicit resolution.", file=sys.stderr)
            else:
                print("Owned process group did not stop; lease retained. Explicit operator resolution required.", file=sys.stderr)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file", required=True)
    subs = parser.add_subparsers(dest="action", required=True)
    subs.add_parser("status")
    resolve = subs.add_parser("resolve", help="explicit operator acknowledgement after checking workload is stopped")
    resolve.add_argument("request_id")
    resolve.add_argument("--confirm-workload-stopped", action="store_true", required=True)
    run = subs.add_parser("run")
    run.add_argument("--job", required=True)
    run.add_argument("--peak-mib", required=True, type=int)
    run.add_argument("--run-timeout", required=True, type=int)
    run.add_argument("--wait-timeout", type=int, default=86400)
    run.add_argument("--native", action="store_true")
    run.add_argument("--status-file", type=pathlib.Path, help="optional credential-free timing metadata for a local supervisor")
    run.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    client = Client(args.url, args.token_file)
    try:
        if args.action == "status":
            print(json.dumps(client.call("GET", "/v1/status"), indent=2))
        elif args.action == "resolve":
            path = "/v1/requests/" + args.request_id
            info = client.call("GET", path)
            value = client.call("POST", path + "/cancel", {"lease_id": info.get("lease_id"), "stopped": True})
            print(json.dumps(value, indent=2))
        else:
            if args.command[:1] == ["--"]:
                args.command = args.command[1:]
            if not args.command:
                parser.error("run requires a command after --")
            return run_command(client, args)
    except AdmissionError as exc:
        print(str(exc), file=sys.stderr)
        return 75
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
