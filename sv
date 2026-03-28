#!/usr/bin/env bash
# state-verify: wrapper script
# Usage: ./sv <command> [options]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/state_verify.py" "$@"
