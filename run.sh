#!/usr/bin/env sh
# Helper for running the digest with plain Docker (no Compose needed).
#   ./run.sh start     build and start the weekly scheduler
#   ./run.sh update    pull the latest code, rebuild, restart the scheduler
#   ./run.sh now       send this week's digest now
#   ./run.sh test      send a short setup-check email
#   ./run.sh preview   write out/preview.html without sending
#   ./run.sh logs      follow the scheduler's logs
#   ./run.sh stop      stop and remove the scheduler
# Extra arguments pass through, e.g. ./run.sh now --to me@example.com
set -e
cd "$(dirname "$0")"

IMAGE=birdnet-digest
NAME=birdnet-digest

[ -f .env ] || { echo "No .env found. Copy .env.example to .env and fill it in."; exit 1; }
mkdir -p out

build() { docker build -t "$IMAGE" .; }
once() { docker image inspect "$IMAGE" >/dev/null 2>&1 || build; docker run --rm --network host --env-file .env -v "$PWD/out:/out" "$IMAGE" "$@"; }
start() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker run -d --name "$NAME" --restart unless-stopped --network host --env-file .env "$IMAGE"
  echo "Scheduler running. ./run.sh logs to watch it."
}

cmd=${1:-help}
[ $# -gt 0 ] && shift
case "$cmd" in
  start)   build && start ;;
  update)  git pull --ff-only && build && start ;;
  now)     once --now "$@" ;;
  test)    once --test "$@" ;;
  preview) once --preview /out/preview.html "$@" && echo "Open out/preview.html" ;;
  logs)    docker logs -f "$NAME" ;;
  stop)    docker rm -f "$NAME" ;;
  *)       sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
