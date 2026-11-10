#!/usr/bin/env bash
# Rebuild LinguaFlow with BuildKit layer cache. A code-only change should only
# rebuild the final COPY app layer (seconds), not apt/pip.
set -Eeuo pipefail
cd "$(dirname "$0")/.."

if [[ "${1:-}" == "--fast" ]]; then
  exec docker compose -f docker-compose.yml -f docker-compose.fast.yml up -d
fi

export DOCKER_BUILDKIT=1
export COMPOSE_DOCKER_CLI_BUILD=1
docker compose build
docker compose up -d --no-build
docker compose ps
