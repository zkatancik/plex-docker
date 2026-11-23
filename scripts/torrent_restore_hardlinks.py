#!/usr/bin/env python3
"""
Recreate torrent payload paths as hard links to library files (same inode),
so qBittorrent can seed again after *arr moved/rename-imported the videos.

Usage:
  python3 torrent_restore_hardlinks.py \\
    --torrent /path/to/name.torrent \\
    --torrent-root /path/to/save/path/where/qbit/expects/content \\
    --library-glob '/media/tv/Show Name/Season */*.mkv'

Requires: source and destination paths on the same filesystem for hard links.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path


def bdecode(data: bytes, i: int = 0) -> tuple[object, int]:
    if data[i : i + 1] == b"i":
        i += 1
        end = data.index(b"e", i)
        return int(data[i:end]), end + 1
    if data[i : i + 1] == b"l":
        i += 1
        out = []
        while data[i : i + 1] != b"e":
            v, i = bdecode(data, i)
            out.append(v)
        return out, i + 1
    if data[i : i + 1] == b"d":
        i += 1
        out = {}
        while data[i : i + 1] != b"e":
            k, i = bdecode(data, i)
            v, i = bdecode(data, i)
            out[k.decode() if isinstance(k, bytes) else k] = v
        return out, i + 1
    if data[i : i + 1].isdigit() or data[i : i + 1] == b"0":
        colon = data.index(b":", i)
        n = int(data[i:colon])
        start = colon + 1
        end = start + n
        return data[start:end], end
    raise ValueError(f"Invalid bencode at {i}: {data[i : i + 20]!r}")


def _as_bytes(x) -> bytes:
    return x if isinstance(x, bytes) else str(x).encode()


def load_torrent_files(torrent_path: Path) -> list[tuple[str, int]]:
    raw = torrent_path.read_bytes()
    root, _ = bdecode(raw)
    if not isinstance(root, dict):
        raise ValueError("Invalid torrent root")
    info = root.get("info") or root.get(b"info")
    if not isinstance(info, dict):
        raise ValueError("Invalid torrent info")

    files = info.get("files") or info.get(b"files")
    if files:
        out = []
        for f in files:
            path_el = f.get("path") or f.get(b"path")
            length = f.get("length") or f.get(b"length")
            parts = [_as_bytes(p).decode("utf-8", errors="replace") for p in path_el]
            rel = "/".join(parts)
            out.append((rel, int(length)))
        return out
    # single-file torrent
    name = info.get("name") or info.get(b"name")
    length = info.get("length") or info.get(b"length")
    name = _as_bytes(name).decode("utf-8", errors="replace")
    return [(name, int(length))]


def episode_key_from_torrent_basename(basename: str) -> tuple[int, frozenset[int]] | None:
    """Map '...S01E01E02...' or '...S02E01...' -> (season, frozenset(episode numbers))."""
    # Double episodes: S01E01E02, S04E01E02, S07E25E26
    m = re.search(r"S(\d+)E(\d+)E(\d+)", basename, re.I)
    if m:
        return int(m.group(1)), frozenset({int(m.group(2)), int(m.group(3))})
    m = re.search(r"S(\d+)E(\d+)\s*-\s*", basename, re.I)
    if m:
        return int(m.group(1)), frozenset({int(m.group(2))})
    return None


def episode_key_from_library_basename(basename: str) -> tuple[int, frozenset[int]] | None:
    """Parse 'Show - S01E01-E02 - Title.mkv' or 'Show - S02E01 - Title.mkv'."""
    m = re.search(r"S(\d+)E(\d+)-E(\d+)", basename, re.I)
    if m:
        return int(m.group(1)), frozenset({int(m.group(2)), int(m.group(3))})
    m = re.search(r"S(\d+)E(\d+)\s*-", basename, re.I)
    if m:
        return int(m.group(1)), frozenset({int(m.group(2))})
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Hard-link library files back to torrent paths for seeding.")
    ap.add_argument("--torrent", type=Path, required=True)
    ap.add_argument("--torrent-root", type=Path, required=True, help="Folder qBittorrent uses as torrent content path")
    ap.add_argument("--library-glob", type=str, required=True, help="Glob for mkv files, e.g. '/path/Show/Season */*.mkv'")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    torrent_root: Path = args.torrent_root.resolve()
    entries = load_torrent_files(args.torrent)
    from glob import glob

    lib_paths = [Path(p) for p in glob(args.library_glob) if str(p).lower().endswith(".mkv")]

    lib_map: dict[tuple[int, frozenset[int]], Path] = {}
    for p in lib_paths:
        key = episode_key_from_library_basename(p.name)
        if not key:
            print(f"WARN skip library (unparsed): {p}", file=sys.stderr)
            continue
        if key in lib_map:
            print(f"WARN duplicate key {key}: {lib_map[key]} and {p}", file=sys.stderr)
        lib_map[key] = p

    linked = 0
    skipped = 0
    errors = 0

    for rel, _size in entries:
        if not rel.lower().endswith(".mkv"):
            continue
        dest = torrent_root / rel
        base = Path(rel).name
        key = episode_key_from_torrent_basename(base)
        if not key:
            print(f"ERR unparsed torrent file: {rel}", file=sys.stderr)
            errors += 1
            continue
        src = lib_map.get(key)
        if not src:
            print(f"ERR no library file for {rel} key={key}", file=sys.stderr)
            errors += 1
            continue

        if dest.exists():
            if dest.samefile(src):
                skipped += 1
                continue
            # Original payload still here (e.g. season 1) — do not replace
            if dest.stat().st_ino != src.stat().st_ino:
                print(f"SKIP exists (different file): {dest}", file=sys.stderr)
                skipped += 1
                continue

        dest.parent.mkdir(parents=True, exist_ok=True)
        if args.dry_run:
            print(f"ln {src} -> {dest}")
            linked += 1
            continue
        try:
            os.link(src, dest)
            linked += 1
        except OSError as e:
            print(f"ERR link failed {src} -> {dest}: {e}", file=sys.stderr)
            errors += 1

    print(f"Done: linked={linked} skipped={skipped} errors={errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
