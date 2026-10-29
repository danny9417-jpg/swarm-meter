#!/usr/bin/env bash
# Locate this checkout/installation even when the path contains spaces.
# The Python entry point discovers its sibling venv and optional native libs.
set -eu
SWARM_APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [ -x "$SWARM_APP_DIR/.venv/bin/python" ]; then
  SWARM_PYTHON="$SWARM_APP_DIR/.venv/bin/python"
else
  SWARM_PYTHON="python3"
fi
exec "$SWARM_PYTHON" "$SWARM_APP_DIR/swarm_meter.py" "$@"
