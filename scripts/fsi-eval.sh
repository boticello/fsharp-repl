#!/bin/sh
# A stable shell entry point for one-shot F# Interactive calls.
set -eu
tool_root=$(cd "$(dirname "$0")/.." && pwd)
exec python3 "$tool_root/scripts/fsi-eval.py" "$@"
