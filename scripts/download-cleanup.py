#!/usr/bin/env python3
"""Remove completed-download orphans and reclaim disk space.

Rules:
- Delete download folders with zero hardlinks to media (superseded / removed content).
- For partial-hardlink folders, delete only unlinked files (never touch media-linked inodes).
- Delete stale archive-only release folders after import/extract, unless still seeding.
- Never remove a torrent or payload before 30 cumulative days of active seeding.
- Record observed seed time in durable state before removing qBittorrent jobs.
- Quarantine unexpectedly unmatched payloads for 30 days, then clean them.
- Skip torrents that are still actively managed for seeding after the minimum window.
- Keep Arr removal disabled so this worker is the authoritative retention decision.
- Detach terminal, superseded Sonarr and Radarr queue warnings while preserving seeding.
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
import time
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
SEED_MIN_SECONDS = int(os.environ.get("MIN_SEED_TIME_SECONDS", 30 * 24 * 3600))
SEED_MIN_MINUTES = (SEED_MIN_SECONDS + 59) // 60
MAX_ACTIVE_DOWNLOADS = int(os.environ.get("MAX_ACTIVE_DOWNLOADS", 3))
UNMATCHED_GRACE_SECONDS = int(os.environ.get("UNMATCHED_GRACE_SECONDS", 30 * 24 * 3600))
STATE_VERSION = 1
EARLY_STOP_STATES = {"stoppedUP", "pausedUP"}
TRACKER_REMOVED_MESSAGES = (
    "torrent has been deleted",
    "torrent is not registered",
    "torrent not registered",
    "unregistered torrent",
)
SONARR_TERMINAL_QUEUE_REASONS = (
    "invalid season or episode",
    "not a quality revision upgrade for existing episode file(s)",
    "not an upgrade for existing episode file(s)",
    "one or more episodes expected in this release were not imported or missing from the release",
)
RADARR_TERMINAL_QUEUE_REASONS = (
    "invalid movie",
    "movie file already imported",
    "not a quality revision upgrade for existing movie file(s)",
    "one or more movies expected in this release were not imported or missing",
    "unable to parse file",
)
RADARR_TERMINAL_QUEUE_REASON_PREFIXES = (
    "movie file already imported at ",
    "not a custom format upgrade for existing movie file(s). ",
    "not an upgrade for existing movie file. existing quality: ",
)

DEFAULTS = {
    "downloads": os.environ.get("DOWNLOADS_PATH", "/Volumes/HomeLabPool/downloads/complete"),
    "media_tv": os.environ.get("MEDIA_TV_PATH", "/Volumes/HomeLabPool/Media/tv"),
    "media_movies": os.environ.get("MEDIA_MOVIES_PATH", "/Volumes/HomeLabPool/Media/movies"),
    "qb_url": os.environ.get("QBITTORRENT_URL", "http://localhost:8080"),
    "qb_user": os.environ.get("QBITTORRENT_USER", ""),
    "qb_pass": os.environ.get("QBITTORRENT_PASS", ""),
    "sonarr_url": os.environ.get("SONARR_URL", ""),
    "sonarr_api_key": os.environ.get("SONARR_API_KEY", ""),
    "sonarr_detached_category": os.environ.get(
        "SONARR_DETACHED_CATEGORY", "sonarr-rejected"
    ),
    "radarr_detached_category": os.environ.get(
        "RADARR_DETACHED_CATEGORY", "radarr-rejected"
    ),
    "queue_reconcile_grace_seconds": int(
        os.environ.get("QUEUE_RECONCILE_GRACE_SECONDS", 30 * 60)
    ),
    "radarr_url": os.environ.get("RADARR_URL", ""),
    "radarr_api_key": os.environ.get("RADARR_API_KEY", ""),
    "state_path": os.environ.get("RETENTION_STATE_PATH", "/state/retention-state.json"),
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


def empty_retention_state() -> dict:
    return {"version": STATE_VERSION, "paths": {}}


def load_retention_state(path: Path) -> dict:
    """Load durable observations, falling back to the previous atomic snapshot."""
    if not path.exists():
        return empty_retention_state()

    errors: list[str] = []
    for candidate in (path, path.with_suffix(path.suffix + ".bak")):
        if not candidate.exists():
            continue
        try:
            state = json.loads(candidate.read_text())
            if state.get("version") != STATE_VERSION or not isinstance(state.get("paths"), dict):
                raise ValueError("unsupported or malformed retention state")
            return state
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            errors.append(f"{candidate}: {exc}")
    raise RuntimeError("Unable to load retention state: " + "; ".join(errors))


def save_retention_state(path: Path, state: dict) -> None:
    """Atomically persist state while retaining one known-good prior snapshot."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    backup = path.with_suffix(path.suffix + ".bak")
    payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
    with temporary.open("w") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    if path.exists():
        try:
            current = json.loads(path.read_text())
            if current.get("version") == STATE_VERSION and isinstance(current.get("paths"), dict):
                shutil.copy2(path, backup)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    os.replace(temporary, path)


def torrent_top_level(torrent: dict, downloads: Path) -> str | None:
    """Return the exact managed entry beneath complete/, never a basename guess."""
    content_path = torrent.get("content_path")
    if content_path:
        try:
            relative = Path(content_path).relative_to(downloads)
        except ValueError:
            return None
        return relative.parts[0] if relative.parts else None

    name = torrent.get("name")
    save_path = torrent.get("save_path")
    if name and save_path and Path(save_path) == downloads:
        return name
    return None


def record_torrent_state(state: dict, torrents: list[dict], downloads: Path, now: int) -> set[str]:
    """Record the highest observed cumulative seed time for each managed torrent."""
    seen_paths: set[str] = set()
    paths = state.setdefault("paths", {})
    for torrent in torrents:
        top_level = torrent_top_level(torrent, downloads)
        torrent_hash = torrent.get("hash")
        if not top_level or not torrent_hash:
            continue
        seen_paths.add(top_level)
        path_record = paths.setdefault(
            top_level,
            {"first_seen": now, "last_seen": now, "unmatched_since": None, "torrents": {}},
        )
        path_record["last_seen"] = now
        path_record["unmatched_since"] = None
        torrent_record = path_record.setdefault("torrents", {}).setdefault(torrent_hash, {})
        observed_seed_time = max(
            int(torrent_record.get("seeding_time", 0)),
            int(torrent.get("seeding_time", 0)),
        )
        torrent_record.update(
            {
                "name": torrent.get("name", ""),
                "last_seen": now,
                "progress": torrent.get("progress", 0),
                "seeding_time": observed_seed_time,
                "seed_window_done": bool(torrent_record.get("seed_window_done"))
                or observed_seed_time >= SEED_MIN_SECONDS,
            }
        )
        torrent["_retention_verified"] = bool(torrent_record["seed_window_done"])
    return seen_paths


def unmatched_hold_reason(
    state: dict,
    name: str,
    now: int,
    mutate: bool,
) -> str | None:
    """Bound unmatched retention using verified history or a finite quarantine."""
    paths = state.setdefault("paths", {})
    path_record = paths.get(name)
    torrent_records = path_record.get("torrents", {}) if path_record else {}
    if torrent_records and all(record.get("seed_window_done") for record in torrent_records.values()):
        return None

    if path_record is None:
        path_record = {
            "first_seen": now,
            "last_seen": now,
            "unmatched_since": now,
            "torrents": {},
        }
        if mutate:
            paths[name] = path_record

    unmatched_since = path_record.get("unmatched_since")
    if unmatched_since is None:
        unmatched_since = now
        if mutate:
            path_record["unmatched_since"] = now

    elapsed = max(0, now - int(unmatched_since))
    if elapsed >= UNMATCHED_GRACE_SECONDS:
        return None
    remaining = UNMATCHED_GRACE_SECONDS - elapsed
    observed = max(
        (int(record.get("seeding_time", 0)) for record in torrent_records.values()),
        default=0,
    )
    return (
        f"unmatched quarantine: {remaining / 86400:.1f}d remaining; "
        f"last verified seed time {observed / 86400:.1f}d"
    )


def unmatched_cleanup_label(state: dict, name: str) -> str:
    path_record = state.get("paths", {}).get(name, {})
    torrent_records = path_record.get("torrents", {})
    if torrent_records and all(record.get("seed_window_done") for record in torrent_records.values()):
        return "seed window verified"
    return "unmatched quarantine expired"


def prune_retention_state(state: dict, downloads: Path, live_paths: set[str]) -> None:
    """Forget records only after both the payload and managed path are gone."""
    paths = state.setdefault("paths", {})
    for name in list(paths):
        if name not in live_paths and not (downloads / name).exists():
            del paths[name]


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

    def trackers(self, torrent_hash: str) -> list[dict]:
        query = urllib.parse.urlencode({"hash": torrent_hash})
        with self.opener.open(
            f"{self.base_url}/api/v2/torrents/trackers?{query}", timeout=30
        ) as resp:
            return json.loads(resp.read().decode())

    def preferences(self) -> dict:
        with self.opener.open(f"{self.base_url}/api/v2/app/preferences", timeout=30) as resp:
            return json.loads(resp.read().decode())

    def categories(self) -> dict:
        with self.opener.open(f"{self.base_url}/api/v2/torrents/categories", timeout=30) as resp:
            return json.loads(resp.read().decode())

    def post(self, path: str, values: dict[str, object]) -> None:
        data = urllib.parse.urlencode(values).encode()
        req = urllib.request.Request(f"{self.base_url}{path}", data=data)
        with self.opener.open(req, timeout=120) as response:
            response.read()

    def set_preferences(self, values: dict[str, object]) -> None:
        self.post("/api/v2/app/setPreferences", {"json": json.dumps(values)})

    def create_category(self, category: str) -> None:
        self.post("/api/v2/torrents/createCategory", {"category": category})

    def set_category(self, hashes: list[str], category: str) -> None:
        if hashes:
            self.post(
                "/api/v2/torrents/setCategory",
                {"hashes": "|".join(hashes), "category": category},
            )

    def set_share_limits(
        self,
        torrent_hash: str,
        ratio_limit: float,
        seeding_time_limit: int,
        inactive_seeding_time_limit: int,
        share_limit_action: str,
        share_limits_mode: str,
    ) -> None:
        self.post(
            "/api/v2/torrents/setShareLimits",
            {
                "hashes": torrent_hash,
                "ratioLimit": ratio_limit,
                "seedingTimeLimit": seeding_time_limit,
                "inactiveSeedingTimeLimit": inactive_seeding_time_limit,
                "shareLimitAction": share_limit_action,
                # Required by newer qBittorrent; safely ignored by 5.2.x.
                "shareLimitsMode": share_limits_mode,
            },
        )

    def start_torrents(self, hashes: list[str]) -> None:
        if hashes:
            self.post("/api/v2/torrents/start", {"hashes": "|".join(hashes)})

    def stop_torrents(self, hashes: list[str]) -> None:
        if hashes:
            self.post("/api/v2/torrents/stop", {"hashes": "|".join(hashes)})

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
        with self.opener.open(req, timeout=120) as response:
            response.read()


class ArrApi:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def request(self, path: str, method: str = "GET", payload: dict | None = None) -> object:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={"X-Api-Key": self.api_key, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
        return json.loads(body) if body else None

    def download_clients(self) -> list[dict]:
        result = self.request("/api/v3/downloadclient")
        return result if isinstance(result, list) else []

    def update_download_client(self, client: dict) -> None:
        self.request(f"/api/v3/downloadclient/{client['id']}", method="PUT", payload=client)

    def queue(self, query: str) -> list[dict]:
        result = self.request(f"/api/v3/queue?{query}")
        if not isinstance(result, dict):
            return []
        records = result.get("records", [])
        return records if isinstance(records, list) else []

    def managed_item(self, resource: str, item_id: int) -> dict:
        result = self.request(f"/api/v3/{resource}/{item_id}")
        return result if isinstance(result, dict) else {}


def qbit_categories_from_arr(api: ArrApi, category_field: str) -> set[str]:
    """Return non-empty categories owned by enabled Arr qBittorrent clients."""
    categories: set[str] = set()
    for client in api.download_clients():
        if client.get("implementation") != "QBittorrent" or not client.get("enable", True):
            continue
        for field in client.get("fields", []):
            if field.get("name") == category_field and field.get("value"):
                categories.add(str(field["value"]))
    return categories


def queue_rejection_reasons(record: dict) -> set[str]:
    """Flatten an Arr app's file-level queue messages into normalized reasons."""
    reasons: set[str] = set()
    for status in record.get("statusMessages", []):
        messages = [str(message).strip() for message in status.get("messages", []) if message]
        if messages:
            reasons.update(message.lower() for message in messages)
        elif status.get("title"):
            reasons.add(str(status["title"]).strip().lower())
    return reasons


def is_terminal_arr_queue_record(
    record: dict,
    allowed_reasons: tuple[str, ...],
    allowed_prefixes: tuple[str, ...] = (),
) -> bool:
    """Recognize only warnings that cannot succeed on a later automatic retry."""
    if (
        record.get("status") != "completed"
        or record.get("trackedDownloadStatus") != "warning"
        or record.get("trackedDownloadState") not in {"importPending", "importBlocked"}
    ):
        return False
    reasons = queue_rejection_reasons(record)
    return bool(reasons) and all(
        reason in allowed_reasons or reason.startswith(allowed_prefixes)
        for reason in reasons
    )


def terminal_arr_queue_groups(
    records: list[dict],
    torrents: list[dict],
    existing_item_ids: set[int],
    source_categories: set[str],
    item_id_field: str,
    allowed_reasons: tuple[str, ...],
    allowed_prefixes: tuple[str, ...],
    now: int,
    grace_seconds: int,
) -> list[tuple[dict, list[dict]]]:
    """Return fail-closed torrent/queue groups safe to detach from an Arr app."""
    grouped: dict[str, list[dict]] = {}
    for record in records:
        download_id = str(record.get("downloadId", "")).lower()
        if download_id:
            grouped.setdefault(download_id, []).append(record)

    torrents_by_hash = {
        str(torrent.get("hash", "")).lower(): torrent
        for torrent in torrents
        if torrent.get("hash")
    }
    candidates: list[tuple[dict, list[dict]]] = []
    for download_id, group in grouped.items():
        torrent = torrents_by_hash.get(download_id)
        item_ids = {int(record.get(item_id_field, 0)) for record in group}
        completed_on = int(torrent.get("completion_on", 0)) if torrent else 0
        if (
            not torrent
            or torrent.get("progress", 0) < 1
            or torrent.get("category") not in source_categories
            or completed_on <= 0
            or now - completed_on < grace_seconds
            or not item_ids
            or 0 in item_ids
            or not item_ids.issubset(existing_item_ids)
            or not all(
                is_terminal_arr_queue_record(record, allowed_reasons, allowed_prefixes)
                for record in group
            )
        ):
            continue
        candidates.append((torrent, group))
    return candidates


def reconcile_arr_queue(
    qb: QBittorrent,
    dry_run: bool,
    label: str,
    base_url: str,
    api_key: str,
    detached_category: str,
    category_field: str,
    item_id_field: str,
    item_resource: str,
    queue_query: str,
    allowed_reasons: tuple[str, ...],
    allowed_prefixes: tuple[str, ...],
    grace_seconds: int,
) -> int:
    """Move one Arr app's terminal warnings aside without deleting qBit data."""
    detached_category = detached_category.strip()
    if not detached_category:
        print(f"{label} detached category must not be empty", file=sys.stderr)
        return 1

    try:
        api = ArrApi(base_url, api_key)
        source_categories = qbit_categories_from_arr(api, category_field)
        if not source_categories or detached_category in source_categories:
            print(
                f"Refusing queue reconciliation because {label} categories are unsafe",
                file=sys.stderr,
            )
            return 1

        records = api.queue(queue_query)
        item_ids = {
            int(record.get(item_id_field, 0))
            for record in records
            if record.get(item_id_field)
        }
        existing_item_ids = {
            item_id
            for item_id in item_ids
            if api.managed_item(item_resource, item_id).get("hasFile")
        }
        candidates = terminal_arr_queue_groups(
            records,
            qb.torrents(),
            existing_item_ids,
            source_categories,
            item_id_field,
            allowed_reasons,
            allowed_prefixes,
            int(time.time()),
            grace_seconds,
        )
    except Exception as exc:
        print(f"{label} queue reconciliation failed: {exc}", file=sys.stderr)
        return 1

    mode = "DRY RUN" if dry_run else "EXECUTED"
    print(f"=== {label} queue reconciliation ({mode}) ===")
    if not candidates:
        print("No terminal queue warnings to detach")
        return 0

    if not dry_run:
        try:
            if detached_category not in qb.categories():
                qb.create_category(detached_category)
            qb.set_category([torrent["hash"] for torrent, _ in candidates], detached_category)
        except Exception as exc:
            print(f"Failed to detach {label} queue torrents: {exc}", file=sys.stderr)
            return 1

    for torrent, group in candidates:
        reasons = sorted({reason for record in group for reason in queue_rejection_reasons(record)})
        print(
            f"DETACH    {torrent.get('name', torrent['hash'])[:80]} "
            f"({len(group)} queue row(s); {'; '.join(reasons)}) -> {detached_category}"
        )
    return 0


def reconcile_arr_queues(dry_run: bool) -> int:
    """Reconcile Sonarr and Radarr independently, failing closed for either app."""
    cfg = DEFAULTS.copy()
    if not cfg["qb_user"] or not cfg["qb_pass"]:
        print("Set QBITTORRENT_USER and QBITTORRENT_PASS", file=sys.stderr)
        return 1

    try:
        qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"])
    except Exception as exc:
        print(f"Queue reconciliation could not connect to qBittorrent: {exc}", file=sys.stderr)
        return 1

    apps = (
        {
            "label": "Sonarr",
            "base_url": cfg["sonarr_url"],
            "api_key": cfg["sonarr_api_key"],
            "detached_category": cfg["sonarr_detached_category"],
            "category_field": "tvCategory",
            "item_id_field": "episodeId",
            "item_resource": "episode",
            "queue_query": (
                "page=1&pageSize=1000&includeUnknownSeriesItems=true"
                "&includeSeries=false&includeEpisode=false"
            ),
            "allowed_reasons": SONARR_TERMINAL_QUEUE_REASONS,
            "allowed_prefixes": (),
        },
        {
            "label": "Radarr",
            "base_url": cfg["radarr_url"],
            "api_key": cfg["radarr_api_key"],
            "detached_category": cfg["radarr_detached_category"],
            "category_field": "movieCategory",
            "item_id_field": "movieId",
            "item_resource": "movie",
            "queue_query": (
                "page=1&pageSize=1000&includeUnknownMovieItems=true&includeMovie=false"
            ),
            "allowed_reasons": RADARR_TERMINAL_QUEUE_REASONS,
            "allowed_prefixes": RADARR_TERMINAL_QUEUE_REASON_PREFIXES,
        },
    )

    failed = False
    for app in apps:
        if not app["base_url"] or not app["api_key"]:
            print(f"{app['label']} queue reconciliation is not configured", file=sys.stderr)
            failed = True
            continue
        result = reconcile_arr_queue(
            qb=qb,
            dry_run=dry_run,
            grace_seconds=int(cfg["queue_reconcile_grace_seconds"]),
            **app,
        )
        failed = failed or result != 0
    return 1 if failed else 0


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
        return False
    return bool(torrent.get("_retention_verified")) or (
        torrent.get("seeding_time", 0) >= SEED_MIN_SECONDS
    )


def retention_hold_reason(torrent: dict) -> str | None:
    """Return a fail-closed reason that prevents every destructive branch."""
    if torrent.get("progress", 0) < 1:
        return f"torrent incomplete ({torrent.get('progress', 0):.1%})"
    if torrent.get("_retention_verified"):
        return None
    seeded = int(torrent.get("seeding_time", 0))
    if seeded < SEED_MIN_SECONDS:
        return (
            f"seeded {seeded / 86400:.1f}/{SEED_MIN_SECONDS / 86400:.0f}d "
            f"(state {torrent.get('state', 'unknown')})"
        )
    return None


def tracker_payload_is_retired(trackers: list[dict]) -> bool:
    """Return true only when every real tracker explicitly says the torrent is gone."""
    real_trackers = [
        tracker
        for tracker in trackers
        if str(tracker.get("url", "")).startswith(("http://", "https://", "udp://"))
    ]
    if not real_trackers:
        return False
    return all(
        any(message in str(tracker.get("msg", "")).lower() for message in TRACKER_REMOVED_MESSAGES)
        for tracker in real_trackers
    )


def torrent_is_old_enough_to_retire(torrent: dict, now: int) -> bool:
    completed_on = int(torrent.get("completion_on", 0))
    return (
        torrent.get("progress", 0) >= 1
        and completed_on > 0
        and now - completed_on >= SEED_MIN_SECONDS
    )


def torrent_stopped_too_early(torrent: dict) -> bool:
    return (
        torrent.get("progress", 0) >= 1
        and torrent.get("seeding_time", 0) < SEED_MIN_SECONDS
        and torrent.get("state") in EARLY_STOP_STATES
        and not torrent.get("_retention_verified")
    )


def mark_torrent_retention_verified(
    state: dict, torrent: dict, downloads: Path, now: int, reason: str
) -> None:
    top_level = torrent_top_level(torrent, downloads)
    torrent_hash = torrent.get("hash")
    if not top_level or not torrent_hash:
        return
    path_record = state.setdefault("paths", {}).setdefault(
        top_level,
        {"first_seen": now, "last_seen": now, "unmatched_since": None, "torrents": {}},
    )
    torrent_record = path_record.setdefault("torrents", {}).setdefault(torrent_hash, {})
    torrent_record["seed_window_done"] = True
    torrent_record["retention_reason"] = reason
    torrent_record["retention_verified_at"] = now
    torrent["_retention_verified"] = True


def torrent_maps(
    torrents: list[dict], downloads: Path
) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """Map both qBit names and the first path component beneath complete/."""
    by_name: dict[str, list[dict]] = {}
    by_top_level: dict[str, list[dict]] = {}

    def add(mapping: dict[str, list[dict]], key: str, torrent: dict) -> None:
        matches = mapping.setdefault(key, [])
        if not any(existing.get("hash") == torrent.get("hash") for existing in matches):
            matches.append(torrent)

    for torrent in torrents:
        top_level = torrent_top_level(torrent, downloads)
        if not top_level:
            continue
        if torrent.get("content_path"):
            add(by_top_level, top_level, torrent)
        else:
            add(by_name, top_level, torrent)
    return by_name, by_top_level


def matched_torrents(
    name: str, by_name: dict[str, list[dict]], by_top_level: dict[str, list[dict]]
) -> list[dict]:
    matches: dict[str, dict] = {}
    for torrent in [*by_name.get(name, []), *by_top_level.get(name, [])]:
        matches[torrent.get("hash", f"unknown-{id(torrent)}")] = torrent
    return list(matches.values())


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
    torrent_hashes: list[str],
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
        if torrent_hashes:
            qb.delete_torrents(torrent_hashes, delete_files=False)
        elif folder.exists() and not any(folder.rglob("*")):
            shutil.rmtree(folder, ignore_errors=True)


def required_preference_updates(preferences: dict) -> dict[str, object]:
    """Keep retention safe and bound concurrent downloads without limiting uploads."""
    updates: dict[str, object] = {}
    current_limit = int(preferences.get("max_seeding_time", -1))
    if not preferences.get("max_seeding_time_enabled"):
        updates["max_seeding_time_enabled"] = True
    if current_limit < SEED_MIN_MINUTES:
        updates["max_seeding_time"] = SEED_MIN_MINUTES
    if preferences.get("max_ratio_enabled"):
        updates["max_ratio_enabled"] = False
    if preferences.get("max_inactive_seeding_time_enabled"):
        updates["max_inactive_seeding_time_enabled"] = False
    if preferences.get("max_ratio_act") != 0:
        updates["max_ratio_act"] = 0
    if MAX_ACTIVE_DOWNLOADS > 0:
        if not preferences.get("queueing_enabled"):
            updates["queueing_enabled"] = True
        if int(preferences.get("max_active_downloads", -1)) != MAX_ACTIVE_DOWNLOADS:
            updates["max_active_downloads"] = MAX_ACTIVE_DOWNLOADS
        # Keep every completed torrent eligible to seed. Queueing is used only
        # to limit simultaneous downloads, not upload availability.
        if int(preferences.get("max_active_uploads", -1)) != -1:
            updates["max_active_uploads"] = -1
        if int(preferences.get("max_active_torrents", -1)) != -1:
            updates["max_active_torrents"] = -1
    return updates


def required_torrent_limits(torrent: dict) -> tuple[float, int, int] | None:
    """Return explicit safe limits when any effective qBit limit is too short."""
    ratio = torrent.get("ratio_limit", -2)
    seed_minutes = int(torrent.get("seeding_time_limit", -2))
    inactive_minutes = int(torrent.get("inactive_seeding_time_limit", -2))
    effective_ratio = torrent.get("max_ratio", ratio)
    effective_seed_minutes = int(torrent.get("max_seeding_time", seed_minutes))
    effective_inactive_minutes = int(torrent.get("max_inactive_seeding_time", inactive_minutes))

    desired_ratio = -1 if effective_ratio >= 0 else ratio
    desired_seed_minutes = (
        SEED_MIN_MINUTES
        if 0 <= effective_seed_minutes < SEED_MIN_MINUTES
        else seed_minutes
    )
    desired_inactive_minutes = -1 if effective_inactive_minutes >= 0 else inactive_minutes
    desired = (desired_ratio, desired_seed_minutes, desired_inactive_minutes)
    current = (ratio, seed_minutes, inactive_minutes)
    return desired if desired != current else None


def guard_arr_removal(
    label: str, base_url: str, api_key: str, dry_run: bool
) -> list[str]:
    """Make this worker the only component authorized to remove qBittorrent jobs."""
    if not base_url or not api_key:
        return [f"ERROR     {label} retention API is not configured"]
    api = ArrApi(base_url, api_key)
    actions: list[str] = []
    for client in api.download_clients():
        if client.get("implementation") != "QBittorrent" or not client.get("enable", True):
            continue
        changes: list[str] = []
        if client.get("removeCompletedDownloads"):
            changes.append("completed")
        if client.get("removeFailedDownloads"):
            changes.append("failed")
        if changes:
            actions.append(
                f"NORMALIZE {label} {client.get('name', 'qBittorrent')}: "
                f"disable {'/'.join(changes)} removal"
            )
            if not dry_run:
                updated = client.copy()
                updated["removeCompletedDownloads"] = False
                updated["removeFailedDownloads"] = False
                api.update_download_client(updated)
    return actions


def guard_retention(dry_run: bool) -> int:
    """Continuously normalize share limits and restart torrents stopped too early."""
    cfg = DEFAULTS.copy()
    if not cfg["qb_user"] or not cfg["qb_pass"]:
        print("Set QBITTORRENT_USER and QBITTORRENT_PASS", file=sys.stderr)
        return 1

    state_path = Path(cfg["state_path"])
    state = load_retention_state(state_path)
    qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"])
    preference_updates = required_preference_updates(qb.preferences())
    torrents = qb.torrents()
    downloads = Path(cfg["downloads"])
    now = int(time.time())
    record_torrent_state(state, torrents, downloads, now)
    actions: list[str] = []

    if preference_updates:
        actions.append(f"NORMALIZE global share limits: {sorted(preference_updates)}")
        if not dry_run:
            qb.set_preferences(preference_updates)

    restart: list[str] = []
    retire: list[str] = []
    for torrent in torrents:
        corrected = required_torrent_limits(torrent)
        share_action = torrent.get("share_limit_action", "Default")
        share_mode = torrent.get("share_limits_mode", "Default")
        action_needs_update = share_action != "Stop"
        mode_needs_update = "share_limits_mode" in torrent and share_mode != "MatchAny"
        if corrected or action_needs_update or mode_needs_update:
            ratio, seed_minutes, inactive_minutes = corrected or (
                torrent.get("ratio_limit", -2),
                int(torrent.get("seeding_time_limit", -2)),
                int(torrent.get("inactive_seeding_time_limit", -2)),
            )
            actions.append(
                f"NORMALIZE {torrent.get('name', torrent.get('hash', 'unknown'))[:80]} "
                f"(seed {torrent.get('seeding_time_limit')} -> {seed_minutes} min; action Stop)"
            )
            if not dry_run:
                qb.set_share_limits(
                    torrent["hash"],
                    ratio,
                    seed_minutes,
                    inactive_minutes,
                    "Stop",
                    "MatchAny" if "share_limits_mode" in torrent else "Default",
                )

        if torrent_stopped_too_early(torrent):
            restart.append(torrent["hash"])
            actions.append(
                f"RESTART   {torrent.get('name', torrent['hash'])[:80]} "
                f"({torrent.get('seeding_time', 0) / 86400:.1f}d seeded)"
            )

        if (
            torrent.get("state") not in EARLY_STOP_STATES
            and torrent.get("seeding_time", 0) < SEED_MIN_SECONDS
            and torrent_is_old_enough_to_retire(torrent, now)
        ):
            try:
                payload_retired = tracker_payload_is_retired(qb.trackers(torrent["hash"]))
            except Exception as exc:
                actions.append(
                    f"WARNING   tracker check failed for "
                    f"{torrent.get('name', torrent['hash'])[:65]}: {exc}"
                )
                payload_retired = False
            if payload_retired:
                retire.append(torrent["hash"])
                actions.append(
                    f"RETIRE    {torrent.get('name', torrent['hash'])[:80]} "
                    f"(tracker deleted it; completed "
                    f"{(now - int(torrent['completion_on'])) / 86400:.1f}d ago)"
                )
                if not dry_run:
                    mark_torrent_retention_verified(
                        state,
                        torrent,
                        downloads,
                        now,
                        "all trackers report torrent deleted",
                    )

    if restart and not dry_run:
        qb.start_torrents(restart)
    if retire and not dry_run:
        qb.stop_torrents(retire)

    failed = False
    for label, url_key, api_key in (
        ("Sonarr", "sonarr_url", "sonarr_api_key"),
        ("Radarr", "radarr_url", "radarr_api_key"),
    ):
        try:
            arr_actions = guard_arr_removal(label, cfg[url_key], cfg[api_key], dry_run)
            actions.extend(arr_actions)
            failed = failed or any(action.startswith("ERROR") for action in arr_actions)
        except Exception as exc:
            actions.append(f"ERROR     {label} retention guard failed: {exc}")
            failed = True

    if not dry_run:
        save_retention_state(state_path, state)

    if actions or dry_run:
        mode = "DRY RUN" if dry_run else "EXECUTED"
        print(f"=== seeding-retention guard ({mode}) ===")
        for action in actions:
            print(action)
        if not actions:
            print("No changes required")
    return 1 if failed else 0


def run(dry_run: bool, include_seeding: bool) -> int:
    cfg = DEFAULTS.copy()
    downloads = Path(cfg["downloads"])
    media_roots = [Path(cfg["media_tv"]), Path(cfg["media_movies"])]
    state_path = Path(cfg["state_path"])

    if not cfg["qb_user"] or not cfg["qb_pass"]:
        print("Set QBITTORRENT_USER and QBITTORRENT_PASS", file=sys.stderr)
        return 1

    missing_paths = [path for path in [downloads, *media_roots] if not path.is_dir()]
    if missing_paths:
        print(
            "Refusing cleanup because required storage paths are unavailable: "
            + ", ".join(str(path) for path in missing_paths),
            file=sys.stderr,
        )
        return 1

    inodes = media_inodes(media_roots)
    state = load_retention_state(state_path)
    qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"])
    torrents = qb.torrents()
    now = int(time.time())
    live_paths = record_torrent_state(state, torrents, downloads, now)
    torrent_by_name, torrent_by_top_level = torrent_maps(torrents, downloads)

    remove_torrent_only: list[str] = []
    bytes_reclaimed = 0
    decluttered = 0
    actions: list[str] = []

    for folder in sorted(downloads.iterdir()):
        if folder.name.startswith("."):
            continue

        matches = matched_torrents(folder.name, torrent_by_name, torrent_by_top_level)
        unmatched_allowed = False
        unmatched_label = ""
        hold_reasons = [reason for torrent in matches if (reason := retention_hold_reason(torrent))]
        if not matches:
            unmatched_reason = unmatched_hold_reason(
                state,
                folder.name,
                now,
                mutate=not dry_run,
            )
            if unmatched_reason:
                hold_reasons.append(unmatched_reason)
            else:
                unmatched_allowed = True
                unmatched_label = unmatched_cleanup_label(state, folder.name)
        if hold_reasons:
            actions.append(
                f"PROTECT      {fmt_size(du(folder)):>8}  {folder.name[:70]} "
                f"({'; '.join(hold_reasons)})"
            )
            continue

        torrent = min(matches, key=lambda item: item.get("seeding_time", 0)) if matches else None
        torrent_hashes = [item["hash"] for item in matches if item.get("hash")]
        seeding = any(is_seeding(item) for item in matches)

        # Loose video files dropped directly in complete/
        if folder.is_file():
            if folder.suffix.lower() not in VIDEO_EXTS:
                if unmatched_allowed:
                    size = folder.stat().st_size
                    bytes_reclaimed += size
                    actions.append(f"DELETE unmatched file {fmt_size(size):>8}  {folder.name}")
                    if not dry_run:
                        folder.unlink()
                continue
            try:
                if os.stat(folder).st_ino in inodes:
                    if not seeding and (not torrent or seed_window_done(torrent)):
                        size = folder.stat().st_size
                        decluttered += 1
                        label = (
                            f"{torrent.get('seeding_time', 0) // 86400}d seeded"
                            if torrent
                            else unmatched_label
                        )
                        actions.append(f"DECLUTTER   {fmt_size(size):>8}  {folder.name[:55]} ({label})")
                        if not dry_run:
                            folder.unlink()
                            qb.delete_torrents(torrent_hashes, delete_files=False)
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
                if torrent_hashes:
                    qb.delete_torrents(torrent_hashes, delete_files=True)
                else:
                    folder.unlink()
            continue

        if not folder.is_dir():
            continue

        linked_count, total_count, linked, unlinked = analyze_folder(folder, inodes)

        if total_count == 0:
            residue, unknown = release_residue_files(folder)
            if not residue or unknown:
                if unmatched_allowed:
                    size = du(folder)
                    bytes_reclaimed += size
                    actions.append(f"DELETE unmatched dir  {fmt_size(size):>8}  {folder.name}")
                    if not dry_run:
                        shutil.rmtree(folder, ignore_errors=True)
                continue
            if seeding and not include_seeding:
                actions.append(f"SKIP archive {fmt_size(du(folder)):>8}  {folder.name}")
                continue
            size = du(folder)
            bytes_reclaimed += size
            actions.append(f"DELETE archive {fmt_size(size):>8}  {folder.name}")
            if not dry_run:
                if torrent_hashes:
                    qb.delete_torrents(torrent_hashes, delete_files=True)
                else:
                    shutil.rmtree(folder, ignore_errors=True)
            continue

        if linked_count == total_count and total_count > 0:
            if not seeding and (not torrent or seed_window_done(torrent)):
                size = du(folder)
                decluttered += 1
                days = torrent.get("seeding_time", 0) // 86400 if torrent else 0
                label = f"{days}d seeded" if torrent else unmatched_label
                actions.append(f"DECLUTTER   {fmt_size(size):>8}  {folder.name[:55]} ({label})")
                if not dry_run:
                    declutter_linked_folder(folder, linked, torrent_hashes, qb, dry_run)
            continue

        if linked_count == 0 and total_count > 0:
            if seeding and not include_seeding:
                actions.append(f"SKIP seeding  {fmt_size(du(folder)):>8}  {folder.name}")
                continue
            size = du(folder)
            bytes_reclaimed += size
            actions.append(f"DELETE all   {fmt_size(size):>8}  {folder.name}")
            if not dry_run:
                if torrent_hashes:
                    qb.delete_torrents(torrent_hashes, delete_files=True)
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
                    if torrent_hashes:
                        qb.delete_torrents(torrent_hashes, delete_files=True)
                    else:
                        shutil.rmtree(folder, ignore_errors=True)
                elif torrent_hashes:
                    remove_torrent_only.extend(torrent_hashes)

    if not dry_run and remove_torrent_only:
        qb.delete_torrents(remove_torrent_only, delete_files=False)

    if not dry_run:
        prune_retention_state(state, downloads, live_paths)
        save_retention_state(state_path, state)

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
        "--guard",
        action="store_true",
        help="Normalize qBittorrent share limits and restart completed torrents stopped before 30 days",
    )
    parser.add_argument(
        "--reconcile-queue",
        action="store_true",
        help="Detach terminal Sonarr/Radarr warnings while preserving qBittorrent data",
    )
    parser.add_argument(
        "--include-seeding",
        action="store_true",
        help="Remove active orphans only after the hard minimum seed window (default: skip)",
    )
    args = parser.parse_args()
    if args.guard:
        return guard_retention(dry_run=args.dry_run)
    if args.reconcile_queue:
        return reconcile_arr_queues(dry_run=args.dry_run)
    return run(dry_run=args.dry_run, include_seeding=args.include_seeding)


if __name__ == "__main__":
    raise SystemExit(main())
