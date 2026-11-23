#!/bin/zsh
set -u
set -o pipefail

PATH="/usr/local/bin:/opt/homebrew/bin:/usr/local/zfs/bin:/usr/sbin:/usr/bin:/sbin:/bin"

PROJECT_DIR="/Users/zack/plex-docker"
POOL_NAME="HomeLabPool"
POOL_MOUNT="/Volumes/HomeLabPool"
LOG_DIR="${HOME}/Library/Logs/plex-docker-recover"
LOG_FILE="${LOG_DIR}/recover.log"
LOCK_FILE="${HOME}/Library/Caches/plex-docker-recover.lock"
MAX_WAIT_SECONDS="${MAX_WAIT_SECONDS:-1800}"
SLEEP_SECONDS=10
DOCKER="/usr/local/bin/docker"
DOCKER_APP="/Applications/Docker.app"
BIND_TEST_IMAGE="python:3.12-alpine"
CONFIG_DIR="${HOME}/.config/docker-stack-guardian"
MAINTENANCE_FILE="${CONFIG_DIR}/maintenance-until"
DISABLED_FILE="${CONFIG_DIR}/disabled-stacks"

mkdir -p "${LOG_DIR}" "${HOME}/Library/Caches"

log() {
  local LC_ALL=C
  local entry="[$(/bin/date '+%Y-%m-%d %H:%M:%S')] $*"
  local current_size=0
  # Bound a single diagnostic as well as the complete log. Keep one old file.
  if (( ${#entry} > 8192 )); then
    entry="${entry[1,8178]} [truncated]"
  fi
  if [[ -L "${LOG_FILE}" || -L "${LOG_FILE}.1" || -L "${LOG_FILE}.tmp" ]]; then
    return 1
  fi
  if [[ -e "${LOG_FILE}" ]]; then
    [[ -f "${LOG_FILE}" ]] || return 1
    current_size="$(/usr/bin/stat -f %z "${LOG_FILE}")" || return 1
  fi
  if (( current_size + ${#entry} + 1 > 1048576 )); then
    /usr/bin/tail -c 1048576 "${LOG_FILE}" >"${LOG_FILE}.tmp" &&
      /bin/mv -f "${LOG_FILE}.tmp" "${LOG_FILE}.1" || return 1
    : >"${LOG_FILE}"
  fi
  print -r -- "${entry}" >>"${LOG_FILE}"
}

exit_for_maintenance() {
  if [[ -e "${MAINTENANCE_FILE}" || -L "${MAINTENANCE_FILE}" ]]; then
    local maintenance_until
    if [[ ! -f "${MAINTENANCE_FILE}" || -L "${MAINTENANCE_FILE}" ]] ||
       [[ "$(/usr/bin/stat -f %z "${MAINTENANCE_FILE}")" -gt 128 ]]; then
      log "Invalid maintenance marker; deferring recovery"
      exit 0
    fi
    maintenance_until="$(<"${MAINTENANCE_FILE}")"
    if [[ "${maintenance_until}" != <-> ]] ||
       [[ "${maintenance_until}" -gt "$(/bin/date +%s)" ]]; then
      log "Scheduled maintenance is active; skipping recovery"
      exit 0
    fi
  fi
}

storage_ready() {
  /Users/zack/.homebrew/bin/python3 /Users/zack/plex-docker/scripts/docker_storage_location.py --check-startup --require-shared-dirs >/dev/null 2>&1
}

finish() {
  /bin/rm -f "${LOCK_FILE}"
}

if [[ -f "${LOCK_FILE}" ]]; then
  previous_pid="$(<"${LOCK_FILE}")"
  if [[ "${previous_pid}" == <-> ]] &&
     /bin/kill -0 "${previous_pid}" >/dev/null 2>&1 &&
     /bin/ps -p "${previous_pid}" -o command= | /usr/bin/grep -q "plex-docker-recover.sh"; then
    log "Another plex-docker recovery run is active; exiting"
    exit 0
  fi

  log "Removing stale recovery lock from PID ${previous_pid:-unknown}"
  /bin/rm -f "${LOCK_FILE}"
fi

if ! ( set -o noclobber; echo "$$" > "${LOCK_FILE}" ) 2>/dev/null; then
  log "Could not acquire recovery lock; exiting"
  exit 1
fi

trap finish EXIT INT TERM
exit_for_maintenance

deadline=$(( $(/bin/date +%s) + MAX_WAIT_SECONDS ))

within_deadline() {
  [[ "$(/bin/date +%s)" -lt "${deadline}" ]]
}

wait_for_storage() {
  log "Waiting for ${POOL_NAME} at ${POOL_MOUNT}"
  while within_deadline; do
    exit_for_maintenance
    if storage_ready; then
      log "Storage is ready"
      return 0
    fi
    /bin/sleep "${SLEEP_SECONDS}"
  done

  log "Timed out waiting for ${POOL_NAME}"
  return 1
}

wait_for_docker() {
  log "Waiting for Docker Desktop"
  local opened=0
  while within_deadline; do
    exit_for_maintenance
    if ! storage_ready; then
      /bin/sleep "${SLEEP_SECONDS}"
      continue
    fi
    if [[ -x "${DOCKER}" ]] && "${DOCKER}" info >/dev/null 2>&1; then
      log "Docker daemon is ready"
      return 0
    fi

    if [[ "${opened}" -eq 0 && -d "${DOCKER_APP}" ]]; then
      log "Opening Docker Desktop"
      /usr/bin/open -ga Docker >/dev/null 2>&1 || true
      opened=1
    fi

    /bin/sleep "${SLEEP_SECONDS}"
  done

  log "Timed out waiting for Docker"
  return 1
}

wait_for_docker_bind_mount() {
  exit_for_maintenance
  storage_ready || return 1
  if ! "${DOCKER}" image inspect "${BIND_TEST_IMAGE}" >/dev/null 2>&1; then
    log "Bind-mount test image ${BIND_TEST_IMAGE} is not local; skipping bind-mount probe"
    return 0
  fi

  log "Verifying Docker can bind-mount ${POOL_MOUNT}"
  while within_deadline; do
    exit_for_maintenance
    storage_ready || return 1
    if "${DOCKER}" run --rm --pull=never --network none -v "${POOL_MOUNT}:/data:ro" "${BIND_TEST_IMAGE}" \
      /bin/sh -c 'test -d /data/downloads && test -d /data/Media' >/dev/null 2>&1; then
      log "Docker bind-mount probe passed"
      return 0
    fi
    /bin/sleep "${SLEEP_SECONDS}"
  done

  log "Timed out waiting for Docker bind-mount access to ${POOL_MOUNT}"
  return 1
}

start_existing_containers() {
  exit_for_maintenance
  storage_ready || return 1
  if [[ -f "${DISABLED_FILE}" ]] && /usr/bin/grep -Fqx plex-docker "${DISABLED_FILE}"; then
    log "plex-docker is disabled; leaving existing containers stopped"
    return 0
  fi

  local container_ids container_id details observed_state service
  local -a owners=() others=()
  if ! container_ids="$("${DOCKER}" ps --all --quiet \
    --filter label=com.docker.compose.project=plex-docker \
    --filter label=com.docker.compose.oneoff=False 2>/dev/null)"; then
    log "Could not discover existing plex-docker containers"
    return 1
  fi
  for container_id in ${(f)container_ids}; do
    [[ -n "${container_id}" ]] || continue
    if ! details="$("${DOCKER}" inspect --format '{{.State.Status}} {{index .Config.Labels "com.docker.compose.service"}}' "${container_id}" 2>/dev/null)"; then
      log "Could not inspect an existing plex-docker container; deferring startup"
      return 1
    fi
    observed_state="${details%% *}"
    service="${details#* }"
    [[ "${observed_state}" == exited || "${observed_state}" == created ]] || continue
    if [[ -f "${DISABLED_FILE}" ]] && /usr/bin/grep -Fqx plex-vpn "${DISABLED_FILE}"; then
      case "${service}" in
        gluetun|qbittorrent|flaresolverr|qb-port-sync|vpn-network-reconciler) continue ;;
      esac
    fi
    if [[ "${service}" == gluetun ]]; then
      owners+=("${container_id}")
    else
      others+=("${container_id}")
    fi
  done
  if (( ${#owners[@]} + ${#others[@]} == 0 )); then
    log "No existing plex-docker containers need startup"
    return 0
  fi
  # Boot recovery must not apply pending Compose edits or create replacements.
  # Start the existing VPN namespace owner before its existing dependents.
  for container_id in "${owners[@]}" "${others[@]}"; do
    exit_for_maintenance
    storage_ready || return 1
    if ! "${DOCKER}" start "${container_id}" 2>&1 |
      while IFS= read -r startup_output || [[ -n "${startup_output}" ]]; do
        log "${startup_output}"
      done; then
      log "Existing container startup failed"
      return 1
    fi
  done
  log "Existing plex-docker containers started"
}

main() {
  log "plex-docker recovery started"
  wait_for_storage || return 1
  wait_for_docker || return 1
  wait_for_docker_bind_mount || return 1
  start_existing_containers || return 1
  log "plex-docker recovery completed"
}

main
