# Movie acquisition and VPN recovery

Plex runs on the Mac. Radarr imports movies into `/data/Media/movies`, which
Plex sees as `/Volumes/HomeLabPool/Media/movies`. A request or trailer
placeholder in Plex does not mean the full movie has been downloaded.

## VPN restart failure repaired on 2026-09-18

Gluetun restarted in place while qBittorrent, FlareSolverr, and the port
manager remained in its old Linux network namespace. All three still passed
their local health checks. Radarr and Sonarr could not reach qBittorrent,
Prowlarr could not reach FlareSolverr, and download cleanup kept retrying.

Radarr and Sonarr now address qBittorrent as `gluetun:8080`, and Prowlarr
addresses FlareSolverr as `http://gluetun:8191/`. Docker service discovery
keeps these connections independent of the Mac's LAN address.

The old reconciler compared container IDs and Docker health alone. Because
Gluetun kept its ID, it missed this failure. The reconciler now also compares
each running container's `/proc/1/ns/net` with Gluetun's, then recreates the
dependents when they differ. The existing 60-second interval and five-minute
repair cooldown remain in effect. Failed namespace inspection defers repair.
The reconciler's Docker health now requires a recent verification of healthy
dependents; failed checks cannot look healthy merely because the loop is alive.

Check actual namespace membership:

```sh
for service in gluetun qbittorrent flaresolverr qb-port-sync; do
  echo "$service"
  docker exec "$service" readlink /proc/1/ns/net
done
```

All four should return the same `net:[...]` value. A container reporting
`healthy` is not sufficient evidence that other services can reach it.

See [the reconciler operations guide](../containers/vpn-network-reconciler/README.md)
for deployment, manual recovery, and regression tests.

## Prompt Plex scans after import

Radarr and Sonarr previously had no Plex connection configured. Plex's
30-minute periodic scan was the fallback when filesystem events did not
promptly detect an import on the ZFS pool.

Both applications now have a tested **Plex - scan after import** connection,
enabled on import, upgrade, and rename. It connects to
`host.docker.internal:32400` and maps `/data` to `/Volumes/HomeLabPool` so
Plex scans the correct host directory. Credentials remain in the private
application configuration. Plex's periodic scan remains enabled as a fallback.

## Audio and subtitles

Train to Busan had an additional blocker: all Radarr movie profiles required
English audio. They now use **Original**, following the user's preference.
English-language films still prefer English; foreign films can use their
original audio. Recyclarr synchronizes custom formats only and preserves this
application setting.

Bazarr's movie default is **English for original audio**:

- Non-English audio requests full English subtitles.
- English audio requests forced English subtitles for foreign dialogue.
- Existing embedded subtitles count toward those requirements.

The old **English Forced** profile remains available for television. The new
movie default applies when Bazarr adds a movie; existing assignments are not
bulk-rewritten.

## External catalog changes

AnimeTosho was disabled in Prowlarr, Radarr, and Sonarr after its searchless
API requests started returning HTTP 404. Its operator announced that the
feed/storage server is shutting down; retrying or restarting the stack cannot
repair that retired service. Other existing indexers remain configured.
Source: [AnimeTosho shutdown notice](https://animetosho.org/).

TheTVDB retired the separate Grand Tour (2026) entry and lists those six
episodes as season 7 of The Grand Tour (2016), TVDB ID 314087.
Source: [TheTVDB episode listing](https://thetvdb.com/series/the-grand-tour-2016/allseasons/official).

All six files were hardlinked into the canonical series, verified by inode
and size, and indexed by Sonarr before its retired entry was removed without
deleting files. The original folder is retained outside Plex's library at
`/Volumes/HomeLabPool/.repair-backups/20260918-201220/The Grand Tour (2026)`.

Settings and metadata captured before this repair are stored in the ignored
`repair-backups/` directory. These contain private configuration and must not
be committed.

## Verification

Both Train to Busan and The Twilight Saga: Eclipse completed downloading,
imported into Radarr, and appeared in Plex with their full movie files. Plex
reports Korean audio and full English subtitles for Train to Busan. Its
import exercised the new automatic scan connection. The Grand Tour retains
the watched state of its first episode after the catalog correction.

All 16 Compose services reported healthy. Radarr, Sonarr, Prowlarr, and
Bazarr returned empty health-warning lists. Download cleanup completed a
successful run after recovery. Eight reconciler regression tests passed,
including the same-ID/different-namespace failure, unchanged healthy
containers, replaced Gluetun IDs, unreadable namespace probes, startup
readiness, and failed recovery commands. All 102 repository tests passed.
Shell syntax, Compose configuration validation, and diff whitespace checks
passed. A controlled live restart of Gluetun reproduced the same-ID namespace
failure; the running reconciler recreated all three dependents automatically
and restored their shared network and health. Future changes are checked by
the `Check stack automation` GitHub Actions workflow.

## MP4 normalizer destinations

The normalizer supports both MKV and MP4 library files. MP4 output keeps its
container and pathname, uses the Apple-compatible `hvc1` HEVC tag, and moves
the metadata to the beginning of the file for playback. Audio, subtitles,
cover art, other secondary video streams, dispositions, and chapter inventory
must survive validation. MKV output retains its existing muxer options.

Both formats use the same source-stability, active-playback, free-space,
decode, quality, rollback, and atomic-replacement checks. The source is
hardlinked into the rollback area before encoding; replacing the library
file does not modify a download's seeding inode. Other destination formats
remain blocked. Run the normal poll under its existing process and shared
library locks; an overlapping scheduled poll exits without another encoder.

Quality comparisons start on shared frame boundaries and use the same frame
clock for both samples. Seeking halfway through a 25 fps frame used to shift
the 50 fps reference's frame selection; rounded Matroska timestamps also
misaligned NTSC comparisons. Both could reject a lossless conversion. The
regression tests exercise real lossless 50/25 and 59.94/29.97 fps fixtures
when FFmpeg is installed. The SSIM and PSNR thresholds are unchanged.

## Reconciling completed acquisitions

Seerr's Radarr and Sonarr scans reconcile request availability from the
download managers. If Plex and Arr both have a full file but a request still
shows processing, run the corresponding scan from Seerr's jobs settings and
verify the request afterward. A Plex trailer placeholder alone is insufficient.

A catalog correction can leave an old torrent in Sonarr's unknown-series
queue even after every episode has been imported under its canonical series.
The automatic queue reconciler deliberately leaves unknown identities alone.
Before moving such a torrent to `sonarr-rejected`, verify each requested
episode in Sonarr and compare the library and torrent payload inodes and
sizes. Change only the category; preserve the torrent, payload, share limits,
and cumulative seed history. Recheck Sonarr and Unpackerr after their next
polls. The retention guard also covers the rejected category.

For a reproducibly damaged library file, an otherwise acceptable replacement
can be rejected because Arr still counts the existing file. First identify a
replacement within the current profile and preserve the damaged source with
a hardlink outside the library. Under the normalizer and library locks, check
for playback, remove only that damaged Arr file, and re-evaluate the release.
Download only after Arr accepts it with no rejection reasons; restore the
backup if that cannot be confirmed. Validate the imported file by full decode,
Plex visibility, and its retained seeding payload. A missing acceptable source
does not justify bypassing quality rules or discarding the original.
