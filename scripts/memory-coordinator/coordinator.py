#!/usr/bin/env python3
"""Small, fail-closed admission service. It never executes or kills client jobs."""
import argparse
import contextlib
import fcntl
import hmac
import json
import math
import os
import pathlib
import re
import secrets
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class APIError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def number(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum:
        raise APIError(400, f"invalid {name}")
    return value


class StateFile:
    def __init__(self, path):
        self.path = pathlib.Path(path)

    def load(self):
        return json.loads(self.path.read_text())  # Corruption deliberately prevents startup.

    def save(self, state):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".admission-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(state, stream, separators=(",", ":"), allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)


class HealthProbe:
    """Cached, bounded read-only probes; absence of evidence never grants a slot."""
    def __init__(self, config, clock=time.time, runner=subprocess.check_output):
        self.config, self.clock, self.runner = config, clock, runner
        self.host_cache = self.guest_cache = None
        self.last_vm = None

    def _run(self, args):
        return self.runner(args, text=True, stderr=subprocess.DEVNULL, timeout=2).strip()

    def _maintenance_active(self, now):
        path = self.config.get("maintenance_file")
        if not path:
            return False
        try:
            with pathlib.Path(path).open() as stream:
                value = stream.read(129)
            if len(value) > 128:
                return True
            deadline = float(value.strip())
            if not math.isfinite(deadline) or deadline <= 0:
                return True
            return deadline > now
        except FileNotFoundError:
            return False
        except (OSError, ValueError, UnicodeError):
            return True

    def get(self):
        now = self.clock()
        if self.host_cache is None or now - self.host_cache[0] >= 5 or now < self.host_cache[0]:
            try:
                level = int(self._run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"]))
                vm = self._run(["/usr/bin/vm_stat"])
                page_size = int(re.search(r"page size of (\d+)", vm)[1])
                counts = [int(re.search(r"^" + key + r":\s+(\d+)\.", vm, re.M)[1]) for key in ("Swapins", "Swapouts")]
                rates = [None, None]
                if self.last_vm is not None:
                    before, old, old_page_size = self.last_vm
                    if now > before and page_size == old_page_size and all(a >= b for a, b in zip(counts, old)):
                        rates = [(a - b) * page_size / 2**20 / (now - before) for a, b in zip(counts, old)]
                self.last_vm = (now, counts, page_size)
                self.host_cache = (now, {"pressure_level": level, "swap_in_mib_s": rates[0], "swap_out_mib_s": rates[1]})
            except Exception:
                self.last_vm = None
                self.host_cache = (now, {"pressure_level": None, "swap_in_mib_s": None, "swap_out_mib_s": None})
        if self.guest_cache is None or now - self.guest_cache[0] >= 15 or now < self.guest_cache[0]:
            try:
                container = self.config.get("probe_container")
                if not container:
                    raise ValueError("guest probe not configured")
                raw = self._run([self.config.get("docker_path", "/usr/local/bin/docker"),
                                 "exec", container, "cat", "/proc/meminfo"])
                match = re.search(r"^MemAvailable:\s+(\d+)\s+kB$", raw, re.M)
                self.guest_cache = (now, {"guest_available_mib": int(match[1]) / 1024})
            except Exception:
                self.guest_cache = (now, {"guest_available_mib": None})
        # Check the maintenance fence on every admission read, independently of
        # cached pressure/guest probes, so a freshly written hold takes effect now.
        return {**self.host_cache[1], **self.guest_cache[1], "maintenance_active": self._maintenance_active(now)}


class Coordinator:
    TERMINAL = {"released", "cancelled", "expired"}

    def __init__(self, store, health, config=None, clock=time.time):
        self.store, self.health, self.clock = store, health, clock
        self.config = {"healthy_seconds": 60, "max_peak_mib": 3072, "guest_reserve_mib": 512,
                       "max_swap_mib_s": 1,
                       "max_queue": 32, "lease_timeout_s": 30, "heartbeat_interval_s": 5,
                       "queued_owner_timeout_s": 30,
                       "max_wait_s": 86400, "max_run_s": 86400, **(config or {})}
        number(self.config["queued_owner_timeout_s"], "queued_owner_timeout_s", 5, 300)
        self.healthy_since = None
        self.last_tick = None
        self.failed = False
        self.state = store.load()
        self._validate_state()
        # Queue polling is deliberately volatile. After restart, live clients
        # have a fresh bounded grace to poll; persisted queue entries alone never
        # prove an owner is present and can never acquire the active slot.
        self.queue_last_seen = {key: self.clock() for key in self.state["queue"]}
        if self.state["active"]:
            request = self.state["requests"][self.state["active"]]
            request.update(state="blocked_orphan", reason="coordinator restarted; owner heartbeat required")
            self.store.save(self.state)

    def _validate_state(self):
        s = self.state
        if not isinstance(s, dict) or type(s.get("version")) is not int or s.get("version") != 1 or "active" not in s or not isinstance(s.get("requests"), dict) or not isinstance(s.get("queue"), list):
            raise ValueError("invalid admission state; explicit recovery required")
        for key, value in s["requests"].items():
            if not isinstance(value, dict) or value.get("request_id") != key or value.get("state") not in self.TERMINAL | {"queued", "granted", "blocked_orphan"}:
                raise ValueError("invalid admission request record")
            required = {"queue_deadline", "peak_mib", "guest", "run_timeout_s"} if value["state"] == "queued" else set()
            if value["state"] in {"granted", "blocked_orphan"}:
                required = {"lease_id", "run_deadline", "last_heartbeat"}
            if not required.issubset(value):
                raise ValueError("incomplete active or queued admission record")
            numeric = {"queue_deadline", "peak_mib", "run_timeout_s"} if value["state"] == "queued" else set()
            if value["state"] in {"granted", "blocked_orphan"}:
                numeric = {"run_deadline", "last_heartbeat"}
                if not isinstance(value["lease_id"], str) or not value["lease_id"]:
                    raise ValueError("invalid stored lease identity")
            if any(isinstance(value[field], bool) or not isinstance(value[field], (int, float)) or not math.isfinite(value[field]) or value[field] <= 0 for field in numeric):
                raise ValueError("invalid stored admission budget or deadline")
            if value["state"] == "queued" and not isinstance(value["guest"], bool):
                raise ValueError("invalid stored guest flag")
            if value["state"] == "queued" and (value.get("lease_id") or any(
                    field in value for field in ("granted_at", "run_deadline", "last_heartbeat"))):
                raise ValueError("queued request contains previous active ownership")
        active = s.get("active")
        live = [k for k, v in s["requests"].items() if v.get("state") in {"granted", "blocked_orphan"}]
        if live != ([active] if active else []):
            raise ValueError("inconsistent active admission state")
        if len(s["queue"]) != len(set(s["queue"])) or any(
                k not in s["requests"] or s["requests"][k].get("state") != "queued" for k in s["queue"]):
            raise ValueError("inconsistent admission queue")
        for k, v in s["requests"].items():
            if v.get("state") == "queued" and k not in s["queue"]:
                raise ValueError("unqueued pending request")

    def _save(self):
        terminal = sorted((v for v in self.state["requests"].values() if v["state"] in self.TERMINAL),
                          key=lambda v: v.get("finished_at", 0))
        for item in terminal[:-128]:
            del self.state["requests"][item["request_id"]]
        try:
            self.store.save(self.state)
        except Exception:
            self.failed = True
            raise

    def _request(self, request_id):
        try:
            return self.state["requests"][request_id]
        except KeyError:
            raise APIError(404, "unknown request")

    def _view(self, request):
        return {**request, "heartbeat_interval_s": self.config["heartbeat_interval_s"],
                "lease_timeout_s": self.config["lease_timeout_s"]}

    def tick(self, owner_poll=None):
        if self.failed:
            raise APIError(503, "durable state failed; restart and inspect before admitting work")
        now, changed = self.clock(), False
        if self.last_tick is None or now - self.last_tick > 10 or now < self.last_tick:
            self.healthy_since = None
        self.last_tick = now
        health = self.health.get()
        maintenance = health.get("maintenance_active", False) is not False
        swap_ok = all(isinstance(health.get(k), (int, float)) and 0 <= health[k] <= self.config["max_swap_mib_s"]
                      for k in ("swap_in_mib_s", "swap_out_mib_s"))
        if not maintenance and health.get("pressure_level") == 1 and swap_ok:
            if self.healthy_since is None or now < self.healthy_since:
                self.healthy_since = now
        else:
            self.healthy_since = None
        for key in list(self.state["queue"]):
            item = self._request(key)
            last_seen = self.queue_last_seen[key]
            if now < last_seen:
                self.queue_last_seen[key] = last_seen = now
            if now >= item["queue_deadline"] or now - last_seen >= self.config["queued_owner_timeout_s"]:
                reason = "queue deadline reached" if now >= item["queue_deadline"] else "queued owner stopped polling before work was granted"
                item.update(state="expired", reason=reason, finished_at=now)
                self.state["queue"].remove(key)
                self.queue_last_seen.pop(key, None)
                changed = True
        active = self.state["active"]
        if active:
            item = self._request(active)
            if now >= item["run_deadline"]:
                reason = "runtime deadline reached; confirm workload stopped before release"
            elif now - item["last_heartbeat"] >= self.config["lease_timeout_s"]:
                reason = "owner heartbeat expired; confirm workload stopped before release"
            else:
                reason = None
            if reason and (item["state"] != "blocked_orphan" or item.get("reason") != reason):
                item.update(state="blocked_orphan", reason=reason)
                changed = True
        elif self.state["queue"]:
            item = self._request(self.state["queue"][0])
            if maintenance:
                reason = "maintenance window active or maintenance file invalid/unreadable"
            elif self.healthy_since is None:
                reason = "host pressure or swap activity elevated or unavailable"
            elif now - self.healthy_since < self.config["healthy_seconds"]:
                reason = "waiting for stable normal host pressure"
            elif item["peak_mib"] > self.config["max_peak_mib"]:
                reason = "declared peak exceeds the current configured batch budget"
            elif item["guest"] and health.get("guest_available_mib") is None:
                reason = "guest available-memory probe unavailable"
            elif item["guest"] and health["guest_available_mib"] < item["peak_mib"] + self.config["guest_reserve_mib"]:
                reason = "guest available memory below declared peak plus reserve"
            elif owner_poll != item["request_id"]:
                reason = "waiting for the queued owner's current poll"
            else:
                reason = None
            if reason:
                item["reason"] = reason
            else:
                self.state["queue"].pop(0)
                self.queue_last_seen.pop(item["request_id"], None)
                self.state["active"] = item["request_id"]
                item.update(state="granted", reason=None, lease_id=secrets.token_hex(24),
                            granted_at=now, last_heartbeat=now, run_deadline=now + item["run_timeout_s"])
                changed = True
        if changed:
            self._save()

    def submit(self, body):
        request_id, job = body.get("request_id"), body.get("job")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", request_id):
            raise APIError(400, "invalid request_id")
        if not isinstance(job, str) or not 1 <= len(job) <= 100 or any(ord(c) < 32 for c in job):
            raise APIError(400, "invalid job")
        peak = number(body.get("peak_mib"), "peak_mib", 1, self.config["max_peak_mib"])
        wait = number(body.get("wait_timeout_s", 86400), "wait_timeout_s", 1, self.config["max_wait_s"])
        run = number(body.get("run_timeout_s"), "run_timeout_s", 1, self.config["max_run_s"])
        guest = body.get("guest", True)
        if not isinstance(guest, bool):
            raise APIError(400, "invalid guest flag")
        fields = dict(request_id=request_id, job=job, peak_mib=peak, guest=guest,
                      wait_timeout_s=wait, run_timeout_s=run)
        self.tick()
        existing = self.state["requests"].get(request_id)
        if existing:
            if any(existing.get(k) != v for k, v in fields.items()):
                raise APIError(409, "request_id already used for different work")
            return self._view(existing)
        if len(self.state["queue"]) >= self.config["max_queue"]:
            raise APIError(429, "admission queue full")
        now = self.clock()
        item = {**fields, "state": "queued", "created_at": now, "queue_deadline": now + wait,
                "lease_id": None, "reason": "queued behind earlier work"}
        self.state["requests"][request_id] = item
        self.state["queue"].append(request_id)
        self.queue_last_seen[request_id] = now
        self._save()
        self.tick()
        return self._view(item)

    def get(self, request_id):
        item = self._request(request_id)
        if item["state"] == "queued":
            self.queue_last_seen[request_id] = self.clock()
        self.tick(owner_poll=request_id)
        return self._view(self._request(request_id))

    def heartbeat(self, request_id, body):
        self.tick()
        item = self._request(request_id)
        self._owner(item, body)
        if self.state["active"] != request_id:
            raise APIError(409, "request no longer owns the slot")
        if self.clock() < item["run_deadline"]:
            recovered = item["state"] != "granted"
            item.update(state="granted", reason=None, last_heartbeat=self.clock())
            # Slot ownership and the immutable deadline are already durable.
            # Restart always requires owner recovery, so ordinary heartbeats
            # need not rewrite/fsync the entire bounded history every 5 seconds.
            if recovered:
                self._save()
        return self._view(item)

    @staticmethod
    def _owner(item, body):
        supplied = body.get("lease_id")
        if not isinstance(supplied, str) or not item.get("lease_id") or not hmac.compare_digest(supplied, item["lease_id"]):
            raise APIError(409, "lease identity mismatch")

    def finish(self, request_id, body, cancel=False):
        self.tick()
        item = self._request(request_id)
        if item["state"] in self.TERMINAL:
            return self._view(item)
        if self.state["active"] == request_id:
            self._owner(item, body)
            if body.get("stopped") is not True:
                raise APIError(409, "must confirm actual workload stopped")
            self.state["active"] = None
        elif item["state"] == "queued" and cancel:
            self.state["queue"].remove(request_id)
            self.queue_last_seen.pop(request_id, None)
        else:
            raise APIError(409, "only an active lease can be released")
        item.update(state="cancelled" if cancel else "released", finished_at=self.clock(), reason=None)
        self._save()  # Persist release before any replacement grant.
        self.tick()
        return self._view(item)

    def status(self):
        self.tick()
        return {"active": self.state["active"], "queue": list(self.state["queue"]),
                "healthy_since": self.healthy_since, "health": self.health.get(),
                "requests": [self._view(v) for v in self.state["requests"].values() if v["state"] not in self.TERMINAL]}


def dispatch(engine, method, endpoint, body):
    path = endpoint.strip("/").split("/")
    if method == "GET" and path == ["v1", "status"]:
        return engine.status()
    if method == "POST" and path == ["v1", "requests"]:
        return engine.submit(body)
    if len(path) >= 3 and path[:2] == ["v1", "requests"]:
        if method == "GET" and len(path) == 3:
            return engine.get(path[2])
        if method == "POST" and len(path) == 4 and path[3] == "heartbeat":
            return engine.heartbeat(path[2], body)
        if method == "POST" and len(path) == 4 and path[3] in {"release", "cancel"}:
            return engine.finish(path[2], body, cancel=path[3] == "cancel")
    raise APIError(404, "unknown endpoint")


def maintain(engine, lock, stop):
    """Observe stable health even when clients use short nonblocking queue waits."""
    while not stop.is_set():
        try:
            with lock:
                engine.tick()
        except Exception:
            pass  # Probe/state failures block grants; ownership is never discarded.
        stop.wait(5)


def handler_class(engine, token, lock=None):
    lock = lock or threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Never log auth, request bodies, URLs or job names.

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def respond(self, status, body):
            data = json.dumps(body, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def handle_api(self):
            try:
                if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                    raise APIError(401, "authentication required")
                body = {}
                if self.command == "POST":
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                        if not 0 < length <= 16384:
                            raise ValueError()
                        body = json.loads(self.rfile.read(length))
                        if not isinstance(body, dict):
                            raise ValueError()
                    except (ValueError, json.JSONDecodeError):
                        raise APIError(400, "invalid JSON body")
                with lock:
                    result = dispatch(engine, self.command, self.path, body)
                self.respond(200, result)
            except APIError as exc:
                self.respond(exc.status, {"error": exc.message})
            except Exception:
                # Includes failed durable writes; no success/grant is returned.
                self.respond(503, {"error": "admission unavailable; retain or stop owned work safely"})

        do_GET = do_POST = handle_api

    return Handler


@contextlib.contextmanager
def exclusive_state_lock(path):
    path = pathlib.Path(str(path) + ".lock")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=pathlib.Path, required=True)
    parser.add_argument("--initialize-state", action="store_true", help="one-time empty-state creation; exits without serving")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    token = pathlib.Path(config["token_file"]).read_text().strip()
    if len(token) < 32 or any(c.isspace() for c in token):
        raise ValueError("token file must contain one strong token of at least 32 characters")
    with exclusive_state_lock(config["state_file"]):
        if args.initialize_state:
            if pathlib.Path(config["state_file"]).exists():
                raise ValueError("refusing to overwrite existing state")
            StateFile(config["state_file"]).save({"version": 1, "requests": {}, "queue": [], "active": None})
            return
        engine = Coordinator(StateFile(config["state_file"]), HealthProbe(config), config)
        lock = threading.Lock()
        server = ThreadingHTTPServer((config.get("listen_host", "127.0.0.1"), config.get("port", 8766)), handler_class(engine, token, lock))
        server.daemon_threads = True
        stop_bridge = threading.Event()
        threading.Thread(target=maintain, args=(engine, lock, stop_bridge), daemon=True).start()
        if config.get("bridge_dir"):
            from bridge import FileBridge
            bridge = FileBridge(config["bridge_dir"], token, lambda method, path, body: dispatch(engine, method, path, body), lock)
            threading.Thread(target=bridge.serve, args=(stop_bridge,), daemon=True).start()
        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            stop_bridge.set()
            server.server_close()


if __name__ == "__main__":
    main()
