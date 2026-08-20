# VPN network reconciler

`qbittorrent`, `flaresolverr`, and `qb-port-sync` use
`network_mode: service:gluetun`. Docker resolves that setting to the current
Gluetun container ID when each dependent container is created.

If an image updater replaces Gluetun, an existing dependent can retain the old
network namespace. It may continue running until the Docker engine restarts,
but it cannot start afterward because the referenced Gluetun container no
longer exists. A Docker restart policy only retries the stale container
configuration; it does not recreate it with the new Gluetun ID.

## Protection

The Compose services declare explicit Watchtower dependencies so a normal
Gluetun or qBittorrent update includes the appropriate dependents.

`vpn-network-reconciler` also checks the VPN group every 60 seconds. It:

1. Restores or restarts Gluetun if it is stopped or unhealthy.
2. Waits for Gluetun to become healthy, keeping dependents fail-closed if it
   does not.
3. Detects stopped, unhealthy, missing, or stale-namespace dependents.
4. Force-recreates qBittorrent and FlareSolverr, followed by the port manager,
   against the current Gluetun namespace.

A five-minute repair cooldown prevents a persistent failure from causing a
rapid recreation loop. Both intervals are configurable with
`INTERVAL_SECONDS` and `REPAIR_COOLDOWN_SECONDS`.

## Operations

Deploy or refresh the reconciler:

```sh
docker compose up -d vpn-network-reconciler
```

Inspect its status and repair history:

```sh
docker compose ps vpn-network-reconciler
docker compose logs vpn-network-reconciler
```

The service mounts the Docker socket and the stack's Compose and environment
files so it can perform recovery. Docker socket access is effectively
host-root access; the service uses the official Docker CLI image, is excluded
from Watchtower updates, and limits Compose operations to this stack's VPN
services.
