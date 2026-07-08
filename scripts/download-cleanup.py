#!/usr/bin/env python3
"""Remove completed-download orphans and reclaim disk space.

Rules:
- Delete download folders with zero hardlinks to media (superseded / removed content).
- For partial-hardlink folders, delete only unlinked files (never touch media-linked inodes).
- Delete stale archive-only release folders after import/extract, unless still seeding.
- Skip torrents that are still actively managed for seeding.
- After seeding stops, *arr removeCompletedDownloads handles linked imports; this script
  catches superseded releases and manual media deletions.
- After 30 days, remove download-folder copies of fully-hardlinked content that *arr missed
  (media hardlinks survive; declutters Downloads/complete, does not free disk space).
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".wmv", ".ts"}
ARCHIVE_EXTS = {
    ".7z",
    ".ace",
    ".apk",
    ".appx",
    ".arj",
    ".bin",
    ".bz2",
    ".cab",
    ".cbr",
    ".cbt",
    ".cbz",
    ".cso",
    ".deb",
    ".dmg",
    ".ear",
    ".esd",
    ".gz",
    ".img",
    ".iso",
    ".jar",
    ".lha",
    ".lzh",
    ".lz",
    ".lz4",
    ".lzma",
    ".mdf",
    ".msi",
    ".msix",
    ".nrg",
    ".pkg",
    ".rar",
    ".rpm",
    ".sea",
    ".sit",
    ".sitx",
    ".swm",
    ".tar",
    ".tbz",
    ".tbz2",
    ".tgz",
    ".txz",
    ".war",
    ".wim",
    ".xar",
    ".xpi",
    ".xz",
    ".zip",
    ".zipx",
    ".zst",
    ".zstd",
}

RELEASE_SIDECAR_EXTS = {
    ".ass",
    ".bmp",
    ".cue",
    ".gif",
    ".jpg",
    ".jpeg",
    ".log",
    ".m3u",
    ".m3u8",
    ".md5",
    ".nfo",
    ".par2",
    ".png",
    ".rev",
    ".sfv",
    ".sha1",
    ".sha256",
    ".srs",
    ".srr",
    ".ssa",
    ".srt",
    ".idx",
    ".sub",
    ".sup",
    ".tif",
    ".tiff",
    ".txt",
    ".url",
    ".vtt",
    ".webp",
}
RELEASE_RESIDUE_EXTS = ARCHIVE_EXTS | RELEASE_SIDECAR_EXTS
SEED_MIN_SECONDS = 30 * 24 * 3600  # 30 days — matches Sonarr/Radarr seedTime and qBit global limit

DEFAULTS = {
    "downloads": os.environ.get("DOWNLOADS_PATH", "/Volumes/HomeLabPool/downloads/complete"),
    "media_tv": os.environ.get("MEDIA_TV_PATH", "/Volumes/HomeLabPool/Media/tv"),
    "media_movies": os.environ.get("MEDIA_MOVIES_PATH", "/Volumes/HomeLabPool/Media/movies"),
    "qb_url": os.environ.get("QBITTORRENT_URL", "http://localhost:8080"),
    "qb_user": os.environ.get("QBITTORRENT_USER", ""),
    "qb_pass": os.environ.get("QBITTORRENT_PASS", ""),
}


def fmt_size(num: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024:
            return f"{num:.1f}{unit}"
        num /= 1024
    return f"{num:.1f}PB"


def du(path: Path) -> int:
    result = subprocess.run(["du", "-sk", str(path)], capture_output=True, text=True)
    if result.returncode != 0:
        return 0
    return int(result.stdout.split()[0]) * 1024


def media_inodes(media_roots: list[Path]) -> set[int]:
    inodes: set[int] = set()
    for root in media_roots:
        if not root.exists():
            continue
        for dirpath, _, files in os.walk(root):
            for name in files:
                if name.startswith("."):
                    continue
                try:
                    inodes.add(os.stat(Path(dirpath) / name).st_ino)
                except OSError:
                    pass
    return inodes


def video_files(folder: Path) -> list[Path]:
    found: list[Path] = []
    for dirpath, _, files in os.walk(folder):
        for name in files:
            if Path(name).suffix.lower() in VIDEO_EXTS:
                found.append(Path(dirpath) / name)
    return found


def is_release_residue_file(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in RELEASE_RESIDUE_EXTS:
        return True
    if re.fullmatch(r"\.r\d{1,4}", suffix):
        return True
    if re.fullmatch(r"\.z\d{1,4}", suffix):
        return True
    if re.fullmatch(r"\.\d{1,4}", suffix):
        return True
    if re.search(r"\.part\d+$", path.name.lower()):
        return True
    return False


def release_residue_files(folder: Path) -> tuple[list[Path], list[Path]]:
    residue: list[Path] = []
    unknown: list[Path] = []
    for dirpath, _, files in os.walk(folder):
        for name in files:
            if name.startswith("."):
                continue
            path = Path(dirpath) / name
            if is_release_residue_file(path):
                residue.append(path)
            else:
                unknown.append(path)
    return residue, unknown


class QBittorrent:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        login = urllib.request.Request(
            f"{self.base_url}/api/v2/auth/login",
            data=urllib.parse.urlencode({"username": username, "password": password}).encode(),
        )
        self.opener.open(login, timeout=30)

    def torrents(self) -> list[dict]:
        with self.opener.open(f"{self.base_url}/api/v2/torrents/info", timeout=60) as resp:
            return json.loads(resp.read().decode())

    def delete_torrents(self, hashes: list[str], delete_files: bool) -> None:
        if not hashes:
            return
        data = urllib.parse.urlencode(
            {
                "hashes": "|".join(hashes),
                "deleteFiles": "true" if delete_files else "false",
            }
        ).encode()
        req = urllib.request.Request(f"{self.base_url}/api/v2/torrents/delete", data=data)
        self.opener.open(req, timeout=120)


def is_seeding(torrent: dict | None) -> bool:
    """Treat completed, non-stopped torrents as seeding even with no current peers."""
    if not torrent:
        return False
    state = torrent.get("state", "")
    progress = torrent.get("progress", 0)
    if torrent.get("upspeed", 0) > 0:
        return True
    if progress >= 1 and state not in {"stoppedUP", "pausedUP", "missingFiles", "error"}:
        return True
    return False


def seed_window_done(torrent: dict | None) -> bool:
    if not torrent:
        return True
    return torrent.get("seeding_time", 0) >= SEED_MIN_SECONDS


def prune_empty_dirs(path: Path) -> None:
    if not path.exists():
        return
    for dirpath, dirnames, filenames in os.walk(path, topdown=False):
        current = Path(dirpath)
        if current == path:
            continue
        try:
            if not any(current.iterdir()):
                current.rmdir()
        except OSError:
            pass
    try:
        if path.exists() and not any(path.iterdir()):
            path.rmdir()
    except OSError:
        pass


def analyze_folder(folder: Path, inodes: set[int]) -> tuple[int, int, list[Path], list[Path]]:
    videos = video_files(folder)
    if not videos:
        return 0, 0, [], []
    linked = [v for v in videos if os.stat(v).st_ino in inodes]
    unlinked = [v for v in videos if os.stat(v).st_ino not in inodes]
    return len(linked), len(videos), linked, unlinked


def declutter_linked_folder(
    folder: Path,
    linked: list[Path],
    torrent: dict | None,
    t_hash: str | None,
    qb: QBittorrent,
    dry_run: bool,
) -> None:
    """Remove download-folder hardlink copies; media paths keep the same inodes."""
    for path in linked:
        if not dry_run:
            try:
                path.unlink()
            except OSError:
                pass
    if not dry_run:
        prune_empty_dirs(folder)
        if t_hash:
            qb.delete_torrents([t_hash], delete_files=False)
        elif folder.exists() and not any(folder.rglob("*")):
            shutil.rmtree(folder, ignore_errors=True)


def run(dry_run: bool, include_seeding: bool) -> int:
    cfg = DEFAULTS.copy()
    downloads = Path(cfg["downloads"])
    media_roots = [Path(cfg["media_tv"]), Path(cfg["media_movies"])]

    if not cfg["qb_user"] or not cfg["qb_pass"]:
        print("Set QBITTORRENT_USER and QBITTORRENT_PASS", file=sys.stderr)
        return 1

    inodes = media_inodes(media_roots)
    qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"])
    torrents = qb.torrents()
    torrent_by_name = {t["name"]: t for t in torrents}
    torrent_by_content_name = {
        Path(t["content_path"]).name: t
        for t in torrents
        if t.get("content_path")
    }

    delete_torrent_files: list[str] = []
    remove_torrent_only: list[str] = []
    bytes_reclaimed = 0
    decluttered = 0
    actions: list[str] = []

    for folder in sorted(downloads.iterdir()):
        if folder.name.startswith("."):
            continue

        # Loose video files dropped directly in complete/
        if folder.is_file():
            if folder.suffix.lower() not in VIDEO_EXTS:
                continue
            torrent = torrent_by_name.get(folder.name) or torrent_by_content_name.get(folder.name)
            t_hash = torrent["hash"] if torrent else None
            seeding = is_seeding(torrent)
            try:
                if os.stat(folder).st_ino in inodes:
                    if torrent and not seeding and seed_window_done(torrent):
                        size = folder.stat().st_size
                        decluttered += 1
                        days = torrent.get("seeding_time", 0) // 86400
                        actions.append(f"DECLUTTER   {fmt_size(size):>8}  {folder.name[:55]} ({days}d seeded)")
                        if not dry_run:
                            folder.unlink()
                            qb.delete_torrents([t_hash], delete_files=False)
                    continue
            except OSError:
                continue
            if seeding and not include_seeding:
                actions.append(f"SKIP seeding  {fmt_size(folder.stat().st_size):>8}  {folder.name}")
                continue
            size = folder.stat().st_size
            bytes_reclaimed += size
            actions.append(f"DELETE file  {fmt_size(size):>8}  {folder.name}")
            if not dry_run:
                if t_hash:
                    qb.delete_torrents([t_hash], delete_files=True)
                else:
                    folder.unlink()
            continue

        if not folder.is_dir():
            continue

        linked_count, total_count, linked, unlinked = analyze_folder(folder, inodes)
        torrent = torrent_by_name.get(folder.name) or torrent_by_content_name.get(folder.name)
        t_hash = torrent["hash"] if torrent else None
        seeding = is_seeding(torrent) if torrent else False

        if total_count == 0:
            residue, unknown = release_residue_files(folder)
            if not residue or unknown:
                continue
            if seeding and not include_seeding:
                actions.append(f"SKIP archive {fmt_size(du(folder)):>8}  {folder.name}")
                continue
            size = du(folder)
            bytes_reclaimed += size
            actions.append(f"DELETE archive {fmt_size(size):>8}  {folder.name}")
            if not dry_run:
                if t_hash:
                    qb.delete_torrents([t_hash], delete_files=True)
                else:
                    shutil.rmtree(folder, ignore_errors=True)
            continue

        if linked_count == total_count and total_count > 0:
            if not seeding and seed_window_done(torrent):
                size = du(folder)
                decluttered += 1
                days = torrent.get("seeding_time", 0) // 86400 if torrent else 0
                label = f"{days}d seeded" if torrent else "no torrent"
                actions.append(f"DECLUTTER   {fmt_size(size):>8}  {folder.name[:55]} ({label})")
                if not dry_run:
                    declutter_linked_folder(folder, linked, torrent, t_hash, qb, dry_run)
            continue

        if linked_count == 0 and total_count > 0:
            if seeding and not include_seeding:
                actions.append(f"SKIP seeding  {fmt_size(du(folder)):>8}  {folder.name}")
                continue
            size = du(folder)
            bytes_reclaimed += size
            actions.append(f"DELETE all   {fmt_size(size):>8}  {folder.name}")
            if not dry_run:
                if t_hash:
                    qb.delete_torrents([t_hash], delete_files=True)
                else:
                    shutil.rmtree(folder, ignore_errors=True)
            continue

        if linked_count > 0 and unlinked:
            if seeding and not include_seeding:
                actions.append(f"SKIP partial {fmt_size(du(folder)):>8}  {folder.name} ({len(unlinked)} unlinked files)")
                continue
            unlinked_size = sum(os.stat(p).st_size for p in unlinked)
            bytes_reclaimed += unlinked_size
            actions.append(
                f"TRIM partial {fmt_size(unlinked_size):>8}  {folder.name} "
                f"({len(unlinked)}/{total_count} unlinked files)"
            )
            if not dry_run:
                for path in unlinked:
                    try:
                        path.unlink()
                    except OSError as exc:
                        actions.append(f"  ! failed to delete {path}: {exc}")
                prune_empty_dirs(folder)
                if not video_files(folder):
                    actions.append(f"  -> folder empty, removing torrent")
                    if t_hash:
                        qb.delete_torrents([t_hash], delete_files=True)
                    else:
                        shutil.rmtree(folder, ignore_errors=True)
                elif t_hash:
                    remove_torrent_only.append(t_hash)

    if not dry_run and remove_torrent_only:
        qb.delete_torrents(remove_torrent_only, delete_files=False)

    mode = "DRY RUN" if dry_run else "EXECUTED"
    print(f"=== download-cleanup ({mode}) ===\n")
    for line in actions:
        print(line)
    print(f"\nEstimated space reclaimed: {fmt_size(bytes_reclaimed)}")
    print(f"Hardlinked folders decluttered: {decluttered}")
    print(f"Actions: {len(actions)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report only, do not delete")
    parser.add_argument(
        "--include-seeding",
        action="store_true",
        help="Also remove orphans that are still trying to seed (default: skip)",
    )
    args = parser.parse_args()
    return run(dry_run=args.dry_run, include_seeding=args.include_seeding)


if __name__ == "__main__":
    raise SystemExit(main())
