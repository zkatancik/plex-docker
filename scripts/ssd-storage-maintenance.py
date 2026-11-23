#!/usr/bin/env python3
"""Small, internal-SSD-only maintenance. No Docker API or external-volume I/O."""
import argparse
import fcntl
import json
import os
import re
import secrets
import stat
import subprocess
import time
from dataclasses import dataclass

from docker_storage_location import monitor_metadata

USER_ROOT = '/Users/zack'
DATA_ROOT = '/System/Volumes/Data'
STATE_PARTS = ('Library', 'Logs', 'storage-maintenance')
POLICY_FILE = 'ssd-policy.json'
FILE_FLAGS = os.O_NOFOLLOW | os.O_CLOEXEC
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | FILE_FLAGS

# Paths are relative to USER_ROOT. Never include source trees, profiles, live
# databases, swap, generic temp roots, Plex, or entire package/module stores.
ROOTS = (
    ('Library/Caches/go-build', 14),
    ('.npm/_cacache', 14),
    ('.npm/_logs', 30),
    ('Library/Caches/pip/http-v2', 14),
    ('Library/Caches/Homebrew/downloads', 14),
    ('Library/Application Support/Google/GoogleUpdater/crx_cache', 14),
    ('.vscode-server/data/CachedExtensionVSIXs', 14),
    ('Library/Application Support/Code/CachedExtensionVSIXs', 14),
    ('Library/Application Support/Cursor/CachedExtensionVSIXs', 14),
    ('Library/Logs/Homebrew', 30),
    ('Library/Application Support/Code/logs', 30),
    ('Library/Application Support/Cursor/logs', 30),
    ('go/pkg/mod/cache/download', 14),
)
# Only duplicate downloaded ZIP payloads are eligible here; retain extracted
# module directories, .mod/.info/.ziphash files and dependency metadata.
SUFFIXES = {'go/pkg/mod/cache/download': '.zip'}


def open_absolute_directory(path):
    """Open every component without following symlinks, including ancestors."""
    fd = os.open('/', DIR_FLAGS)
    try:
        for part in path.strip('/').split('/'):
            child = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def checked_dir(parent, name, device):
    fd = os.open(name, DIR_FLAGS, dir_fd=parent)
    s = os.fstat(fd)
    if s.st_dev != device or s.st_uid != os.getuid():
        os.close(fd)
        raise ValueError('directory is external or not owned by this user')
    return fd


def open_relative_directory(parent, parts, device):
    fd = os.dup(parent)
    try:
        for part in parts:
            if part in ('', '.', '..'):
                raise ValueError('invalid directory component')
            child = checked_dir(fd, part, device)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_json_at(fd, name, default):
    try:
        f = os.open(name, os.O_RDONLY | FILE_FLAGS, dir_fd=fd)
        with os.fdopen(f) as stream:
            s = os.fstat(stream.fileno())
            if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_size > 65536:
                raise ValueError('unsafe state file')
            return json.load(stream)
    except FileNotFoundError:
        return default


def write_json_at(fd, name, value):
    temporary = name + '.' + secrets.token_hex(6) + '.tmp'
    f = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | FILE_FLAGS,
                0o600, dir_fd=fd)
    with os.fdopen(f, 'w') as stream:
        s = os.fstat(stream.fileno())
        if s.st_uid != os.getuid() or s.st_nlink != 1 or not stat.S_ISREG(s.st_mode):
            raise ValueError('unsafe state file')
        json.dump(value, stream, indent=2)
        stream.write('\n')
    os.rename(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)


def metadata_snapshot(user_fd, device):
    v = os.statvfs(DATA_ROOT)
    result = {'time': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
              'free_bytes': v.f_bavail * v.f_frsize,
              'load': list(os.getloadavg())}
    result.update(monitor_metadata(user_fd, device))
    return result


def health_reasons():
    """Fail closed. All subprocesses query local process/kernel metadata only."""
    load = os.getloadavg()
    reasons = []
    if load[0] > 2.0 or load[1] > 3.0:
        return ['machine busy: load exceeds 2.0/3.0']
    try:
        boot = subprocess.run(['/usr/sbin/sysctl', '-n', 'kern.boottime'],
            capture_output=True, text=True, timeout=2, check=True).stdout
        match = re.search(r'sec\s*=\s*(\d+)', boot)
        if not match or time.time() - int(match[1]) < 1800:
            reasons.append('within 30 minutes of boot or boot time unavailable')
        memory = subprocess.run(['/usr/bin/memory_pressure', '-Q'],
            capture_output=True, text=True, timeout=2, check=True).stdout
        match = re.search(r'memory free percentage:\s*(\d+)%', memory)
        if not match or int(match[1]) < 25:
            reasons.append('memory headroom below 25% or unavailable')
        processes = subprocess.run(['/bin/ps', '-axo', 'pcpu=,comm='],
            capture_output=True, text=True, timeout=2, check=True).stdout
        blocked = {'go', 'compile', 'link', 'xcodebuild', 'swift', 'swiftc',
            'clang', 'clang++', 'npm', 'pnpm', 'pip', 'pip3', 'uv', 'brew',
            'ruby', 'docker', 'docker-buildx', 'GoogleUpdater',
            'GoogleSoftwareUpdateAgent', 'ShipIt'}
        for line in processes.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2:
                continue
            cpu = float(parts[0])
            name = os.path.basename(parts[1])
            if name in blocked or name.endswith('.test'):
                reasons.append('build, package, or updater process active: ' + name)
            elif name in {'backupd', 'backupd-helper'} and cpu > 1:
                reasons.append('Time Machine busy')
            elif name in {'node', 'com.apple.Virtualization.VirtualMachine'} and cpu > 50:
                reasons.append('VM or Node workload busy')
    except (OSError, subprocess.SubprocessError, ValueError):
        reasons.append('health metadata unavailable')
    return sorted(set(reasons))


@dataclass
class Budget:
    deadline: float
    max_entries: int = 2000
    max_files: int = 100
    max_bytes: int = 256 * 1024 * 1024
    entries: int = 0
    files: int = 0
    allocated_bytes: int = 0
    charged_bytes: int = 0
    oversized: int = 0
    errors: int = 0
    exhausted: bool = False

    def done(self):
        value = (self.exhausted or time.monotonic() >= self.deadline or self.entries >= self.max_entries
                 or self.files >= self.max_files or self.charged_bytes >= self.max_bytes)
        self.exhausted |= value
        return value


def sweep(fd, device, cutoff, budget, dry_run, depth=0, suffix=None):
    """Unlink old regular cache files only. No rmtree, file reads, or dir removal."""
    if depth > 8 or budget.done():
        return
    try:
        with os.scandir(fd) as entries:
            for entry in entries:
                if budget.done():
                    break
                budget.entries += 1
                try:
                    s = entry.stat(follow_symlinks=False)
                    if s.st_dev != device or s.st_uid != os.getuid():
                        continue
                    if stat.S_ISDIR(s.st_mode):
                        child = checked_dir(fd, entry.name, device)
                        try:
                            # Recheck inode against enumeration before descending.
                            if os.fstat(child).st_ino == s.st_ino:
                                sweep(child, device, cutoff, budget, dry_run, depth + 1, suffix)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(s.st_mode) and s.st_nlink == 1:
                        if suffix is not None and not entry.name.endswith(suffix):
                            continue
                        if max(s.st_atime, s.st_mtime, s.st_ctime) >= cutoff:
                            continue
                        amount = s.st_blocks * 512
                        # Also cap logical bytes so sparse files cannot escape the limit.
                        charge = max(amount, s.st_size)
                        if charge > budget.max_bytes - budget.charged_bytes:
                            budget.oversized += 1
                            continue
                        again = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                        if (again.st_ino, again.st_mode, again.st_size, again.st_mtime_ns,
                            again.st_ctime_ns, again.st_nlink) != (s.st_ino, s.st_mode,
                            s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_nlink):
                            continue
                        if not dry_run:
                            os.unlink(entry.name, dir_fd=fd)
                        budget.files += 1
                        budget.allocated_bytes += amount
                        budget.charged_bytes += charge
                        # Recheck load during work; stop rather than competing with a build.
                        if os.getloadavg()[0] > 2.0:
                            budget.exhausted = True
                            return
                        time.sleep(0.05)
                except (OSError, ValueError):
                    budget.errors += 1
    except OSError:
        budget.errors += 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pressure-check', action='store_true', help='metadata only')
    parser.add_argument('--status', action='store_true', help='print metadata only')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    device = os.stat(DATA_ROOT).st_dev
    user_fd = open_absolute_directory(USER_ROOT)
    state_fd = lock_fd = None
    try:
        if os.fstat(user_fd).st_dev != device:
            raise ValueError('user directory is not on Macintosh HD Data')
        report = metadata_snapshot(user_fd, device)
        if args.status:
            print(json.dumps(report, indent=2))
            return 0
        state_fd = open_relative_directory(user_fd, STATE_PARTS, device)
        lock_fd = os.open('ssd-maintenance.lock', os.O_RDWR | os.O_CREAT | FILE_FLAGS,
                          0o600, dir_fd=state_fd)
        lock_stat = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1 or lock_stat.st_uid != os.getuid():
            raise ValueError('unsafe lock')
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        report['mode'] = 'metadata-only' if args.pressure_check else 'cache-cleanup'
        if args.pressure_check:
            report['warning'] = report['free_bytes'] < 40 * 2**30
            report['urgent'] = report['free_bytes'] < 25 * 2**30
            write_json_at(state_fd, 'ssd-pressure.json', report)
            return 0
        policy = read_json_at(state_fd, POLICY_FILE, {})
        if policy.get('cleanup_enabled') is not True:
            report['skipped'] = ['cleanup held after repeated kernel panics']
        else:
            reasons = health_reasons()
            if reasons:
                report['skipped'] = reasons
            else:
                previous = read_json_at(state_fd, 'ssd-last-run.json', {})
                start = int(previous.get('next_root', 0)) % len(ROOTS)
                budget = Budget(time.monotonic() + 10)
                visited = []
                index = start
                for offset in range(len(ROOTS)):
                    index = (start + offset) % len(ROOTS)
                    if budget.done():
                        break
                    root, days = ROOTS[index]
                    try:
                        fd = open_relative_directory(user_fd, root.split('/'), device)
                        try:
                            visited.append(root)
                            sweep(fd, device, time.time() - days * 86400, budget,
                                  args.dry_run, suffix=SUFFIXES.get(root))
                        finally:
                            os.close(fd)
                    except FileNotFoundError:
                        continue
                    except (OSError, ValueError):
                        budget.errors += 1
                report.update({'dry_run': args.dry_run, 'visited': visited,
                    'entries_checked': budget.entries, 'cache_files': budget.files,
                    'eligible_allocated_bytes': budget.allocated_bytes,
                    'oversized_files_preserved': budget.oversized,
                    'errors': budget.errors, 'limit_reached': budget.exhausted,
                    'next_root': (index + 1) % len(ROOTS)})
        write_json_at(state_fd, 'ssd-last-run.json', report)
        print(json.dumps(report))
        return 0
    finally:
        for fd in (lock_fd, state_fd, user_fd):
            if fd is not None:
                os.close(fd)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({'skipped': ['unsafe or unavailable internal path/state'],
                          'error_type': type(error).__name__}))
        raise SystemExit(1)
