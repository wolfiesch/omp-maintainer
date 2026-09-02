#!/usr/bin/env bash
set -euo pipefail

# Copy optional image- or deployment-provided agent configuration into the
# private runtime HOME. A standalone image needs neither file; empty staging
# directories keep the same startup path for deployments that add one.
staged_home=/srv/agent-home-stage
runtime_home=/srv/agent-home

umask 077
rm -rf "${runtime_home}/.agent" "${runtime_home}/.omp/agent"
install --directory --mode=0700 "${runtime_home}" "${runtime_home}/.agent" "${runtime_home}/.omp/agent"

copy_if_present() {
    local source=$1
    local destination=$2

    if [[ -f "${source}" && -s "${source}" ]]; then
        install --directory --mode=0700 "$(dirname "${destination}")"
        install --mode=0600 "${source}" "${destination}"
    fi
}

copy_if_present \
    "${staged_home}/.omp/agent/models.yml" \
    "${runtime_home}/.omp/agent/models.yml"
copy_if_present \
    "${staged_home}/.agent/AGENTS.md" \
    "${runtime_home}/.agent/AGENTS.md"

install --directory --mode=0700 /data /data/logs /data/workspaces
exec "$@"
