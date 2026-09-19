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
