#!/bin/sh

set -u

INTERVAL_SECONDS="${INTERVAL_SECONDS:-60}"
REPAIR_COOLDOWN_SECONDS="${REPAIR_COOLDOWN_SECONDS:-300}"
STACK_DIR="${STACK_DIR:-/stack}"
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-plex-docker}"
DEPENDENT_SERVICES="qbittorrent flaresolverr qb-port-sync"
last_repair_epoch=0

log() {
  printf '%s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"
}

compose() {
  docker compose \
    --project-name "$COMPOSE_PROJECT_NAME" \
    --project-directory "$STACK_DIR" \
    --env-file "$STACK_DIR/.env" \
    --file "$STACK_DIR/docker-compose.yml" \
    "$@"
}

container_state() {
  docker inspect --format '{{.State.Status}}' "$1" 2>/dev/null
}

container_health() {
  docker inspect \
    --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
    "$1" 2>/dev/null
}

wait_for_gluetun() {
  attempts=0
  while [ "$attempts" -lt 24 ]; do
    state="$(container_state gluetun || true)"
    health="$(container_health gluetun || true)"
    if [ "$state" = "running" ] && { [ "$health" = "healthy" ] || [ "$health" = "none" ]; }; then
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 5
  done

  return 1
}

cooldown_elapsed() {
  now="$(date +%s)"
  [ $((now - last_repair_epoch)) -ge "$REPAIR_COOLDOWN_SECONDS" ]
}

mark_repair() {
  last_repair_epoch="$(date +%s)"
}

recover_gluetun_if_needed() {
  state="$(container_state gluetun || true)"
  health="$(container_health gluetun || true)"
  recovered=false

  if [ "$state" != "running" ]; then
    if ! cooldown_elapsed; then
      return 1
    fi
    log "Gluetun is ${state:-missing}; asking Compose to restore it"
    mark_repair
    compose up --detach --no-deps gluetun
    recovered=true
  elif [ "$health" = "unhealthy" ]; then
    if ! cooldown_elapsed; then
      return 1
    fi
    log "Gluetun is unhealthy; restarting it"
    mark_repair
    docker restart gluetun >/dev/null
    recovered=true
  fi

  if ! wait_for_gluetun; then
    return 1
  fi

  # A new Gluetun ID requires immediate dependent recreation, not a cooldown.
  if [ "$recovered" = true ]; then
    last_repair_epoch=0
  fi
}

dependent_repair_reason() {
  service="$1"
  expected_network="$2"

  state="$(container_state "$service" || true)"
  if [ "$state" != "running" ]; then
    printf '%s is %s' "$service" "${state:-missing}"
    return 0
  fi

  network_mode="$(docker inspect --format '{{.HostConfig.NetworkMode}}' "$service" 2>/dev/null || true)"
  if [ "$network_mode" != "$expected_network" ]; then
    printf '%s uses stale network namespace %s' "$service" "${network_mode:-missing}"
    return 0
  fi

  health="$(container_health "$service" || true)"
  if [ "$health" = "unhealthy" ]; then
    printf '%s is unhealthy' "$service"
    return 0
  fi

  return 1
}

reconcile_dependents() {
  gluetun_id="$(docker inspect --format '{{.Id}}' gluetun 2>/dev/null || true)"
  if [ -z "$gluetun_id" ]; then
    log "Cannot inspect Gluetun after recovery attempt; leaving dependents fail-closed"
    return 1
  fi

  expected_network="container:$gluetun_id"
  reasons=""
  for service in $DEPENDENT_SERVICES; do
    reason="$(dependent_repair_reason "$service" "$expected_network" || true)"
    if [ -n "$reason" ]; then
      if [ -n "$reasons" ]; then
        reasons="$reasons; $reason"
      else
        reasons="$reason"
      fi
    fi
  done

  if [ -z "$reasons" ]; then
    return 0
  fi

  if ! cooldown_elapsed; then
    log "Repair is cooling down: $reasons"
    return 1
  fi

  log "Recreating VPN dependents: $reasons"
  mark_repair
  compose up --detach --no-deps --force-recreate qbittorrent flaresolverr
  compose up --detach --no-deps --force-recreate qb-port-sync
}

log "VPN network reconciler started"
while :; do
  touch /tmp/vpn-network-reconciler.heartbeat
  if recover_gluetun_if_needed; then
    reconcile_dependents || true
  else
    log "Gluetun did not become healthy; leaving dependents fail-closed"
  fi
  touch /tmp/vpn-network-reconciler.heartbeat
  sleep "$INTERVAL_SECONDS"
done
