#!/usr/bin/env python3
"""Audit and normalize media that is incompatible with the Plex Apple TV client.

The worker is intentionally host-native: it uses the Mac's VideoToolbox encoder,
keeps runtime state outside Git, and only modifies future Arr imports unless an
operator explicitly selects an existing path or Sonarr season.
"""

from __future__ import annotations

import argparse
import configparser
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple


STATE_VERSION = 1
SCORE_REJECT = -10000
CUSTOM_HFR_NAME = "Explicit HFR FPS"
CUSTOM_HFR_REGEX = r"\b(?:48|50|59[ .]?94|60|120)[ ._-]?fps\b"
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".ts"}
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
HDR_PRIMARIES = {"bt2020"}
RETRYABLE_STATUSES = {"blocked", "error", "interrupted"}
NORMALIZABLE_SIGNATURES = {"av1", "hfr", "incomplete_hvcc"}
PROFILE_NAMES = {
    "sonarr": ["1080p HQ", "4K HDR Preferred", "WEB-2160p", "WEB-1080p"],
    "radarr": [
        "1080p HQ",
        "4K HDR Preferred",
        "UHD Bluray + WEB",
        "Remux + WEB 1080p",
    ],
}
DEFAULT_PATH_MAPPINGS = [
    ("/data/", "/Volumes/HomeLabPool/"),
    ("/tv/", "/Volumes/HomeLabPool/Media/tv/"),
    ("/movies/", "/Volumes/HomeLabPool/Media/movies/"),
]


class NormalizerError(RuntimeError):
    """Base error for expected worker failures."""


class SafetyError(NormalizerError):
    """A replacement safety precondition was not met."""


class ValidationError(NormalizerError):
    """An encoded output failed a compatibility or quality gate."""


class LockBusy(NormalizerError):
    """Another normalizer process owns the lock."""


@dataclass
class Settings:
    repo_root: Path
    env_path: Path
    state_path: Path
    lock_path: Path
    rollback_root: Path
    ffmpeg: str
    ffprobe: str
    sonarr_url: str
    sonarr_api_key: str
    radarr_url: str
    radarr_api_key: str
    tautulli_url: str
    tautulli_config: Path
    plex_plist: Path
    stable_age_seconds: int = 120
    settle_seconds: float = 2.0
    rollback_days: int = 30
    quality_window_seconds: int = 10


@dataclass
class ManagedMedia:
    service: str
    owner_id: int
    file_id: int
    path: Path
    date_added: str
    season_number: Optional[int] = None


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def isoformat(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_datetime(value: str) -> dt.datetime:
    if not value:
        return dt.datetime.fromtimestamp(0, tz=dt.timezone.utc)
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def load_dotenv(path: Path) -> Dict[str, str]:
    """Parse dotenv values without executing the file in a shell."""
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        value = raw_value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            quote = value[0]
            value = value[1:-1]
            if quote == '"':
                value = bytes(value, "utf-8").decode("unicode_escape")
        values[key] = value
    return values


def executable(env: Dict[str, str], key: str, preferred: str, fallback: str) -> str:
    configured = env.get(key) or os.environ.get(key)
    if configured:
        return configured
    if Path(preferred).is_file():
        return preferred
    found = shutil.which(fallback)
    return found or preferred


def load_settings(repo_root: Optional[Path] = None) -> Settings:
    root = (repo_root or Path(__file__).resolve().parents[1]).resolve()
    env_path = Path(os.environ.get("MEDIA_NORMALIZER_ENV", str(root / ".env")))
    env = load_dotenv(env_path)
    runtime = Path(
        os.environ.get("MEDIA_NORMALIZER_STATE_DIR", str(root / "media-normalizer"))
    )
    return Settings(
        repo_root=root,
        env_path=env_path,
        state_path=runtime / "state.json",
        lock_path=runtime / "normalizer.lock",
        rollback_root=Path(
            os.environ.get(
                "MEDIA_NORMALIZER_ROLLBACK_ROOT",
                "/Volumes/HomeLabPool/media-normalizer/rollback",
            )
        ),
        ffmpeg=executable(
            env,
            "MEDIA_NORMALIZER_FFMPEG",
            "/Users/zack/.homebrew/bin/ffmpeg",
            "ffmpeg",
        ),
        ffprobe=executable(
            env,
            "MEDIA_NORMALIZER_FFPROBE",
            "/Users/zack/.homebrew/bin/ffprobe",
            "ffprobe",
        ),
        sonarr_url=os.environ.get(
            "MEDIA_NORMALIZER_SONARR_URL", "http://127.0.0.1:8989"
        ).rstrip("/"),
        sonarr_api_key=env.get("SONARR_API_KEY", os.environ.get("SONARR_API_KEY", "")),
        radarr_url=os.environ.get(
            "MEDIA_NORMALIZER_RADARR_URL", "http://127.0.0.1:7878"
        ).rstrip("/"),
        radarr_api_key=env.get("RADARR_API_KEY", os.environ.get("RADARR_API_KEY", "")),
        tautulli_url=os.environ.get(
            "MEDIA_NORMALIZER_TAUTULLI_URL", "http://127.0.0.1:8181"
        ).rstrip("/"),
        tautulli_config=Path(
            os.environ.get(
                "MEDIA_NORMALIZER_TAUTULLI_CONFIG", str(root / "tautulli/config.ini")
            )
        ),
        plex_plist=Path(
            os.environ.get(
                "MEDIA_NORMALIZER_PLEX_PLIST",
                str(Path.home() / "Library/Preferences/com.plexapp.plexmediaserver.plist"),
            )
        ),
        stable_age_seconds=int(os.environ.get("MEDIA_NORMALIZER_STABLE_AGE", "120")),
        settle_seconds=float(os.environ.get("MEDIA_NORMALIZER_SETTLE_SECONDS", "2")),
        rollback_days=int(os.environ.get("MEDIA_NORMALIZER_ROLLBACK_DAYS", "30")),
        quality_window_seconds=int(
            os.environ.get("MEDIA_NORMALIZER_QUALITY_WINDOW", "10")
        ),
    )


def redact_text(value: str, secrets: Iterable[str]) -> str:
    redacted = value
    for secret in secrets:
        if secret and len(secret) >= 4:
            redacted = redacted.replace(secret, "[REDACTED]")
    redacted = re.sub(
        r"(?i)(apikey|api_key|token|x-plex-token)([=:\s]+)[^&\s\"']+",
        r"\1\2[REDACTED]",
        redacted,
    )
    return redacted


def log(message: str, settings: Optional[Settings] = None, *, stderr: bool = False) -> None:
    secrets: List[str] = []
    if settings:
        secrets = [settings.sonarr_api_key, settings.radarr_api_key]
    timestamp = isoformat(utc_now())
    print(
        "%s %s" % (timestamp, redact_text(message, secrets)),
        file=sys.stderr if stderr else sys.stdout,
        flush=True,
    )


def empty_state() -> Dict[str, Any]:
    return {"version": STATE_VERSION, "watermarks": {}, "files": {}, "rollbacks": []}


def valid_state(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("version") == STATE_VERSION
        and isinstance(value.get("watermarks"), dict)
        and isinstance(value.get("files"), dict)
        and isinstance(value.get("rollbacks"), list)
    )


def load_state(path: Path) -> Dict[str, Any]:
    errors: List[str] = []
    for candidate in (path, path.with_suffix(path.suffix + ".bak")):
        if not candidate.exists():
            continue
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
            if not valid_state(value):
                raise ValueError("unsupported or incomplete state")
            return value
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append("%s: %s" % (candidate, exc))
    if errors:
        raise NormalizerError("unable to recover normalizer state: " + "; ".join(errors))
    return empty_state()


def atomic_write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
    backup = path.with_suffix(path.suffix + ".bak")
    backup_temp = backup.with_name(".%s.%d.tmp" % (backup.name, os.getpid()))
    try:
        if path.exists():
            shutil.copy2(path, backup_temp)
            os.replace(backup_temp, backup)
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        for leftover in (temp, backup_temp):
            with contextlib.suppress(FileNotFoundError):
                leftover.unlink()


@contextlib.contextmanager
def process_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockBusy("another media-normalizer process is already running") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_process(
    command: Sequence[str],
    *,
    timeout: Optional[float] = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NormalizerError("subprocess failed: %s" % exc) from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-4000:]
        raise NormalizerError(
            "command exited %d (%s): %s"
            % (result.returncode, Path(command[0]).name, detail)
        )
    return result


def api_path_to_host(value: str) -> Path:
    mappings = list(DEFAULT_PATH_MAPPINGS)
    configured = os.environ.get("MEDIA_NORMALIZER_PATH_MAPPINGS", "")
    if configured:
        mappings = []
        for item in configured.split(","):
            if "=" in item:
                source, destination = item.split("=", 1)
                mappings.append((source.rstrip("/") + "/", destination.rstrip("/") + "/"))
    for source, destination in mappings:
        if value.startswith(source):
            return Path(destination + value[len(source) :])
    return Path(value)


class ArrClient:
    def __init__(self, service: str, base_url: str, api_key: str):
        self.service = service
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def request(
        self,
        method: str,
        endpoint: str,
        payload: Optional[Dict[str, Any]] = None,
        query: Optional[Dict[str, Any]] = None,
    ) -> Any:
        if not self.api_key:
            raise NormalizerError("%s API key is missing" % self.service)
        url = "%s/api/v3/%s" % (self.base_url, endpoint.lstrip("/"))
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None
        headers = {"X-Api-Key": self.api_key, "Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            raise NormalizerError("%s API request failed: %s" % (self.service, exc)) from exc
        return json.loads(raw) if raw else None

    def get(self, endpoint: str, query: Optional[Dict[str, Any]] = None) -> Any:
        return self.request("GET", endpoint, query=query)

    def post(self, endpoint: str, payload: Dict[str, Any]) -> Any:
        return self.request("POST", endpoint, payload=payload)

    def put(self, endpoint: str, payload: Dict[str, Any]) -> Any:
        return self.request("PUT", endpoint, payload=payload)

    def managed_media(self) -> List[ManagedMedia]:
        found: List[ManagedMedia] = []
        if self.service == "sonarr":
            for series in self.get("series"):
                series_id = int(series["id"])
                for media in self.get("episodefile", {"seriesId": series_id}):
                    raw_path = media.get("path")
                    if not raw_path:
                        raw_path = str(Path(series["path"]) / media["relativePath"])
                    found.append(
                        ManagedMedia(
                            service="sonarr",
                            owner_id=series_id,
                            file_id=int(media["id"]),
                            path=api_path_to_host(raw_path),
                            date_added=media.get("dateAdded", ""),
                            season_number=(
                                int(media["seasonNumber"])
                                if media.get("seasonNumber") is not None
                                else None
                            ),
                        )
                    )
            return found

        for movie in self.get("movie"):
            media = movie.get("movieFile") or {}
            if not media or not movie.get("hasFile"):
                continue
            raw_path = media.get("path")
            if not raw_path:
                raw_path = str(Path(movie["path"]) / media["relativePath"])
            found.append(
                ManagedMedia(
                    service="radarr",
                    owner_id=int(movie["id"]),
                    file_id=int(media["id"]),
                    path=api_path_to_host(raw_path),
                    date_added=media.get("dateAdded", ""),
                )
            )
        return found

    def sonarr_season(self, series_id: int, season: int) -> List[ManagedMedia]:
        if self.service != "sonarr":
            raise NormalizerError("season selection requires Sonarr")
        series = self.get("series/%d" % series_id)
        episodes = self.get("episode", {"seriesId": series_id})
        file_seasons = {
            int(item["episodeFileId"]): int(item["seasonNumber"])
            for item in episodes
            if item.get("hasFile") and item.get("episodeFileId")
        }
        found: List[ManagedMedia] = []
        for media in self.get("episodefile", {"seriesId": series_id}):
            season_number = media.get("seasonNumber")
            if season_number is None:
                season_number = file_seasons.get(int(media["id"]))
            if season_number != season:
                continue
            raw_path = media.get("path") or str(
                Path(series["path"]) / media["relativePath"]
            )
            found.append(
                ManagedMedia(
                    service="sonarr",
                    owner_id=series_id,
                    file_id=int(media["id"]),
                    path=api_path_to_host(raw_path),
                    date_added=media.get("dateAdded", ""),
                    season_number=season,
                )
            )
        return sorted(found, key=lambda item: str(item.path))

    def rescan(self, owner_id: int) -> None:
        if self.service == "sonarr":
            self.post("command", {"name": "RescanSeries", "seriesId": owner_id})
        else:
            self.post("command", {"name": "RescanMovie", "movieId": owner_id})

    def sync_explicit_hfr_policy(self) -> Dict[str, Any]:
        desired = {
            "name": CUSTOM_HFR_NAME,
            "includeCustomFormatWhenRenaming": False,
            "specifications": [
                {
                    "name": "Explicit high frame rate",
                    "implementation": "ReleaseTitleSpecification",
                    "negate": False,
                    "required": False,
                    "fields": [{"name": "value", "value": CUSTOM_HFR_REGEX}],
                }
            ],
        }
        existing = next(
            (item for item in self.get("customformat") if item.get("name") == CUSTOM_HFR_NAME),
            None,
        )
        if existing:
            format_id = int(existing["id"])
            if custom_format_regex(existing) != CUSTOM_HFR_REGEX:
                update = dict(existing)
                update.update(desired)
                updated = self.put("customformat/%d" % format_id, update)
                format_id = int(updated.get("id", format_id))
        else:
            created = self.post("customformat", desired)
            format_id = int(created["id"])

        updated_profiles: List[str] = []
        missing_profiles: List[str] = []
        by_name = {item["name"]: item for item in self.get("qualityprofile")}
        for name in PROFILE_NAMES[self.service]:
            profile = by_name.get(name)
            if profile is None:
                missing_profiles.append(name)
                continue
            format_items = profile.setdefault("formatItems", [])
            item = next((entry for entry in format_items if entry["format"] == format_id), None)
            if item is None:
                format_items.append(
                    {"format": format_id, "name": CUSTOM_HFR_NAME, "score": SCORE_REJECT}
                )
            elif item.get("score") == SCORE_REJECT:
                continue
            else:
                item["score"] = SCORE_REJECT
            self.put("qualityprofile/%d" % profile["id"], profile)
            updated_profiles.append(name)
        return {
            "service": self.service,
            "format_id": format_id,
            "updated_profiles": updated_profiles,
            "missing_profiles": missing_profiles,
        }


def custom_format_regex(custom_format: Dict[str, Any]) -> Optional[str]:
    for specification in custom_format.get("specifications", []):
        if specification.get("implementation") != "ReleaseTitleSpecification":
            continue
        for field in specification.get("fields", []):
            if field.get("name") == "value":
                return field.get("value")
    return None


def arr_clients(settings: Settings) -> Dict[str, ArrClient]:
    return {
        "sonarr": ArrClient("sonarr", settings.sonarr_url, settings.sonarr_api_key),
        "radarr": ArrClient("radarr", settings.radarr_url, settings.radarr_api_key),
    }


def parse_fraction(value: Any) -> Optional[Fraction]:
    if value in (None, "", "0/0", "N/A"):
        return None
    try:
        return Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return None


def rate_string(rate: Optional[Fraction]) -> Optional[str]:
    if rate is None:
        return None
    return "%d/%d" % (rate.numerator, rate.denominator)


def hfr_target(rate: Optional[Fraction]) -> Optional[Fraction]:
    if rate is None:
        return None
    candidates = [
        (Fraction(48, 1), Fraction(24, 1)),
        (Fraction(50, 1), Fraction(25, 1)),
        (Fraction(60000, 1001), Fraction(30000, 1001)),
        (Fraction(60, 1), Fraction(30, 1)),
    ]
    for source, target in candidates:
        if abs(float(rate - source)) <= 0.01:
            return target
    return None


def extradata_bytes(value: str) -> bytes:
    chunks: List[str] = []
    for line in (value or "").splitlines():
        if ":" not in line:
            continue
        payload = line.split(":", 1)[1]
        hex_column = re.split(r"\s{2,}", payload.strip(), maxsplit=1)[0]
        chunks.extend(re.findall(r"[0-9A-Fa-f]{2}", hex_column))
    return bytes.fromhex("".join(chunks)) if chunks else b""


def hvcc_parameter_sets(data: bytes) -> Optional[Dict[str, int]]:
    """Return HEVC hvcC VPS/SPS/PPS counts, or None for an unknown record."""
    if len(data) < 23 or data[0] != 1:
        return None
    offset = 23
    counts = {"vps": 0, "sps": 0, "pps": 0}
    names = {32: "vps", 33: "sps", 34: "pps"}
    try:
        for _ in range(data[22]):
            nal_type = data[offset] & 0x3F
            offset += 1
            number = int.from_bytes(data[offset : offset + 2], "big")
            offset += 2
            for _ in range(number):
                length = int.from_bytes(data[offset : offset + 2], "big")
                offset += 2
                if length <= 0 or offset + length > len(data):
                    return None
                if nal_type in names:
                    counts[names[nal_type]] += 1
                offset += length
    except (IndexError, ValueError):
        return None
    return counts


def hvcc_status(video: Dict[str, Any]) -> Tuple[Optional[bool], List[str]]:
    if video.get("codec_name") not in {"hevc", "h265"}:
        return None, []
    counts = hvcc_parameter_sets(extradata_bytes(video.get("extradata", "")))
    if counts is None:
        return None, ["unparseable hvcC configuration"]
    missing = [name.upper() for name in ("vps", "sps", "pps") if counts[name] == 0]
    return not missing, missing


def probe_media(path: Path, ffprobe: str) -> Dict[str, Any]:
    result = run_process(
        [
            ffprobe,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-show_chapters",
            "-show_data",
            "-of",
            "json",
            str(path),
        ],
        timeout=180,
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise NormalizerError("ffprobe returned invalid JSON for %s" % path) from exc


def primary_video(probe: Dict[str, Any]) -> Dict[str, Any]:
    videos = [stream for stream in probe.get("streams", []) if stream.get("codec_type") == "video"]
    if not videos:
        raise NormalizerError("media has no video stream")
    return videos[0]


def media_duration(probe: Dict[str, Any]) -> float:
    values = [probe.get("format", {}).get("duration"), primary_video(probe).get("duration")]
    for value in values:
        try:
            if value not in (None, "N/A"):
                return float(value)
        except (TypeError, ValueError):
            pass
    raise NormalizerError("media duration is unavailable")


def video_rate(video: Dict[str, Any]) -> Optional[Fraction]:
    return parse_fraction(video.get("avg_frame_rate")) or parse_fraction(video.get("r_frame_rate"))


def bit_depth(video: Dict[str, Any]) -> int:
    raw = video.get("bits_per_raw_sample")
    if raw not in (None, "", "N/A"):
        with contextlib.suppress(ValueError):
            return int(raw)
    match = re.search(r"(?:p|yuv\d{3}p)(10|12|16)(?:le|be)?", video.get("pix_fmt", ""))
    return int(match.group(1)) if match else 8


def hdr_reasons(path: Path, video: Dict[str, Any]) -> List[str]:
    reasons: List[str] = []
    transfer = str(video.get("color_transfer", "")).lower()
    primaries = str(video.get("color_primaries", "")).lower()
    if transfer in HDR_TRANSFERS:
        reasons.append("HDR transfer %s" % transfer)
    if primaries in HDR_PRIMARIES:
        reasons.append("HDR primaries %s" % primaries)
    side_data = json.dumps(video.get("side_data_list", []), sort_keys=True).lower()
    if any(marker in side_data for marker in ("dovi", "dolby vision", "mastering display", "content light")):
        reasons.append("HDR or Dolby Vision side data")
    tags = json.dumps(video.get("tags", {}), sort_keys=True)
    label = "%s %s" % (path.name, tags)
    if re.search(r"(?i)\b(?:dovi|dolby[ ._-]?vision|dv|hdr10\+?|hlg|pq)\b", label):
        reasons.append("HDR or Dolby Vision label")
    return sorted(set(reasons))


def classify_media(path: Path, probe: Dict[str, Any]) -> Dict[str, Any]:
    video = primary_video(probe)
    codec = str(video.get("codec_name", "unknown")).lower()
    rate = video_rate(video)
    signatures: List[str] = []
    details: List[str] = []
    if codec in {"av1", "av01"}:
        signatures.append("av1")
    if rate is not None and float(rate) > 30.01:
        signatures.append("hfr")
        if hfr_target(rate) is None:
            details.append("unsupported HFR rate %s" % rate_string(rate))
    hvcc_complete, hvcc_detail = hvcc_status(video)
    if hvcc_complete is False:
        signatures.append("incomplete_hvcc")
        details.append("hvcC missing %s" % ", ".join(hvcc_detail))
    elif hvcc_complete is None and codec in {"hevc", "h265"}:
        details.extend(hvcc_detail)
    hdr = hdr_reasons(path, video)
    decision = "compatible"
    if signatures:
        if hdr:
            decision = "report_only_hdr"
        elif "hfr" in signatures and hfr_target(rate) is None:
            decision = "report_only_unsupported_hfr"
        else:
            decision = "normalize"
    elif hvcc_complete is None and codec in {"hevc", "h265"}:
        decision = "report_only_unknown_hevc_configuration"
    return {
        "path": str(path),
        "codec": codec,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "frame_rate": rate_string(rate),
        "frame_rate_float": float(rate) if rate is not None else None,
        "bit_depth": bit_depth(video),
        "color_primaries": video.get("color_primaries"),
        "color_transfer": video.get("color_transfer"),
        "color_space": video.get("color_space"),
        "signatures": signatures,
        "details": details,
        "hdr_reasons": hdr,
        "decision": decision,
    }


def sample_positions(duration: float, window: float = 5.0) -> List[float]:
    maximum = max(0.0, duration - window)
    values = [0.0, max(0.0, duration / 2.0 - window / 2.0), maximum]
    result: List[float] = []
    for value in values:
        rounded = round(min(value, maximum), 3)
        if rounded not in result:
            result.append(rounded)
    return result


def sample_decode(path: Path, probe: Dict[str, Any], ffmpeg: str) -> List[Dict[str, Any]]:
    duration = media_duration(probe)
    results: List[Dict[str, Any]] = []
    for position in sample_positions(duration):
        command = [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-ss",
            "%.3f" % position,
            "-t",
            "5",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-f",
            "null",
            "-",
        ]
        result = run_process(command, timeout=180, check=False)
        results.append(
            {
                "position": position,
                "ok": result.returncode == 0,
                "error": result.stderr.strip()[-1000:] if result.returncode else "",
            }
        )
    return results


def inspect_path(path: Path, settings: Settings, *, decode_candidates: bool = True) -> Dict[str, Any]:
    if not path.is_file():
        raise NormalizerError("media file does not exist: %s" % path)
    probe = probe_media(path, settings.ffprobe)
    report = classify_media(path, probe)
    stat = path.stat()
    report["file_identity"] = "%d:%d:%d" % (stat.st_dev, stat.st_ino, stat.st_size)
    report["size"] = stat.st_size
    report["duration"] = media_duration(probe)
    if decode_candidates and report["decision"] == "normalize":
        samples = sample_decode(path, probe, settings.ffmpeg)
        report["decode_samples"] = samples
        if not all(sample["ok"] for sample in samples):
            report["decision"] = "report_only_decode_failure"
    return report


def bitrate_range(width: int, height: int) -> Tuple[int, int]:
    if width >= 3000 or height >= 2000:
        return 20_000_000, 35_000_000
    if width >= 1900 or height >= 1000:
        return 8_000_000, 15_000_000
    return 5_000_000, 10_000_000


def source_video_bitrate(probe: Dict[str, Any]) -> int:
    video = primary_video(probe)
    candidates = [video.get("bit_rate"), probe.get("format", {}).get("bit_rate")]
    for candidate in candidates:
        try:
            value = int(candidate)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    duration = media_duration(probe)
    size = int(probe.get("format", {}).get("size") or 0)
    return int(size * 8 / duration) if size and duration else 0


def select_bitrate(probe: Dict[str, Any]) -> Tuple[int, int]:
    video = primary_video(probe)
    floor, ceiling = bitrate_range(int(video.get("width") or 0), int(video.get("height") or 0))
    source = source_video_bitrate(probe)
    return min(ceiling, max(floor, source)), ceiling


def retry_bitrate(current: int, ceiling: int) -> Optional[int]:
    if current >= ceiling:
        return None
    return min(ceiling, int(math.ceil(current * 1.25 / 1000.0) * 1000))


def output_frame_rate(report: Dict[str, Any]) -> Fraction:
    source = parse_fraction(report.get("frame_rate"))
    if "hfr" in report.get("signatures", []):
        target = hfr_target(source)
        if target is None:
            raise ValidationError("unsupported HFR rate %s" % report.get("frame_rate"))
        return target
    if source is None:
        raise ValidationError("source frame rate is unknown")
    return source


def stream_inventory(probe: Dict[str, Any]) -> Dict[str, Any]:
    inventory: Dict[str, Any] = {"chapters": len(probe.get("chapters", []))}
    for kind in ("audio", "subtitle", "attachment", "data"):
        entries = []
        for stream in probe.get("streams", []):
            if stream.get("codec_type") != kind:
                continue
            entries.append(
                {
                    "codec": stream.get("codec_name"),
                    "tags": {
                        name: stream.get("tags", {}).get(name)
                        for name in ("language", "title", "filename", "mimetype")
                        if stream.get("tags", {}).get(name) is not None
                    },
                    "disposition": stream.get("disposition", {}),
                }
            )
        inventory[kind] = entries
    return inventory


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def require_stable(path: Path, settings: Settings) -> os.stat_result:
    first = path.stat()
    age = time.time() - first.st_mtime
    if age < settings.stable_age_seconds:
        raise SafetyError("file is only %.0f seconds old" % age)
    if settings.settle_seconds > 0:
        time.sleep(settings.settle_seconds)
    second = path.stat()
    if (first.st_dev, first.st_ino, first.st_size, first.st_mtime_ns) != (
        second.st_dev,
        second.st_ino,
        second.st_size,
        second.st_mtime_ns,
    ):
        raise SafetyError("file changed during the stability check")
    return second


def lsof_active(path: Path) -> bool:
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    result = run_process([lsof, "-F", "p", "--", str(path)], timeout=30, check=False)
    return result.returncode == 0 and bool(result.stdout.strip())


def tautulli_api_key(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path)
    for section in parser.sections():
        for key in ("api_key", "apikey"):
            value = parser.get(section, key, fallback="").strip()
            if value:
                return value
    return None


def tautulli_active(path: Path, settings: Settings) -> bool:
    api_key = tautulli_api_key(settings.tautulli_config)
    if not api_key:
        return False
    query = urllib.parse.urlencode({"apikey": api_key, "cmd": "get_activity"})
    url = "%s/api/v2?%s" % (settings.tautulli_url, query)
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise SafetyError("unable to verify Tautulli playback activity: %s" % exc) from exc
    sessions = payload.get("response", {}).get("data", {}).get("sessions", [])
    target = str(path.resolve())
    for session in sessions:
        candidate = session.get("file") or session.get("full_path")
        if candidate and str(api_path_to_host(candidate).resolve()) == target:
            return True
    return False


def require_inactive(path: Path, settings: Settings) -> None:
    if lsof_active(path):
        raise SafetyError("file is open according to lsof")
    if tautulli_active(path, settings):
        raise SafetyError("file is in an active Plex/Tautulli session")


def estimated_output_bytes(probe: Dict[str, Any], bitrate: int) -> int:
    duration = media_duration(probe)
    source_total = int(probe.get("format", {}).get("bit_rate") or bitrate)
    source_video = max(0, source_video_bitrate(probe))
    non_video = max(0, source_total - source_video)
    return int(duration * (bitrate + non_video) / 8 * 1.2) + 1024**3


def require_free_space(directory: Path, required: int) -> None:
    free = shutil.disk_usage(directory).free
    if free < required:
        raise SafetyError(
            "insufficient free space: need %.1f GiB, have %.1f GiB"
            % (required / 1024**3, free / 1024**3)
        )


def rollback_path(source: Path, settings: Settings) -> Path:
    digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:12]
    timestamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    return settings.rollback_root / (timestamp + "-" + digest) / source.name


def create_rollback(source: Path, destination: Path) -> Dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=False)
    before = source.stat()
    method = "hardlink"
    try:
        os.link(source, destination)
    except OSError:
        method = "copy"
        require_free_space(destination.parent, before.st_size + 1024**3)
        shutil.copy2(source, destination)
    after = destination.stat()
    if after.st_size != before.st_size:
        raise SafetyError("rollback copy size does not match source")
    if method == "hardlink" and (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
        raise SafetyError("rollback hardlink does not reference the source inode")
    return {
        "path": str(destination),
        "method": method,
        "inode": after.st_ino,
        "device": after.st_dev,
        "created_at": isoformat(utc_now()),
    }


def temporary_output(path: Path) -> Path:
    return path.parent / (".%s.media-normalizer.%d.tmp.mkv" % (path.name, os.getpid()))


def encode_command(
    source: Path,
    output: Path,
    probe: Dict[str, Any],
    report: Dict[str, Any],
    bitrate: int,
    ffmpeg: str,
) -> List[str]:
    video = primary_video(probe)
    depth = bit_depth(video)
    pixel_format = "p010le" if depth > 8 else "yuv420p"
    profile = "main10" if depth > 8 else "main"
    filters: List[str] = []
    if "hfr" in report.get("signatures", []):
        filters.append("fps=%s" % rate_string(output_frame_rate(report)))
    filters.append("format=%s" % pixel_format)
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-map",
        "0",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        "-c",
        "copy",
        "-c:v:0",
        "hevc_videotoolbox",
        "-profile:v:0",
        profile,
        "-pix_fmt:v:0",
        pixel_format,
        "-b:v:0",
        str(bitrate),
        "-spatial_aq:v:0",
        "1",
        "-prio_speed:v:0",
        "0",
        "-realtime:v:0",
        "0",
        "-filter:v:0",
        ",".join(filters),
        "-fps_mode:v:0",
        "cfr",
    ]
    color_options = {
        "color_primaries": "-color_primaries:v:0",
        "color_transfer": "-color_trc:v:0",
        "color_space": "-colorspace:v:0",
        "color_range": "-color_range:v:0",
    }
    for field, option in color_options.items():
        value = video.get(field)
        if value and value != "unknown":
            command.extend([option, str(value)])
    command.extend(["-max_muxing_queue_size", "4096", "-f", "matroska", str(output)])
    return command


def full_decode(path: Path, ffmpeg: str) -> None:
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-xerror",
        "-hwaccel",
        "videotoolbox",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-f",
        "null",
        "-",
    ]
    run_process(command, timeout=7200)


def compare_streams(
    source_probe: Dict[str, Any],
    output_probe: Dict[str, Any],
    report: Dict[str, Any],
) -> None:
    source_video = primary_video(source_probe)
    output_video = primary_video(output_probe)
    if output_video.get("codec_name") not in {"hevc", "h265"}:
        raise ValidationError("output video is not HEVC")
    complete, missing = hvcc_status(output_video)
    if complete is not True:
        raise ValidationError("output hvcC is incomplete: %s" % ", ".join(missing))
    for field in ("width", "height"):
        if int(source_video.get(field) or 0) != int(output_video.get(field) or 0):
            raise ValidationError("output %s changed" % field)
    expected_rate = output_frame_rate(report)
    actual_rate = video_rate(output_video)
    if actual_rate is None or abs(float(actual_rate - expected_rate)) > 0.01:
        raise ValidationError(
            "output frame rate %s does not match %s"
            % (rate_string(actual_rate), rate_string(expected_rate))
        )
    if bit_depth(source_video) != bit_depth(output_video):
        raise ValidationError("output bit depth changed")
    for field in ("color_primaries", "color_transfer", "color_space", "color_range"):
        source_value = source_video.get(field)
        if source_value and source_value != "unknown" and output_video.get(field) != source_value:
            raise ValidationError("output %s changed" % field)
    for tag in ("language", "title"):
        if source_video.get("tags", {}).get(tag) != output_video.get("tags", {}).get(tag):
            raise ValidationError("output video %s tag changed" % tag)
    if source_video.get("disposition", {}) != output_video.get("disposition", {}):
        raise ValidationError("output video disposition changed")
    if stream_inventory(source_probe) != stream_inventory(output_probe):
        raise ValidationError("audio, subtitle, attachment, data, or chapter metadata changed")
    duration_difference = abs(media_duration(source_probe) - media_duration(output_probe))
    if duration_difference > max(1.0, 2.0 / float(expected_rate)):
        raise ValidationError("output duration changed by %.3f seconds" % duration_difference)


def metric_value(text: str, kind: str) -> float:
    pattern = r"All:([0-9.]+)" if kind == "ssim" else r"average:([0-9.]+|inf)"
    matches = re.findall(pattern, text)
    if not matches:
        raise ValidationError("unable to parse %s metric" % kind.upper())
    return 100.0 if matches[-1] == "inf" else float(matches[-1])


def quality_windows(duration: float, window: int) -> List[float]:
    maximum = max(0.0, duration - window)
    values = [0.0, max(0.0, duration / 2.0 - window / 2.0), maximum]
    result: List[float] = []
    for value in values:
        rounded = round(min(value, maximum), 3)
        if rounded not in result:
            result.append(rounded)
    return result


def quality_metrics(
    source: Path,
    output: Path,
    source_probe: Dict[str, Any],
    report: Dict[str, Any],
    settings: Settings,
) -> Dict[str, Any]:
    depth = bit_depth(primary_video(source_probe))
    pixel_format = "yuv420p10le" if depth > 8 else "yuv420p"
    reference_filters: List[str] = []
    if "hfr" in report.get("signatures", []):
        reference_filters.append("fps=%s" % rate_string(output_frame_rate(report)))
    reference_filters.extend(["setpts=PTS-STARTPTS", "format=%s" % pixel_format])
    output_filters = ["setpts=PTS-STARTPTS", "format=%s" % pixel_format]
    windows: List[Dict[str, Any]] = []
    duration = media_duration(source_probe)
    for position in quality_windows(duration, settings.quality_window_seconds):
        values: Dict[str, Any] = {"position": position}
        for metric in ("ssim", "psnr"):
            graph = "[0:v:0]%s[ref];[1:v:0]%s[dist];[ref][dist]%s" % (
                ",".join(reference_filters),
                ",".join(output_filters),
                metric,
            )
            command = [
                settings.ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-v",
                "info",
                "-ss",
                "%.3f" % position,
                "-t",
                str(settings.quality_window_seconds),
                "-i",
                str(source),
                "-ss",
                "%.3f" % position,
                "-t",
                str(settings.quality_window_seconds),
                "-i",
                str(output),
                "-filter_complex",
                graph,
                "-an",
                "-sn",
                "-f",
                "null",
                "-",
            ]
            result = run_process(command, timeout=1800)
            values[metric] = metric_value(result.stderr, metric)
        windows.append(values)
    mean_ssim = sum(item["ssim"] for item in windows) / len(windows)
    mean_psnr = sum(item["psnr"] for item in windows) / len(windows)
    return {"windows": windows, "mean_ssim": mean_ssim, "mean_psnr": mean_psnr}


def validate_quality(metrics: Dict[str, Any]) -> None:
    if metrics["mean_ssim"] < 0.98 or metrics["mean_psnr"] < 40.0:
        raise ValidationError(
            "quality gate failed: mean SSIM %.5f, mean PSNR %.2f dB"
            % (metrics["mean_ssim"], metrics["mean_psnr"])
        )


def plex_refresh(path: Path, settings: Settings) -> Dict[str, Any]:
    if not settings.plex_plist.is_file():
        return {"status": "skipped", "reason": "Plex preferences not found"}
    try:
        with settings.plex_plist.open("rb") as handle:
            preferences = plistlib.load(handle)
        token = preferences.get("PlexOnlineToken")
    except (OSError, plistlib.InvalidFileException) as exc:
        return {"status": "skipped", "reason": "unable to read Plex preferences: %s" % exc}
    if not token:
        return {"status": "skipped", "reason": "Plex token not found"}
    sections_url = "http://127.0.0.1:32400/library/sections?" + urllib.parse.urlencode(
        {"X-Plex-Token": token}
    )
    try:
        with urllib.request.urlopen(sections_url, timeout=20) as response:
            root = ET.fromstring(response.read())
        matches: List[Tuple[int, int]] = []
        for directory in root.findall("Directory"):
            key = directory.get("key")
            if not key:
                continue
            for location in directory.findall("Location"):
                location_path = location.get("path")
                if location_path and is_relative_to(path, Path(location_path)):
                    matches.append((len(location_path), int(key)))
        if not matches:
            return {"status": "skipped", "reason": "no Plex library contains the path"}
        section = max(matches)[1]
        query = urllib.parse.urlencode(
            {"path": str(path.parent), "X-Plex-Token": token}
        )
        request = urllib.request.Request(
            "http://127.0.0.1:32400/library/sections/%d/refresh?%s" % (section, query),
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=30):
            pass
        return {"status": "requested", "section": section}
    except (urllib.error.URLError, TimeoutError, ET.ParseError) as exc:
        return {"status": "failed", "reason": str(exc)}


def replace_atomically(output: Path, destination: Path) -> None:
    if output.parent != destination.parent:
        raise SafetyError("temporary output is not in the destination directory")
    if not output.name.startswith("." + destination.name + ".media-normalizer."):
        raise SafetyError("refusing to replace from an unexpected temporary path")
    os.replace(output, destination)


def record_for(report: Dict[str, Any], status: str, **extra: Any) -> Dict[str, Any]:
    value = {
        "updated_at": isoformat(utc_now()),
        "file_identity": report.get("file_identity"),
        "signature": report.get("signatures", []),
        "decision": report.get("decision"),
        "status": status,
        "metrics": report,
    }
    value.update(extra)
    return value


def normalize_one(
    path: Path,
    settings: Settings,
    state: Dict[str, Any],
    *,
    dry_run: bool,
    managed: Optional[ManagedMedia] = None,
    clients: Optional[Dict[str, ArrClient]] = None,
) -> Dict[str, Any]:
    path = path.resolve()
    report = inspect_path(path, settings)
    result: Dict[str, Any] = {"path": str(path), "inspection": report}
    if report["decision"] != "normalize":
        result["status"] = report["decision"]
        if not dry_run:
            state["files"][str(path)] = record_for(report, result["status"])
        return result
    if dry_run:
        result["status"] = "would_normalize"
        return result
    try:
        if path.suffix.lower() != ".mkv":
            raise SafetyError("automatic replacement currently requires an MKV destination")
        source_probe = probe_media(path, settings.ffprobe)
        bitrate, ceiling = select_bitrate(source_probe)
        require_stable(path, settings)
        require_inactive(path, settings)
        require_free_space(path.parent, estimated_output_bytes(source_probe, ceiling))
        rollback = create_rollback(path, rollback_path(path, settings))
    except (NormalizerError, OSError) as exc:
        state["files"][str(path)] = record_for(
            report,
            "blocked" if isinstance(exc, SafetyError) else "error",
            error=str(exc),
        )
        atomic_write_json(settings.state_path, state)
        raise
    original_stat = path.stat()
    output = temporary_output(path)
    attempts: List[Dict[str, Any]] = []
    state["files"][str(path)] = record_for(
        report,
        "encoding",
        rollback=rollback,
        original_inode=original_stat.st_ino,
        original_device=original_stat.st_dev,
    )
    state["rollbacks"].append(dict(rollback, source_path=str(path)))
    atomic_write_json(settings.state_path, state)
    try:
        while True:
            with contextlib.suppress(FileNotFoundError):
                output.unlink()
            log("encoding %s at %.3f Mb/s" % (path, bitrate / 1_000_000), settings)
            started = time.monotonic()
            run_process(
                encode_command(path, output, source_probe, report, bitrate, settings.ffmpeg),
                timeout=24 * 3600,
            )
            output_probe = probe_media(output, settings.ffprobe)
            compare_streams(source_probe, output_probe, report)
            full_decode(output, settings.ffmpeg)
            metrics = quality_metrics(path, output, source_probe, report, settings)
            attempt = {
                "bitrate": bitrate,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "output_size": output.stat().st_size,
                "quality": metrics,
            }
            attempts.append(attempt)
            try:
                validate_quality(metrics)
                break
            except ValidationError:
                next_bitrate = retry_bitrate(bitrate, ceiling)
                if next_bitrate is None:
                    raise
                bitrate = next_bitrate
                log("quality gate requested retry at %.3f Mb/s" % (bitrate / 1_000_000), settings)

        require_inactive(path, settings)
        current_stat = path.stat()
        if (current_stat.st_dev, current_stat.st_ino, current_stat.st_size) != (
            original_stat.st_dev,
            original_stat.st_ino,
            original_stat.st_size,
        ):
            raise SafetyError("library file changed before atomic replacement")
        replace_atomically(output, path)
        replacement_stat = path.stat()
        if (replacement_stat.st_dev, replacement_stat.st_ino) == (
            original_stat.st_dev,
            original_stat.st_ino,
        ):
            raise SafetyError("replacement unexpectedly reused the source inode")
        arr_scan: Dict[str, Any] = {"status": "skipped"}
        if managed and clients:
            clients[managed.service].rescan(managed.owner_id)
            arr_scan = {"status": "requested", "service": managed.service, "id": managed.owner_id}
        plex_scan = plex_refresh(path, settings)
        result.update(
            {
                "status": "normalized",
                "rollback": rollback,
                "attempts": attempts,
                "output": {
                    "path": str(path),
                    "inode": replacement_stat.st_ino,
                    "device": replacement_stat.st_dev,
                    "size": replacement_stat.st_size,
                },
                "arr_scan": arr_scan,
                "plex_scan": plex_scan,
            }
        )
        state["files"][str(path)] = record_for(
            report,
            "normalized",
            rollback=rollback,
            attempts=attempts,
            output=result["output"],
            arr_scan=arr_scan,
            plex_scan=plex_scan,
        )
        atomic_write_json(settings.state_path, state)
        return result
    except BaseException as exc:
        status = "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "error"
        state["files"][str(path)] = record_for(
            report,
            status,
            rollback=rollback,
            attempts=attempts,
            error=str(exc),
        )
        atomic_write_json(settings.state_path, state)
        raise
    finally:
        with contextlib.suppress(FileNotFoundError):
            output.unlink()


def cleanup_rollbacks(state: Dict[str, Any], settings: Settings) -> List[str]:
    cutoff = utc_now() - dt.timedelta(days=settings.rollback_days)
    removed: List[str] = []
    for rollback in state.get("rollbacks", []):
        raw_path = rollback.get("path")
        created = rollback.get("created_at")
        if (
            not raw_path
            or not created
            or rollback.get("retired_at")
            or parse_datetime(created) > cutoff
        ):
            continue
        path = Path(raw_path)
        if not is_relative_to(path, settings.rollback_root):
            continue
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
            removed.append(str(path))
        with contextlib.suppress(OSError):
            path.parent.rmdir()
        rollback["retired_at"] = isoformat(utc_now())
    return removed


def all_managed(clients: Dict[str, ArrClient]) -> List[ManagedMedia]:
    found: List[ManagedMedia] = []
    for client in clients.values():
        found.extend(client.managed_media())
    unique: Dict[str, ManagedMedia] = {}
    for item in found:
        if item.path.suffix.lower() in VIDEO_EXTENSIONS:
            unique[str(item.path)] = item
    return sorted(unique.values(), key=lambda item: str(item.path))


def find_managed(path: Path, clients: Dict[str, ArrClient]) -> Optional[ManagedMedia]:
    target = str(path.resolve())
    return next((item for item in all_managed(clients) if str(item.path.resolve()) == target), None)


def run_audit(settings: Settings, *, json_output: bool) -> int:
    clients = arr_clients(settings)
    reports: List[Dict[str, Any]] = []
    failures = 0
    for item in all_managed(clients):
        try:
            report = inspect_path(item.path, settings)
            report.update({"service": item.service, "owner_id": item.owner_id, "file_id": item.file_id})
        except NormalizerError as exc:
            failures += 1
            report = {
                "path": str(item.path),
                "service": item.service,
                "decision": "audit_error",
                "error": str(exc),
            }
        reports.append(report)
        if not json_output and report["decision"] != "compatible":
            print("%-42s %s" % (report["decision"], report["path"]))
    if json_output:
        json.dump(
            {
                "generated_at": isoformat(utc_now()),
                "count": len(reports),
                "failures": failures,
                "reports": reports,
            },
            sys.stdout,
            indent=2,
            sort_keys=True,
        )
        print()
    else:
        findings = sum(1 for report in reports if report["decision"] != "compatible")
        print("Audited %d files; %d findings; %d probe failures." % (len(reports), findings, failures))
    return 0


def run_poll(settings: Settings, *, dry_run: bool) -> int:
    state = load_state(settings.state_path)
    clients = arr_clients(settings)
    cycle = utc_now()
    if not state["watermarks"]:
        if dry_run:
            log("dry run: first real poll would establish the import watermark", settings)
        else:
            state["watermarks"] = {service: isoformat(cycle) for service in clients}
            state["initialized_at"] = isoformat(cycle)
            atomic_write_json(settings.state_path, state)
            log("established first-run watermark; existing media remains report-only", settings)
        return 0

    candidates: Dict[str, ManagedMedia] = {}
    for service, client in clients.items():
        watermark = parse_datetime(state["watermarks"].get(service, ""))
        for item in client.managed_media():
            added = parse_datetime(item.date_added)
            record = state["files"].get(str(item.path), {})
            retry = record.get("automatic") and record.get("status") in RETRYABLE_STATUSES
            if (watermark < added <= cycle) or retry:
                candidates[str(item.path)] = item

    failures = 0
    for item in sorted(candidates.values(), key=lambda value: (value.date_added, str(value.path))):
        try:
            result = normalize_one(
                item.path,
                settings,
                state,
                dry_run=dry_run,
                managed=item,
                clients=clients,
            )
            if not dry_run:
                state["files"][str(item.path)]["automatic"] = True
                state["files"][str(item.path)]["service"] = item.service
                state["files"][str(item.path)]["date_added"] = item.date_added
                atomic_write_json(settings.state_path, state)
            log("poll result: %s %s" % (result["status"], item.path), settings)
        except (NormalizerError, OSError) as exc:
            failures += 1
            record = state["files"].setdefault(str(item.path), {})
            record.update(
                {
                    "updated_at": isoformat(utc_now()),
                    "status": "blocked" if isinstance(exc, SafetyError) else "error",
                    "error": str(exc),
                    "automatic": True,
                    "service": item.service,
                    "date_added": item.date_added,
                }
            )
            if not dry_run:
                atomic_write_json(settings.state_path, state)
            log("poll failed for %s: %s" % (item.path, exc), settings, stderr=True)

    if not dry_run:
        state["watermarks"] = {service: isoformat(cycle) for service in clients}
        removed = cleanup_rollbacks(state, settings)
        atomic_write_json(settings.state_path, state)
        if removed:
            log("retired %d rollback links older than %d days" % (len(removed), settings.rollback_days), settings)
    log("poll complete: %d candidate(s), %d failure(s)" % (len(candidates), failures), settings)
    return 1 if failures else 0


def run_normalize_path(settings: Settings, path: Path, *, dry_run: bool) -> int:
    state = load_state(settings.state_path)
    clients = arr_clients(settings)
    managed = find_managed(path, clients)
    try:
        result = normalize_one(
            path,
            settings,
            state,
            dry_run=dry_run,
            managed=managed,
            clients=clients,
        )
    except (NormalizerError, OSError) as exc:
        log("normalize failed for %s: %s" % (path, exc), settings, stderr=True)
        return 1
    if not dry_run:
        atomic_write_json(settings.state_path, state)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def run_normalize_season(
    settings: Settings, series_id: int, season: int, *, dry_run: bool
) -> int:
    state = load_state(settings.state_path)
    clients = arr_clients(settings)
    files = clients["sonarr"].sonarr_season(series_id, season)
    failures = 0
    for item in files:
        try:
            result = normalize_one(
                item.path,
                settings,
                state,
                dry_run=dry_run,
                managed=item,
                clients=clients,
            )
            log("season result: %s %s" % (result["status"], item.path), settings)
        except (NormalizerError, OSError) as exc:
            failures += 1
            log("season failed for %s: %s" % (item.path, exc), settings, stderr=True)
            break
    if not dry_run:
        atomic_write_json(settings.state_path, state)
    log("season complete: %d file(s), %d failure(s)" % (len(files), failures), settings)
    return 1 if failures else 0


def run_sync_policy(settings: Settings, *, verify_only: bool) -> int:
    clients = arr_clients(settings)
    results: List[Dict[str, Any]] = []
    if not verify_only:
        for client in clients.values():
            results.append(client.sync_explicit_hfr_policy())

    failures: List[str] = []
    verification: List[Dict[str, Any]] = []
    for service, client in clients.items():
        formats = {item["name"]: item["id"] for item in client.get("customformat")}
        profiles = {item["name"]: item for item in client.get("qualityprofile")}
        for format_name in ("HFR", "AV1", CUSTOM_HFR_NAME):
            format_id = formats.get(format_name)
            for profile_name in PROFILE_NAMES[service]:
                score = None
                if format_id is not None and profile_name in profiles:
                    item = next(
                        (
                            entry
                            for entry in profiles[profile_name].get("formatItems", [])
                            if entry["format"] == format_id
                        ),
                        None,
                    )
                    score = item.get("score") if item else None
                record = {
                    "service": service,
                    "profile": profile_name,
                    "format": format_name,
                    "score": score,
                }
                verification.append(record)
                if score != SCORE_REJECT:
                    failures.append("%(service)s %(profile)s %(format)s=%(score)s" % record)
    print(json.dumps({"changes": results, "verification": verification}, indent=2, sort_keys=True))
    if failures:
        log("policy verification failed: " + "; ".join(failures), settings, stderr=True)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    audit = subparsers.add_parser("audit", help="inspect all Arr-managed media")
    audit.add_argument("--json", action="store_true", help="emit a machine-readable report")
    poll = subparsers.add_parser("poll", help="process imports newer than the watermark")
    poll.add_argument("--dry-run", action="store_true", help="report without changing state or media")
    normalize = subparsers.add_parser("normalize", help="explicitly normalize existing media")
    selection = normalize.add_mutually_exclusive_group(required=True)
    selection.add_argument("--path", type=Path)
    selection.add_argument("--sonarr-series-id", type=int)
    normalize.add_argument("--season", type=int)
    normalize.add_argument("--dry-run", action="store_true", help="inspect without changing media")
    policy = subparsers.add_parser(
        "sync-policy", help="install or verify explicit frame-rate title rejection"
    )
    policy.add_argument("--verify-only", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "normalize" and args.sonarr_series_id is not None and args.season is None:
        raise SystemExit("--season is required with --sonarr-series-id")
    if args.command == "normalize" and args.path is not None and args.season is not None:
        raise SystemExit("--season is only valid with --sonarr-series-id")
    settings = load_settings()
    try:
        with process_lock(settings.lock_path):
            if args.command == "audit":
                return run_audit(settings, json_output=args.json)
            if args.command == "poll":
                return run_poll(settings, dry_run=args.dry_run)
            if args.command == "sync-policy":
                return run_sync_policy(settings, verify_only=args.verify_only)
            if args.path is not None:
                return run_normalize_path(settings, args.path, dry_run=args.dry_run)
            return run_normalize_season(
                settings,
                args.sonarr_series_id,
                args.season,
                dry_run=args.dry_run,
            )
    except LockBusy as exc:
        log(str(exc), settings, stderr=True)
        return 75
    except (NormalizerError, OSError) as exc:
        log("fatal: %s" % exc, settings, stderr=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
