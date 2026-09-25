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

## Advertise the forwarded VPN address

On 2026-09-23, Proton's NAT-PMP gateway reported a different public address
from outbound HTTP traffic. qBittorrent listened on the correct forwarded
port, but had no announce-address override. Testing the outbound address
timed out; the NAT-PMP address accepted a BitTorrent handshake and served a
16 KiB block from a retained torrent. Zero upload speed and the WebUI's
`firewalled` indicator alone were not sufficient to diagnose this.

`qb-port-sync` now runs `scripts/qb-port-sync.py` in Gluetun's namespace.
Every 60 seconds it reads Gluetun's port file, sends a read-only NAT-PMP
external-address query to `10.2.0.1`, and reconciles both `listen_port` and
`announce_ip`. Gluetun remains responsible for allocating and renewing the
lease. The worker validates the response and port, detects concurrent port
changes, and reads back qBittorrent preferences before recording success.
Its health check requires a verification within three minutes. An unchanged
endpoint produces no settings write; failures never fall back to the host's
public address or log credentials.

Deploy only the affected worker:

```sh
docker compose up -d --no-deps qb-port-sync
docker exec qb-port-sync python3 /app/qb-port-sync.py --health
docker compose logs --tail 10 qb-port-sync
python3 -m unittest discover -s tests -p test_qb_port_sync.py -v
```

For a rollback to the old port-only image, also restore the prior qBittorrent
announce-address setting through its API. Do not leave a static VPN address
configured after removing the worker that keeps it current.

Protocol references: [Proton's manual NAT-PMP setup](https://protonvpn.com/support/port-forwarding-manual-setup)
and [qBittorrent's announce-address setting](https://github.com/qbittorrent/qBittorrent/blob/master/src/base/bittorrent/sessionimpl.cpp).

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

## Sonarr logging database corruption

Sonarr can return an empty health-warning list while `TrimLogDatabase` fails.
Check recent housekeeping logs as well as the health API. On 2026-09-25,
SQLite integrity checks reproduced corruption in `logs.db` (including the
`IX_Logs_Time` index), while `sonarr.db` passed both online and stopped checks.
The initial cause of the corruption was not established.

With the download/import queue empty, stop only Sonarr, preserve its database
files and sidecars in a private ignored backup directory, and move the damaged
`logs.db*` files out of its configuration directory. Start Sonarr so it creates
a fresh logging database. Do not replace the healthy main database. This is
the [Sonarr maintainer's logging-database recovery procedure](https://forums.sonarr.tv/t/getting-a-nzbdroneerrorpipeline-in-the-logs-should-i-be-worried/7289),
with the original files retained instead of deleted.

Validate both databases, the log API, a completed `Housekeeping` command,
unchanged series identities, download-client connectivity, and later worker
cycles. The isolated logging repair preserved all 57 configured series.

## Continuing seasons that were previously complete

The pinned Seerr fork retained `AVAILABLE` for both a season and its parent
show after the catalog added more episodes. For example, The Adam Ray Show
had ten files but thirty catalog episodes, including eight missing aired
episodes, and remained fully available after a Sonarr scan.

`containers/seerr/patch-availability.cjs` removes that stale completion only
when Arr is the configured authority and Sonarr reports a positive incomplete
count for the same resolution. Empty scans, specials, the other resolution,
and configurations without Arr priority keep their existing behavior. Plex
and Jellyfin scans still cannot override Arr availability in priority mode.
The patch changes display availability, not request approval, monitoring,
quality profiles, or downloads.

The build uses the same pinned upstream source and Node base as before,
checks each patch location exactly once, and tests the actual compiled
`processShow` method against nine scenarios. An upstream source change that
invalidates the patch fails the build. Deploy only Seerr:

```sh
docker compose build overseerr
docker compose up -d --no-deps overseerr
```

Run Seerr's Sonarr scan, verify the exact season and media status, then repeat
the scan to check persistence. A successful Plex scan should also leave the
partial status intact. The prior `overseerr:radarr-sonarr-priority` image can
be retained for rollback; application data is unchanged by the image build.
Live validation changed only request 14's media availability from complete
to partial across all 48 requests; approval states were preserved. A Sonarr
scan, Plex full scan, and second Sonarr scan all retained the corrected state.
The repository regression suite now requires Node.js as well as Python.

## Plex catalog identity checks

Full files can be present while their Plex catalog match is missing or wrong.
Compare the exact file path and Radarr TMDB/IMDb IDs against Plex's GUIDs.
Query Plex's movie matcher by `tmdb-<id>` and apply only a verified exact
match through its match API. Preserve the prior metadata privately and verify
the rating key, file path, and watched count afterward. This corrected Busboys
and Obsession on 2026-09-25 without moving or replacing their media.
The API operation is documented in [PlexAPI's match implementation](https://python-plexapi.readthedocs.io/en/latest/_modules/plexapi/mixins/unmatch_match.html).
