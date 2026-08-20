#!/bin/zsh
set -u

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

mkdir -p "${LOG_DIR}" "${HOME}/Library/Caches"

log() {
  echo "[$(/bin/date '+%Y-%m-%d %H:%M:%S')] $*" | /usr/bin/tee -a "${LOG_FILE}"
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

deadline=$(( $(/bin/date +%s) + MAX_WAIT_SECONDS ))

within_deadline() {
  [[ "$(/bin/date +%s)" -lt "${deadline}" ]]
}

wait_for_storage() {
  log "Waiting for ${POOL_NAME} at ${POOL_MOUNT}"
  while within_deadline; do
    if /usr/local/zfs/bin/zpool list "${POOL_NAME}" >/dev/null 2>&1 &&
       /sbin/mount | /usr/bin/grep -q " on ${POOL_MOUNT} (zfs" &&
       [[ -d "${POOL_MOUNT}/downloads" ]] &&
       [[ -d "${POOL_MOUNT}/Media" ]]; then
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
  if ! "${DOCKER}" image inspect "${BIND_TEST_IMAGE}" >/dev/null 2>&1; then
    log "Bind-mount test image ${BIND_TEST_IMAGE} is not local; skipping bind-mount probe"
    return 0
  fi

  log "Verifying Docker can bind-mount ${POOL_MOUNT}"
  while within_deadline; do
    if "${DOCKER}" run --rm --network none -v "${POOL_MOUNT}:/data:ro" "${BIND_TEST_IMAGE}" \
      /bin/sh -c 'test -d /data/downloads && test -d /data/Media' >/dev/null 2>&1; then
      log "Docker bind-mount probe passed"
      return 0
    fi
    /bin/sleep "${SLEEP_SECONDS}"
  done

  log "Timed out waiting for Docker bind-mount access to ${POOL_MOUNT}"
  return 1
}

start_compose_stack() {
  if [[ ! -f "${PROJECT_DIR}/docker-compose.yml" ]]; then
    log "Missing ${PROJECT_DIR}/docker-compose.yml"
    return 1
  fi

  cd "${PROJECT_DIR}" || return 1
  log "Running docker compose up -d"
  if "${DOCKER}" compose up -d >>"${LOG_FILE}" 2>&1; then
    log "Compose stack is up"
    "${DOCKER}" compose ps >>"${LOG_FILE}" 2>&1 || true
    return 0
  fi

  log "docker compose up -d failed"
  return 1
}

main() {
  log "plex-docker recovery started"
  wait_for_storage || return 1
  wait_for_docker || return 1
  wait_for_docker_bind_mount || return 1
  start_compose_stack || return 1
  log "plex-docker recovery completed"
}

main
