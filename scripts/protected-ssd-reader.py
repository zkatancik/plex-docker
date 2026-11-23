#!/usr/bin/python3
"""Read-only, bounded metadata audit of named Macintosh HD locations.

No deletion, file-content reads, subprocesses, external volumes, or symlink
traversal. Intended for a single macOS administrator-authorized invocation.
"""
import json
import os
import stat
import time

BASE = '/System/Volumes/Data'
ROOTS = (
    '.Spotlight-V100',
    'Library/Application Support/Apple/AssetCache/Data',
    'System/Library/AssetsV2/com_apple_MobileAsset_UAF_FM_Visual',
    'System/Library/AssetsV2/com_apple_MobileAsset_UAF_FM_GenerativeModels',
    'System/Library/AssetsV2/com_apple_MobileAsset_UAF_IF_Planner',
    'System/Library/AssetsV2/com_apple_MobileAsset_UAF_IF_PlannerOverrides',
    'System/Library/AssetsV2/com_apple_MobileAsset_UAF_FM_Overrides',
    'System/Library/AssetsV2/com_apple_MobileAsset_UAF_FM_CodeLM',
    'private/var/db',
)
FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def absolute_fd(path):
    fd = os.open('/', FLAGS)
    try:
        for part in path.strip('/').split('/'):
            child = os.open(part, FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def relative_fd(parent, path, device):
    fd = os.dup(parent)
    try:
        for part in path.split('/'):
            if part in ('', '.', '..'):
                raise ValueError('invalid component')
            s = os.stat(part, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(s.st_mode) or s.st_dev != device:
                raise ValueError('symlink, external filesystem, or non-directory')
            child = os.open(part, FLAGS, dir_fd=fd)
            if os.fstat(child).st_dev != device:
                os.close(child)
                raise ValueError('external filesystem')
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def collect(fd, device, result, deadline, entry_limit, depth=0):
    if depth > 14:
        result['complete'] = False
        return
    with os.scandir(fd) as entries:
        for entry in entries:
            if result['entries'] >= entry_limit or time.monotonic() >= deadline:
                result['complete'] = False
                return
            result['entries'] += 1
            try:
                s = entry.stat(follow_symlinks=False)
                if s.st_dev != device:
                    result['external_entries_skipped'] += 1
                    continue
                if stat.S_ISREG(s.st_mode):
                    result['allocated_bytes_checked'] += s.st_blocks * 512
                elif stat.S_ISDIR(s.st_mode):
                    child = os.open(entry.name, FLAGS, dir_fd=fd)
                    try:
                        if os.fstat(child).st_dev == device:
                            collect(child, device, result, deadline, entry_limit, depth + 1)
                    finally:
                        os.close(child)
            except OSError as error:
                result['errors'] += 1
                result['complete'] = False
                result['error_numbers'].append(error.errno)


def main():
    fd = absolute_fd(BASE)
    device = os.fstat(fd).st_dev
    report = {'time': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
              'uid': os.getuid(), 'scope': BASE, 'roots': []}
    overall_deadline = time.monotonic() + 15
    total_entries = 0
    try:
        for root in ROOTS:
            if time.monotonic() >= overall_deadline or total_entries >= 30000:
                report['unvisited_roots'] = list(ROOTS[len(report['roots']):])
                break
            result = {'root': root, 'entries': 0, 'allocated_bytes_checked': 0,
                      'complete': True, 'errors': 0, 'error_numbers': [],
                      'external_entries_skipped': 0}
            child = None
            try:
                child = relative_fd(fd, root, device)
                collect(child, device, result,
                        min(overall_deadline, time.monotonic() + 1),
                        min(8000, 30000 - total_entries))
            except (OSError, ValueError) as error:
                result['complete'] = False
                result['errors'] += 1
                result['error_numbers'].append(getattr(error, 'errno', None))
            finally:
                if child is not None:
                    os.close(child)
            result['error_numbers'] = sorted(set(result['error_numbers']), key=str)
            total_entries += result['entries']
            report['roots'].append(result)
        report['entries_checked'] = total_entries
        print(json.dumps(report, indent=2))
    finally:
        os.close(fd)


if __name__ == '__main__':
    main()
