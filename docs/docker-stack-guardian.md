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
