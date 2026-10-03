#!/usr/bin/env bash
# Keep this terminal running while the local Android controller uses the API.
set -euo pipefail
exec ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 -L 127.0.0.1:18085:127.0.0.1:18085 n30285
