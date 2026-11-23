#!/usr/bin/env python3
"""Read internal settings; gate managed startup on the approved external disk."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

USER_ROOT = '/Users/zack'
DATA_ROOT = '/System/Volumes/Data'
INTERNAL_PARTS = ('Library', 'Containers', 'com.docker.docker', 'Data', 'vms', '0', 'data')
SETTINGS_PARTS = ('Library', 'Group Containers', 'group.com.docker')
READER_PARTS = ('Applications', 'Docker Settings Reader.app', 'Contents', 'MacOS')
READER_NAME = 'DockerSettingsReader'
EXTERNAL_FOLDER = '/Volumes/HomeLabPool/DockerDesktop'
POOL_HELPER = '/Library/PrivilegedHelperTools/com.katancik.homelabpool-ready'
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_STARTUP_LOCK_FD = None
_STARTUP_PHASE = None

def _phase(name):
    if _STARTUP_PHASE is not None: _STARTUP_PHASE(name)


def descend(parent, parts, device, owner):
    fd = os.dup(parent)
    try:
        for part in parts:
            if part in ('', '.', '..'): raise ValueError('Unsafe directory component')
            child = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
            s = os.fstat(fd)
            if s.st_dev != device or s.st_uid != owner:
                raise ValueError('Unexpected directory device or owner')
        return fd
    except BaseException:
        os.close(fd)
        raise


def configured_folder(user_fd, device):
    """Never follows a configured path or reads an external volume."""
    directory = descend(user_fd, SETTINGS_PARTS, device, os.getuid())
    try:
        fd = os.open('settings-store.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
        try:
            s = os.fstat(fd)
            if (not stat.S_ISREG(s.st_mode) or s.st_dev != device or s.st_uid != os.getuid()
                    or s.st_nlink != 1 or s.st_size > 131072 or s.st_mode & 0o022):
                raise ValueError('Unsafe Docker settings file')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                raw = stream.read(131073)
            if len(raw) > 131072: raise ValueError('Docker settings exceed128KiB')
            data = json.loads(raw)
        finally:
            os.close(fd)
    finally:
        os.close(directory)
    if not isinstance(data, dict): raise ValueError('Invalid Docker settings object')
    default = str(Path(USER_ROOT).joinpath(*INTERNAL_PARTS))
    # Docker omits the field when using its default internal location.
    folder = data.get('DataFolder', default)
    if folder not in (default, EXTERNAL_FOLDER):
        raise ValueError('Docker disk location is unexpected; review configuration')
    return folder, data.get('AutoStart')


def startup_settings(user_fd, device):
    """Ask the sandboxed reader for live settings. Older installations read directly."""
    try:
        directory = descend(user_fd, READER_PARTS, device, os.getuid())
    except FileNotFoundError:
        return configured_folder(user_fd, device)
    try:
        s = os.stat(READER_NAME, dir_fd=directory, follow_symlinks=False)
        if (not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_dev != device
                or s.st_nlink != 1 or s.st_mode & 0o022 or not s.st_mode & 0o100):
            raise ValueError('Unsafe Docker settings reader')
    finally:
        os.close(directory)
    executable = Path(USER_ROOT).joinpath(*READER_PARTS, READER_NAME)
    # Keep this child inside the existing worker's deadline and lock ownership.
    locks = () if _STARTUP_LOCK_FD is None else (_STARTUP_LOCK_FD,)
    result = subprocess.run([str(executable), '--read-settings'], check=True, timeout=8,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, pass_fds=locks)
    if len(result.stdout) > 4096:
        raise ValueError('Oversized Docker settings reader response')
    data = json.loads(result.stdout)
    if not isinstance(data, dict) or set(data) != {'DataFolder', 'AutoStart'}:
        raise ValueError('Invalid Docker settings reader response')
    return data['DataFolder'], data['AutoStart']


def internal_raw_metadata(user_fd, device):
    directory = descend(user_fd, INTERNAL_PARTS, device, os.getuid())
    try:
        s = os.stat('Docker.raw', dir_fd=directory, follow_symlinks=False)
        if (not stat.S_ISREG(s.st_mode) or s.st_dev != device or s.st_uid != os.getuid()
                or s.st_nlink != 1):
            raise ValueError('Unsafe internal Docker disk metadata')
        return s.st_blocks * 512
    finally:
        os.close(directory)


def monitor_metadata(user_fd, device):
    """SSD monitor: only internal settings and optional internal raw metadata."""
    try:
        folder, _ = configured_folder(user_fd, device)
    except (OSError, ValueError):
        return {'docker_storage_location': 'unavailable', 'docker_metadata_unavailable': True}
    external = folder == EXTERNAL_FOLDER
    result = {'docker_storage_location': 'external' if external else 'internal',
              'docker_configured_data_folder': folder}
    if external:
        result['docker_external_allocation_measured'] = False
        result['docker_external_status'] = 'configured; availability and allocation not probed'
    try:
        amount = internal_raw_metadata(user_fd, device)
        result['docker_ssd_residual_allocated_bytes' if external else 'docker_allocated_bytes'] = amount
    except FileNotFoundError:
        if external: result['docker_ssd_residual_allocated_bytes'] = 0
        else: result['docker_metadata_unavailable'] = True
    except (OSError, ValueError):
        result['docker_internal_metadata_unavailable'] = True
    return result


def open_absolute(path):
    fd = os.open('/', DIR_FLAGS)
    try:
        for part in Path(path).parts[1:]:
            child = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def check_existing_disk(device):
    # Called ONLY after the exact pool helper succeeds. Metadata only, no raw
    # content reads, creation, fallback paths, imports, or feature upgrades.
    root = open_absolute('/Volumes/HomeLabPool')
    try:
        root_stat = os.fstat(root)
        if root_stat.st_dev == device: raise ValueError('Pool path is on the internal disk')
        directory = descend(root, ('DockerDesktop',), root_stat.st_dev, os.getuid())
        try:
            s = os.stat('Docker.raw', dir_fd=directory, follow_symlinks=False)
            if (not stat.S_ISREG(s.st_mode) or s.st_dev != root_stat.st_dev
                    or s.st_uid != os.getuid() or s.st_nlink != 1 or s.st_size <= 0):
                raise ValueError('The existing external Docker disk is unavailable or unsafe')
        finally:
            os.close(directory)
    finally:
        os.close(root)


def check_startup(*, require_shared_dirs=False):
    _phase('opening_internal_root')
    device = os.stat(DATA_ROOT).st_dev
    user = open_absolute(USER_ROOT)
    try:
        if os.fstat(user).st_dev != device: raise ValueError('User settings are not internal')
        _phase('opening_docker_settings')
        folder, auto_start = startup_settings(user, device)
    finally:
        os.close(user)
    if folder != EXTERNAL_FOLDER or auto_start is not False:
        raise ValueError('Expected approved external Docker location and AutoStart=false')
    _phase('checking_pool_helper')
    s = os.lstat(POOL_HELPER)
    if not stat.S_ISREG(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o022:
        raise ValueError('Pool readiness helper is unsafe')
    inherited = {'pass_fds': (_STARTUP_LOCK_FD,)} if _STARTUP_LOCK_FD is not None else {}
    subprocess.run([POOL_HELPER, '--check'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, **inherited)
    _phase('checking_external_disk')
    check_existing_disk(device)
    if require_shared_dirs:
        _phase('checking_shared_directories')
        # Preserve the original shell -d semantics: follow a directory symlink,
        # without introducing an unverified owner or same-device requirement.
        for name in ('downloads', 'Media'):
            if not os.path.isdir('/Volumes/HomeLabPool/' + name):
                raise ValueError('Required shared directory is unavailable')



if __name__ == '__main__':
    from startup_check_supervisor import cli, worker_entry, worker_phase, worker_lock_fd
    args = sys.argv[1:]
    if args in (['--check-startup'], ['--check-startup', '--require-shared-dirs']):
        sys.exit(cli(Path(__file__).resolve(), require_shared_dirs='--require-shared-dirs' in args))
    if args in (['--startup-worker'], ['--startup-worker', '--require-shared-dirs']):
        def checked_worker():
            global _STARTUP_LOCK_FD, _STARTUP_PHASE
            _STARTUP_LOCK_FD = worker_lock_fd()
            _STARTUP_PHASE = worker_phase
            check_startup(require_shared_dirs='--require-shared-dirs' in args)
        sys.exit(worker_entry(checked_worker))
    sys.exit('Usage: docker_storage_location.py --check-startup [--require-shared-dirs]')
