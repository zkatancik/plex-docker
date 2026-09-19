# VPN network reconciler

`qbittorrent`, `flaresolverr`, and `qb-port-sync` use
`network_mode: service:gluetun`. Docker resolves that setting to the current
Gluetun container ID when each dependent container is created.

If an image updater replaces Gluetun, an existing dependent can retain the old
network namespace. It may continue running until the Docker engine restarts,
but it cannot start afterward because the referenced Gluetun container no
longer exists. A Docker restart policy only retries the stale container
configuration; it does not recreate it with the new Gluetun ID.

Restarting Gluetun in place can also strand dependents, even though its
container ID and their `NetworkMode` settings still match. Their localhost
health checks can pass while Radarr, Sonarr, and Prowlarr cannot reach them.
The reconciler compares `/proc/1/ns/net` in each running container against
Gluetun's live namespace to detect this case without relying on container IDs.

## Protection

The Compose services declare explicit Watchtower dependencies so a normal
Gluetun or qBittorrent update includes the appropriate dependents.

`vpn-network-reconciler` also checks the VPN group every 60 seconds. It:

1. Restores or restarts Gluetun if it is stopped or unhealthy.
2. Waits for Gluetun to become healthy, keeping dependents fail-closed if it
   does not.
3. Detects stopped, unhealthy, missing, or stale-namespace dependents, including
   live namespace mismatches after an in-place restart.
4. Force-recreates qBittorrent and FlareSolverr, followed by the port manager,
   against the current Gluetun namespace.

A five-minute repair cooldown prevents a persistent failure from causing a
rapid recreation loop. Both intervals are configurable with
`INTERVAL_SECONDS` and `REPAIR_COOLDOWN_SECONDS`.

Docker health requires a successful verification within the last three
minutes, not merely a running loop. A verification checks that all dependents
are running, healthy, and attached to the current namespace. Starting
containers are given time to become ready. Failed recovery commands and
unreadable namespace probes never refresh the success marker. Recreated
services are verified on the next polling cycle.

## Operations

Deploy or refresh the reconciler:

```sh
docker compose up -d vpn-network-reconciler
docker compose restart vpn-network-reconciler
```

Inspect its status and repair history:

```sh
docker compose ps vpn-network-reconciler
docker compose logs vpn-network-reconciler
```

Run a single reconciliation cycle manually (it can repair services):

```sh
docker exec vpn-network-reconciler sh /usr/local/bin/reconcile.sh --once
```

Unreadable namespace probes defer repair and report failure, rather than
recreating healthy downloads without evidence. `--once` exits successfully
after a requested recreation; Docker health waits for the next successful
verification. Run the regression suite with:

```sh
python3 -m unittest discover -s tests -p test_vpn_network_reconciler.py -v
```

The `Check stack automation` GitHub Actions workflow runs all automation
regression tests on relevant pushes and pull requests. Runtime credentials
and application databases are not needed by these tests.

The service mounts the Docker socket and the stack's Compose and environment
files so it can perform recovery. Docker socket access is effectively
host-root access; the service uses the official Docker CLI image, is excluded
from Watchtower updates, and limits Compose operations to this stack's VPN
services.
