# Docker Stack Guardian

`scripts/docker-stack-guardian.sh` repairs the homelab services that should
remain available after a macOS reboot or Docker Desktop restart.

The host must use Docker Desktop 4.86 or newer. Earlier 4.81-4.85 releases had
a regression that left `restart: unless-stopped` containers down after the
engine restarted. The guardian is an additional recovery layer for engine,
network, and dependency failures.

The LaunchAgent `com.zack.docker-stack-guardian` runs:

- when the user session starts;
- every five minutes afterward.

The guardian checks only these explicit service groups:

- `plex-vpn`: Gluetun, qBittorrent, FlareSolverr, port sync, and the VPN
  network reconciler;
- `nextcloud`;
- `travelagent`;
- `watchtower`;
- `hockey-standings`.

If every configured service in a group is running, the guardian makes no
changes. If one is missing, exited, or restarting, it asks Docker Compose to
recreate that group's configured services. Recreating the group also repairs
containers that still reference a Docker network deleted during an engine
restart.

## Temporarily disable a stack

Create `~/.config/docker-stack-guardian/disabled-stacks` and put one stack name
per line:

```text
travelagent
hockey-standings
```

Valid names are `plex-vpn`, `nextcloud`, `travelagent`, `watchtower`, and
`hockey-standings`. Remove a name when the guardian should manage that stack
again.

## Operations

Run an immediate check:

```sh
/bin/zsh /Users/zack/plex-docker/scripts/docker-stack-guardian.sh
```

Inspect the LaunchAgent:

```sh
launchctl print gui/$(id -u)/com.zack.docker-stack-guardian
```

Review recovery logs:

```sh
open ~/Library/Logs/docker-stack-guardian
```

The main log is `guardian.log`. LaunchAgent stdout and stderr are recorded in
the same directory.

## Discovering live services

The guardian queries Docker's Compose project and service labels directly,
including stopped containers. It uses explicit project names for each managed
group. Working-directory labels are not reliable identities: VPN dependents
created by the reconciler carry `/stack` while host-created containers carry
the host checkout path.

This avoids reading Compose environment files during healthy checks. An iCloud
file-provider error reading Hockey's `.env` previously appeared as a missing
container and triggered repeated failed recovery attempts even though the
service was running. A failed Docker query or inspection now defers recovery;
only a successful query with no matching container means missing. Actual
recovery still requires readable Compose configuration and credentials.

Run the discovery and deferral regression tests with:

```sh
python3 -m unittest discover -s tests -p test_docker_stack_guardian.py -v
```
