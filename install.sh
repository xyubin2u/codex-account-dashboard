#!/usr/bin/env bash
set -euo pipefail
TASK_SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$TASK_SOURCE_DIR/scripts/install.py" "$@"
