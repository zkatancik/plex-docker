#!/bin/zsh
set -u

PATH="/usr/local/bin:/opt/homebrew/bin:/Users/zack/.homebrew/bin:/usr/sbin:/usr/bin:/sbin:/bin"

DOCKER="/usr/local/bin/docker"
LOG_DIR="${HOME}/Library/Logs/docker-stack-guardian"
LOG_FILE="${LOG_DIR}/guardian.log"
LOCK_DIR="${HOME}/Library/Caches/docker-stack-guardian.lock"
CONFIG_DIR="${HOME}/.config/docker-stack-guardian"
DISABLED_FILE="${CONFIG_DIR}/disabled-stacks"

/bin/mkdir -p "${LOG_DIR}" "${HOME}/Library/Caches" "${CONFIG_DIR}"

log() {
  echo "[$(/bin/date '+%Y-%m-%d %H:%M:%S')] $*" | /usr/bin/tee -a "${LOG_FILE}"
}

finish() {
  /bin/rmdir "${LOCK_DIR}" >/dev/null 2>&1 || true
}

if ! /bin/mkdir "${LOCK_DIR}" 2>/dev/null; then
  log "Another guardian run is active; exiting"
  exit 0
fi
trap finish EXIT INT TERM

if [[ ! -x "${DOCKER}" ]] || ! "${DOCKER}" info >/dev/null 2>&1; then
  log "Docker is not ready; the next scheduled run will retry"
  exit 1
fi

stack_disabled() {
  local stack_name="$1"
  [[ -f "${DISABLED_FILE}" ]] && /usr/bin/grep -Fqx "${stack_name}" "${DISABLED_FILE}"
}

service_state() {
  local project_dir="$1"
  local compose_file="$2"
  local env_file="$3"
  local service="$4"
  local container_id
  local -a env_args

  env_args=()
  if [[ -n "${env_file}" ]]; then
    env_args=(--env-file "${env_file}")
  fi

  container_id="$(
    "${DOCKER}" compose \
      --project-directory "${project_dir}" \
      --file "${compose_file}" \
      "${env_args[@]}" \
      ps --quiet "${service}" 2>/dev/null
  )"

  if [[ -z "${container_id}" ]]; then
    echo "missing"
    return
  fi

  "${DOCKER}" inspect --format '{{.State.Status}}' "${container_id}" 2>/dev/null || echo "missing"
}

recover_stack() {
  local stack_name="$1"
  local project_dir="$2"
  local compose_file="$3"
  local env_file="$4"
  shift 4

  local -a services
  local -a unavailable
  local -a env_args
  local service
  local state

  services=("$@")
  unavailable=()
  env_args=()
  if [[ -n "${env_file}" ]]; then
    env_args=(--env-file "${env_file}")
  fi

  if stack_disabled "${stack_name}"; then
    log "${stack_name} is disabled in ${DISABLED_FILE}; skipping"
    return 0
  fi

  if [[ ! -d "${project_dir}" || ! -f "${compose_file}" ]]; then
    log "${stack_name} configuration is unavailable: ${compose_file}"
    return 1
  fi
  if [[ -n "${env_file}" && ! -f "${env_file}" ]]; then
    log "${stack_name} environment file is unavailable: ${env_file}"
    return 1
  fi

  for service in "${services[@]}"; do
    state="$(service_state "${project_dir}" "${compose_file}" "${env_file}" "${service}")"
    if [[ "${state}" != "running" ]]; then
      unavailable+=("${service}:${state}")
    fi
  done

  if (( ${#unavailable[@]} == 0 )); then
    return 0
  fi

  log "${stack_name} recovery required (${unavailable[*]})"
  if (
    cd "${project_dir}" &&
    "${DOCKER}" compose \
      --project-directory "${project_dir}" \
      --file "${compose_file}" \
      "${env_args[@]}" \
      up --detach --force-recreate "${services[@]}"
  ) >>"${LOG_FILE}" 2>&1; then
    log "${stack_name} recovery command completed"
    return 0
  fi

  log "${stack_name} recovery failed"
  return 1
}

failures=0

recover_stack \
  "plex-vpn" \
  "/Users/zack/plex-docker" \
  "/Users/zack/plex-docker/docker-compose.yml" \
  "" \
  gluetun qbittorrent flaresolverr qb-port-sync vpn-network-reconciler || (( failures += 1 ))

recover_stack \
  "nextcloud" \
  "/Users/zack/nextcloud-docker" \
  "/Users/zack/nextcloud-docker/docker-compose.yml" \
  "" \
  db redis app cron || (( failures += 1 ))

recover_stack \
  "travelagent" \
  "/Users/zack/travel-prowler/deploy" \
  "/Users/zack/travel-prowler/deploy/compose.yaml" \
  "/Users/zack/travel-prowler/.env.deploy" \
  postgres api worker jobs web edge dozzle cloudflared || (( failures += 1 ))

recover_stack \
  "watchtower" \
  "/Users/zack/watchtower-docker" \
  "/Users/zack/watchtower-docker/docker-compose.yml" \
  "" \
  watchtower || (( failures += 1 ))

recover_stack \
  "hockey-standings" \
  "/Users/zack/Library/Mobile Documents/com~apple~CloudDocs/Hockey/code/hockey-standings-service" \
  "/Users/zack/Library/Mobile Documents/com~apple~CloudDocs/Hockey/code/hockey-standings-service/compose.yaml" \
  "" \
  hockey-standings || (( failures += 1 ))

if (( failures > 0 )); then
  log "Guardian completed with ${failures} failed stack recovery attempt(s)"
  exit 1
fi

log "Guardian check completed successfully"
