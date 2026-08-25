#!/usr/bin/env bash
set -euo pipefail

KATAGO_HOME="${KATAGO_HOME:-/workspace/katago}"
export HOME="${HOME:-$KATAGO_HOME/.home}"

mkdir -p "$KATAGO_HOME" "$HOME"
exec python3 /opt/katago/server.py
