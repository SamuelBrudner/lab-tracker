#!/bin/sh

deployment_env="${script_dir}/.env"
provider_env="${script_dir}/provider/.env"
compose_file="${script_dir}/docker-compose.yml"

require_private_env_file() {
  lt_env_path="$1"
  if [ ! -f "${lt_env_path}" ]; then
    echo "Required deployment environment file is missing: ${lt_env_path}" >&2
    return 1
  fi
  if [ -n "$(find "${lt_env_path}" -prune -perm -077 -print)" ]; then
    echo "Deployment environment file must not be accessible by group or others: ${lt_env_path}" >&2
    return 1
  fi
}

require_deployment_envs() {
  require_private_env_file "${deployment_env}"
  require_private_env_file "${provider_env}"
}

env_file_value() {
  lt_value_file="$1"
  lt_env_name="$2"
  lt_env_value="$(
    awk -v name="${lt_env_name}" '
      BEGIN {
        prefix = name "="
      }
      index($0, prefix) == 1 {
        count += 1
        value = substr($0, length(prefix) + 1)
      }
      END {
        if (count != 1 || value == "") {
          exit 1
        }
        print value
      }
    ' "${lt_value_file}"
  )" || {
    echo "Expected exactly one non-empty ${lt_env_name} assignment in ${lt_value_file}." >&2
    return 1
  }
  printf '%s\n' "${lt_env_value}"
}

deployment_value() {
  env_file_value "${deployment_env}" "$1"
}

provider_value() {
  env_file_value "${provider_env}" "$1"
}

require_positive_integer() {
  lt_number_name="$1"
  lt_number_value="$2"
  case "${lt_number_value}" in
    ""|*[!0-9]*)
      echo "${lt_number_name} must be a positive integer." >&2
      return 1
      ;;
  esac
  if [ "${lt_number_value}" -lt 1 ]; then
    echo "${lt_number_name} must be a positive integer." >&2
    return 1
  fi
}

require_compose_project_name() {
  lt_project_name="$1"
  case "${lt_project_name}" in
    ""|[!a-z0-9]*|*[!a-z0-9_-]*)
      echo "DEDICATED_COMPOSE_PROJECT_NAME must use lowercase letters, digits, hyphens, or underscores and start with a letter or digit." >&2
      return 1
      ;;
  esac
}

compose() {
  lt_compose_project_name="$(deployment_value DEDICATED_COMPOSE_PROJECT_NAME)"
  require_compose_project_name "${lt_compose_project_name}"
  if [ -n "${COMPOSE_PROJECT_NAME:-}" ] &&
    [ "${COMPOSE_PROJECT_NAME}" != "${lt_compose_project_name}" ]
  then
    echo "COMPOSE_PROJECT_NAME conflicts with DEDICATED_COMPOSE_PROJECT_NAME." >&2
    return 1
  fi
  docker compose \
    --project-name "${lt_compose_project_name}" \
    --project-directory "${script_dir}" \
    --env-file "${deployment_env}" \
    --env-file "${provider_env}" \
    -f "${compose_file}" \
    "$@"
}
