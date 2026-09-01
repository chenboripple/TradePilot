#!/bin/sh
set -eu

python -m ripple_tradePilot.storage
exec "$@"
