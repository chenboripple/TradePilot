#!/bin/sh
set -eu

deployment_mode="${1:-production}"

if ! command -v docker >/dev/null 2>&1; then
    echo "Docker is not installed or not available in PATH." >&2
    exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
    echo "Docker Compose plugin is required." >&2
    exit 1
fi

if [ ! -f .env ]; then
    echo "Missing .env. Create it from .env.example and add the required secrets." >&2
    exit 1
fi

mkdir -p data output

case "$deployment_mode" in
    local)
        docker compose -f compose.yaml -f compose.local.yaml up -d --build --remove-orphans
        ;;
    production)
        docker compose --profile auto-update pull
        docker compose --profile auto-update up -d --remove-orphans
        ;;
    *)
        echo "Usage: $0 [local|production]" >&2
        exit 1
        ;;
esac

docker compose ps
