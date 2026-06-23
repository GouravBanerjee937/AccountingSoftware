#!/bin/bash
# Start the accounting app + in-app assistant with all keys loaded.
# Usage:  bash run_server.sh     (Ctrl+C to stop)
cd "$(dirname "$0")"
# Load keys from set_keys.sh — look next to this script first, then the sibling invoice-mcp repo.
for f in "./set_keys.sh" "../invoice-mcp/set_keys.sh"; do
  [ -f "$f" ] && source "$f" && break
done
exec ./.venv/bin/python server.py
